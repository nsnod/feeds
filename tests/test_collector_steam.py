"""Steam collector: store search (infinite=1), featuredcategories, appdetails (+ keyed-response quirk)."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import httpx
import pytest
import respx

from gembot.collectors.base import CollectContext, SourceReport
from gembot.collectors.steam import (
    TAG_NAMES,
    AppUnavailable,
    SteamCollector,
    is_skipped_name,
    parse_appdetails,
    parse_featured,
    parse_release_date,
    parse_search_html,
    parse_search_response,
    pick_appdetails_entry,
    strip_html,
)
from gembot.config import Config, SteamSearch
from gembot.http import Budget, BudgetExceeded
from gembot.models import Game, Mention, State, SteamInfo
from tests.factories import NOW, make_config, make_game, make_http, make_mention, read_fixture

STORE = "https://store.steampowered.com"
INTERVAL = 2.0  # sources.yaml steam.request_interval_s
COMINGSOON = SteamSearch(name="cs-online-coop", params={"filter": "comingsoon", "tags": "492,3843"})
POPULAR = SteamSearch(name="popular-coop", params={"filter": "popularcomingsoon", "tags": "492,1685"})

HAUNTED, RAGDOLL, BOG, MOTH = 3456780, 3501230, 3533330, 3544440
PIZZA, PROXIMITY, GOOSE, COUCH = 3567890, 3578900, 3600010, 3611110
ALL_KEPT = {HAUNTED, RAGDOLL, BOG, PIZZA, PROXIMITY, GOOSE, COUCH}  # MOTH is adult-only (success:false)


def fixture_json(name: str) -> Any:
    return json.loads(read_fixture("steam", name))


def appdetails(appid: int, name: str, **data: Any) -> dict:
    payload = {
        "type": "game",
        "name": name,
        "steam_appid": appid,
        "short_description": f"{name} is a co-op game.",
        "developers": [f"{name} Team"],
        "release_date": {"coming_soon": True, "date": "Coming soon"},
    }
    payload.update(data)
    return {str(appid): {"success": True, "data": payload}}


def row_html(
    appid: int | str, name: str, released: str = "Q1 2027", tagids: list | None = None, attrs: str = ""
) -> str:
    tags = f' data-ds-tagids="{json.dumps(tagids)}"' if tagids is not None else ""
    capsule = f'<div class="col search_capsule"><img src="https://cdn.test/{appid}/capsule_sm_120.jpg"></div>'
    return (
        f'<a href="{STORE}/app/{appid}/x/" data-ds-appid="{appid}"{tags}{attrs} class="search_result_row ds_collapse_flag ">'
        f'{capsule}<span class="title">{name}</span>'
        f'<div class="col search_released responsive_secondrow">{released}</div></a>'
    )


def search_body(rows: list[str], total: int | str | None) -> str:
    return json.dumps(
        {"success": 1, "results_html": "<!-- List Items -->\r\n" + "\r\n".join(rows), "total_count": total}
    )


def numbered_page(first: int, count: int, total: int) -> str:
    return search_body([row_html(9_000_000 + i, f"Game {i}") for i in range(first, first + count)], total)


DEFAULT_ROUTES: dict[str, Any] = {
    "search:comingsoon:0": "search_comingsoon_page0.json",
    # later runs sharing one State continue the rotating window (7 rows per fixture page)
    "search:comingsoon:7": "search_comingsoon_page0.json",
    "search:comingsoon:14": "search_comingsoon_page0.json",
    "search:popularcomingsoon:0": "search_popular.json",
    "featured": "featuredcategories.json",
    f"appdetails:{HAUNTED}": "appdetails_coming_soon.json",
    f"appdetails:{RAGDOLL}": "appdetails_keyed_by_other_id.json",
    f"appdetails:{MOTH}": "appdetails_fail.json",
    f"appdetails:{PROXIMITY}": "appdetails_free_empty.json",
    f"appdetails:{COUCH}": "appdetails_released.json",
    f"appdetails:{BOG}": appdetails(
        BOG, "Bog Bodies", release_date={"coming_soon": True, "date": "14 Nov, 2026"}
    ),
    f"appdetails:{PIZZA}": appdetails(PIZZA, "Pizza Goblins", genres=[{"id": "23", "description": "Indie"}]),
    f"appdetails:{GOOSE}": appdetails(GOOSE, "Goose Heist Party"),
}


class FakeSteam:
    """Answers store.steampowered.com from a route table and records every request.

    A route value is a fixture file name (``*.json``), a body string, a JSON-able object,
    a callable ``request -> httpx.Response``, or a list of those served in order (the last
    one repeats). Unknown routes get a 404.
    """

    def __init__(self, routes: dict[str, Any] | None = None):
        self.routes = {**DEFAULT_ROUTES, **(routes or {})}
        self.requests: list[httpx.Request] = []

    @staticmethod
    def key(request: httpx.Request) -> str:
        params = request.url.params
        if request.url.path == "/search/results/":
            return f"search:{params.get('filter')}:{params.get('start')}"
        if request.url.path == "/api/featuredcategories":
            return "featured"
        if request.url.path == "/api/appdetails":
            return f"appdetails:{params.get('appids')}"
        return request.url.path

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        value = self.routes.get(self.key(request))
        if isinstance(value, list):
            value = value.pop(0) if len(value) > 1 else value[0]
        if value is None:
            return httpx.Response(404, text="not found")
        if callable(value):
            return value(request)
        if isinstance(value, str) and value.endswith(".json"):
            value = read_fixture("steam", value)
        if isinstance(value, str):
            kind = "application/json" if value.lstrip().startswith(("{", "[", "null")) else "text/html"
            return httpx.Response(200, text=value, headers={"content-type": kind})
        return httpx.Response(200, json=value)

    @property
    def keys(self) -> list[str]:
        return [self.key(r) for r in self.requests]

    def appdetails_ids(self) -> list[int]:
        return [int(k.split(":")[1]) for k in self.keys if k.startswith("appdetails:")]


def steam_config(**overrides: Any) -> Config:
    config = make_config()
    steam = config.sources.steam.model_copy(update={"searches": [COMINGSOON, POPULAR], **overrides})
    return config.model_copy(update={"sources": config.sources.model_copy(update={"steam": steam})})


@dataclass
class SteamRun:
    collector: SteamCollector
    fake: FakeSteam
    sleeps: list[float] = field(default_factory=list)
    http_sleeps: list[float] = field(default_factory=list)
    mentions: list[Mention] = field(default_factory=list)
    report: SourceReport | None = None

    @property
    def by_appid(self) -> dict[int, Mention]:
        return {int(m.source_id): m for m in self.mentions}


@pytest.fixture
def steam_api() -> Iterator[Callable[..., SteamRun]]:
    with respx.mock(assert_all_called=False) as router:

        def build(
            *,
            config: Config | None = None,
            routes: dict[str, Any] | None = None,
            state: State | None = None,
            budget: Budget | None = None,
        ) -> SteamRun:
            fake = FakeSteam(routes)
            router.route(host="store.steampowered.com").mock(side_effect=fake)
            sleeps: list[float] = []
            http_sleeps: list[float] = []
            http = make_http(sleep=http_sleeps.append)
            ctx = CollectContext(config=config or steam_config(), http=http, now=NOW, state=state)
            collector = SteamCollector(ctx, budget=budget, sleep=sleeps.append)
            return SteamRun(collector, fake, sleeps, http_sleeps)

        yield build


@pytest.fixture
def run_steam(steam_api: Callable[..., SteamRun]) -> Callable[..., SteamRun]:
    def run(**kwargs: Any) -> SteamRun:
        result = steam_api(**kwargs)
        result.mentions, result.report = result.collector.run()
        return result

    return run


def stored_info(appid: int, name: str, *, age_hours: float, **fields: Any) -> SteamInfo:
    return SteamInfo(
        appid=appid, name=name, type="game", fetched_at=NOW - timedelta(hours=age_hours), **fields
    )


def state_with_steam_mention(info: SteamInfo, state: State | None = None) -> State:
    state = state or State()
    mention = make_mention("steam", str(info.appid), extra={"steam": info.model_dump(mode="json")})
    state.mentions[mention.key] = mention
    return state


# ---------------------------------------------------------------- search parsing


def test_parse_comingsoon_fixture_rows():
    page = parse_search_response(read_fixture("steam", "search_comingsoon_page0.json"))
    assert page.total_count == 134  # arrived as the string "134"
    assert page.raw_rows == 7  # incl. bundle, package and playtest rows
    assert [r.appid for r in page.rows] == [HAUNTED, RAGDOLL, BOG, MOTH]
    haunted, ragdoll, _bog, moth = page.rows
    assert haunted.name == "Haunted Shift" and haunted.release_text == "Q1 2027"
    assert haunted.tag_ids == [492, 1685, 3843, 1667, 3859]
    assert haunted.capsule.endswith(f"/apps/{HAUNTED}/capsule_sm_120.jpg?t=1759000000")
    assert ragdoll.name == "Ragdoll Removals & Co."  # &amp; decoded
    assert moth.tag_ids == [] and moth.release_text == "2027"  # no data-ds-tagids attribute


def test_parse_search_bare_html_body():
    html = json.loads(read_fixture("steam", "search_popular.json"))["results_html"]
    page = parse_search_response(html)
    assert [r.appid for r in page.rows] == [PIZZA, HAUNTED, PROXIMITY]  # demo row skipped
    assert page.total_count is None and page.raw_rows == 4


def test_parse_search_empty_and_odd_rows():
    page = parse_search_response(read_fixture("steam", "search_empty.json"))
    assert page.rows == [] and page.raw_rows == 0 and page.total_count == 0
    assert parse_search_response("").rows == []
    assert parse_search_response('{"success": 1}').rows == []
    rows = [
        row_html("abc", "No numeric id"),
        row_html(11, "Bad tags", tagids=None, attrs=' data-ds-tagids="[oops"'),
        row_html(12, "Dict tags", attrs=' data-ds-tagids="{}"'),
        row_html(13, "Mixed tags", attrs=' data-ds-tagids="[1,&quot;x&quot;,&quot;4&quot;]"'),
        '<a class="search_result_row" data-ds-appid="14"></a>',
        row_html(15, "Original Soundtrack OST"),
    ]
    page = parse_search_html("".join(rows), total_count=None)
    assert [(r.appid, r.name, r.tag_ids) for r in page.rows] == [
        (11, "Bad tags", []),
        (12, "Dict tags", []),
        (13, "Mixed tags", [1, 4]),
        (14, "", []),
    ]
    assert page.rows[-1].capsule is None and page.rows[-1].release_text is None
    assert page.raw_rows == 6


@pytest.mark.parametrize(
    "body",
    [
        '{"success": 1, "results_html": "<a class=',  # truncated JSON envelope
        '{"success": 2, "results_html": ""}',
        '{"success": false}',
        '{"success": 1, "results_html": 5}',
    ],
)
def test_parse_search_malformed_envelopes_raise(body):
    with pytest.raises(ValueError):
        parse_search_response(body)


def test_skipped_names():
    for name in ("Lantern Crew Playtest", "Night Shift Diner Demo", "Game OST", "Soundtrack Vol. 2"):
        assert is_skipped_name(name)
    for name in ("Demolition Crew", "Ghost Story", "Haunted Shift", "", None):
        assert not is_skipped_name(name)


# ---------------------------------------------------------------- featured / appdetails parsing


def test_parse_featured_fixture():
    lists = parse_featured(fixture_json("featuredcategories.json"))
    assert [(r.appid, r.name) for r in lists["featured-coming-soon"]] == [
        (GOOSE, "Goose Heist Party"),
        (HAUNTED, "Haunted Shift"),
    ]  # the demo is skipped
    assert [r.appid for r in lists["featured-new-releases"]] == [COUCH]  # type 1 item skipped
    assert lists["featured-new-releases"][0].capsule.endswith(f"/apps/{COUCH}/header.jpg?t=1759000000")


def test_parse_featured_odd_shapes():
    with pytest.raises(ValueError):
        parse_featured([])
    weird = {
        "coming_soon": {
            "items": [
                None,
                {"type": 0, "name": "No id"},
                {"id": 5, "name": "Capsule only", "large_capsule_image": "big.jpg"},
            ]
        },
        "new_releases": {"items": "nope"},
    }
    lists = parse_featured(weird)
    assert [(r.appid, r.capsule) for r in lists["featured-coming-soon"]] == [(5, "big.jpg")]
    assert lists["featured-new-releases"] == []
    assert parse_featured({}) == {"featured-coming-soon": [], "featured-new-releases": []}


def test_parse_appdetails_coming_soon():
    info = parse_appdetails(fixture_json("appdetails_coming_soon.json"), HAUNTED, now=NOW)
    assert info is not None
    assert info.appid == HAUNTED and info.name == "Haunted Shift" and info.type == "game"
    assert info.developers == ["Tiny Ghost Studio"] and info.publishers == ["Tiny Ghost Studio"]
    assert info.category_ids == [2, 1, 9, 38]
    assert info.categories == ["Single-player", "Multi-player", "Co-op", "Online Co-op"]
    assert info.genre_ids == [1, 23] and info.genres == ["Action", "Indie"]  # string ids -> int
    assert info.release_date_text == "Q1 2027" and info.release_date is None and info.coming_soon
    assert not info.early_access and not info.is_free and info.price is None
    assert info.header_image.endswith(f"/apps/{HAUNTED}/header.jpg?t=1759000000")
    assert info.short_description == (
        'A 1-4 player online co-op horror game with proximity chat. Clock in, "fix" the haunted '
        "night shift & try not to scream."
    )
    assert info.fetched_at == NOW


def test_parse_appdetails_released_with_price_and_early_access():
    info = parse_appdetails(fixture_json("appdetails_released.json"), COUCH, now=NOW)
    assert info is not None
    assert info.release_date == date(2026, 9, 30) and not info.coming_soon
    assert info.price == "$14.99"
    assert info.early_access and 70 in info.genre_ids
    assert info.category_ids == [1, 9, 39, 24]


def test_parse_appdetails_keyed_by_other_id_quirk():
    payload = fixture_json("appdetails_keyed_by_other_id.json")
    assert str(RAGDOLL) not in payload
    info = parse_appdetails(payload, RAGDOLL, now=NOW)
    assert info is not None and info.appid == RAGDOLL and info.name == "Ragdoll Removals & Co."


def test_pick_appdetails_entry_matches_steam_appid_only():
    dlc = {"success": True, "data": {"steam_appid": 2, "type": "dlc"}}
    game = {"success": True, "data": {"steam_appid": "1", "type": "game"}}  # str id is coerced
    assert pick_appdetails_entry({"1": dlc, "9": game}, 1) is game  # outer keys are opaque
    assert pick_appdetails_entry({"1": dlc}, 1) is None  # keyed by the requested id, other app's data
    assert pick_appdetails_entry({"5": dlc}, 1) is None  # no lone-entry fallback
    assert pick_appdetails_entry({"1": {"success": False, "data": {"steam_appid": 1}}}, 1) is None
    assert pick_appdetails_entry({}, 1) is None
    with pytest.raises(ValueError):
        pick_appdetails_entry(None, 1)


def test_parse_appdetails_failure_paths():
    with pytest.raises(AppUnavailable) as failed:
        parse_appdetails(fixture_json("appdetails_fail.json"), MOTH, now=NOW)
    assert failed.value.alias is None
    drifted = {"730": {"success": True, "data": {"steam_appid": 2678630, "type": "game"}}}
    with pytest.raises(AppUnavailable) as alias:
        parse_appdetails(drifted, 730, now=NOW)
    assert alias.value.alias == 2678630 and "730->2678630" in str(alias.value)
    assert parse_appdetails(fixture_json("appdetails_free_empty.json"), PROXIMITY, now=NOW) is None
    assert parse_appdetails({"1": {"success": True}, "2": {"success": True, "data": {}}}, 3, now=NOW) is None
    assert parse_appdetails({}, 3, now=NOW) is None
    with pytest.raises(ValueError):
        parse_appdetails(None, 1, now=NOW)


def test_parse_appdetails_tolerates_odd_fields():
    data = {
        "steam_appid": "4",  # seen as both str and int, like required_age
        "required_age": "18",
        "name": "  Odd   Game ",
        "developers": ["Solo Dev", "", 7, None],
        "publishers": "not a list",
        "categories": [{"id": 9, "description": "Co-op"}, "junk", {"description": "No id"}, {"id": "x"}],
        "genres": [{"id": "37", "description": "Free To Play"}, {"id": "", "description": "Early Access"}],
        "release_date": "Coming soon",
        "price_overview": [],
        "is_free": True,
        "short_description": "",
    }
    info = parse_appdetails({"4": {"success": True, "data": data}}, 4, now=NOW)
    assert info is not None
    assert info.name == "Odd Game" and info.type is None
    assert info.developers == ["Solo Dev", "7"] and info.publishers == []
    assert info.category_ids == [9] and info.categories == ["Co-op", "No id"]
    assert info.genre_ids == [37] and info.early_access  # by "Early Access" description
    assert info.release_date_text is None and not info.coming_soon
    assert info.is_free and info.price is None and info.header_image is None and info.short_description == ""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Oct 15, 2026", date(2026, 10, 15)),
        ("15 Oct, 2026", date(2026, 10, 15)),
        ("October 15, 2026", date(2026, 10, 15)),
        ("15 October 2026", date(2026, 10, 15)),
        ("Sep 3, 2026", date(2026, 9, 3)),
        ("Sept. 3, 2026", date(2026, 9, 3)),
        ("  Nov  14,   2026 ", date(2026, 11, 14)),
        ("14 Nov, 2026", date(2026, 11, 14)),
        ("October 2026", None),
        ("Q1 2027", None),
        ("2027", None),
        ("Coming soon", None),
        ("To be announced", None),
        ("Feb 30, 2027", None),
        ("Smarch 12, 2026", None),
        ("12 Smarch, 2026", None),
        ("", None),
        (None, None),
    ],
)
def test_release_date_parsing(text, expected):
    assert parse_release_date(text) == expected


def test_strip_html():
    assert strip_html("<p>Up to <b>4</b> friends&nbsp;&amp; a  ghost.</p><br>Bring a torch") == (
        "Up to 4 friends & a ghost. Bring a torch"
    )
    assert strip_html("  plain\n text ") == "plain text"
    assert strip_html("") == "" and strip_html(None) == ""


# ---------------------------------------------------------------- collector: full run


def test_full_run_builds_one_mention_per_app(run_steam):
    run = run_steam()
    assert run.report.errors == [] and run.report.ok
    assert set(run.by_appid) == ALL_KEPT  # bundle/package/playtest/demo/type-1/adult-only all gone

    haunted = run.by_appid[HAUNTED]
    assert haunted.key == f"steam:{HAUNTED}"
    assert haunted.url == f"{STORE}/app/{HAUNTED}/"
    assert haunted.title == "Haunted Shift"
    assert haunted.text.startswith("A 1-4 player online co-op horror game with proximity chat.")
    assert haunted.author == "Tiny Ghost Studio"
    assert haunted.created_at == NOW and haunted.engagement.total == 0
    # seen in the coming-soon list, the popular list (rank 2) and featured: one mention, ranked
    assert (haunted.rank, haunted.list_size, haunted.channel) == (2, 3, "steam:popular-coop")
    assert haunted.extra["channels"] == [
        "steam:cs-online-coop",
        "steam:popular-coop",
        "steam:featured-coming-soon",
    ]
    assert haunted.extra["tag_ids"] == [492, 1685, 3843, 1667, 3859, 745697]  # union of both rows
    assert haunted.raw_tags == [
        "Single-player",
        "Multi-player",
        "Co-op",
        "Online Co-op",
        "Action",
        "Indie",
        "Online Co-Op",
        "Horror",
        "Multiplayer",
        "Social Deduction",
    ]
    assert haunted.media_thumb.endswith(f"/apps/{HAUNTED}/header.jpg?t=1759000000")
    assert haunted.extra["release_text"] == "Q1 2027"
    steam = SteamInfo.model_validate(haunted.extra["steam"])
    assert steam.appid == HAUNTED and steam.coming_soon and steam.category_ids == [2, 1, 9, 38]
    assert steam.tags == ["Indie", "Co-op", "Online Co-Op", "Horror", "Multiplayer", "Social Deduction"]

    assert run.by_appid[PIZZA].rank == 1 and run.by_appid[PIZZA].list_size == 3
    assert run.by_appid[RAGDOLL].extra["steam"]["name"] == "Ragdoll Removals & Co."  # keyed-by-other-id
    assert run.by_appid[RAGDOLL].channel == "steam:cs-online-coop" and run.by_appid[RAGDOLL].rank is None
    assert run.by_appid[BOG].extra["steam"]["release_date"] == "2026-11-14"

    proximity = run.by_appid[PROXIMITY]  # appdetails data: [] -> kept without details
    assert "steam" not in proximity.extra
    assert (proximity.rank, proximity.title, proximity.text) == (3, "Proximity Panic", "To be announced")
    assert proximity.raw_tags == ["Indie", "Online Co-Op", "Social Deduction"]
    assert proximity.media_thumb == run.by_appid[PROXIMITY].media_thumb
    assert proximity.author is None

    couch = run.by_appid[COUCH]
    assert couch.channel == "steam:featured-new-releases"
    assert couch.extra["steam"]["price"] == "$14.99" and couch.extra["steam"]["early_access"]
    assert couch.extra["release_text"] == "Sep 30, 2026"  # filled from appdetails
    assert couch.extra["tag_ids"] == [] and couch.raw_tags[:2] == ["Multi-player", "Co-op"]


def test_full_run_requests_params_pacing_and_order(run_steam):
    run = run_steam()
    keys = run.fake.keys
    assert keys[:3] == ["search:comingsoon:0", "search:popularcomingsoon:0", "featured"]
    # ranked apps first (by rank), then the rest in discovery order
    assert run.fake.appdetails_ids() == [PIZZA, HAUNTED, PROXIMITY, RAGDOLL, BOG, MOTH, GOOSE, COUCH]
    assert run.report.requests == len(keys) == 11
    assert run.sleeps == [INTERVAL] * 10  # between consecutive requests, not before the first

    search = run.fake.requests[0]
    assert search.url.path == "/search/results/"
    assert dict(search.url.params) == {
        "count": "50",
        "cc": "us",
        "l": "english",
        "category1": "998",
        "ignore_preferences": "1",
        "ndl": "1",
        "untags": "128",
        "filter": "comingsoon",
        "tags": "492,3843",
        "infinite": "1",
        "start": "0",
    }
    for request in run.fake.requests:
        assert request.headers["accept-language"] == "en-US,en;q=0.9"
        assert request.headers["user-agent"] == "GemBot-test/0.1"
    details = run.fake.requests[3]
    assert dict(details.url.params) == {"appids": str(PIZZA), "cc": "us", "l": "english"}
    featured = run.fake.requests[2]
    assert dict(featured.url.params) == {"cc": "us", "l": "english"}
    # rows read from the rotating window; the adult-only app is remembered
    scratch = run.collector.scratch()
    assert scratch["cursor:cs-online-coop"] == 7
    assert scratch["skip"][str(MOTH)]["why"] == "unavailable"
    assert "cursor:popular-coop" not in scratch


def test_disabled_and_featured_off(run_steam):
    off = run_steam(config=steam_config(enabled=False))
    assert off.mentions == [] and off.report.skipped and off.fake.requests == []
    no_featured = run_steam(config=steam_config(featured_categories=False, exclude_tags=[]))
    assert "featured" not in no_featured.fake.keys
    assert GOOSE not in no_featured.by_appid
    assert "untags" not in no_featured.fake.requests[0].url.params


def test_default_sleep_is_the_http_clients(steam_api):
    run = steam_api()
    collector = SteamCollector(run.collector.ctx)
    assert collector.sleep == run.collector.http.sleep
    # Steam requests pass retries=0 per request; the shared client keeps its own retry count
    assert collector.http.retries == 2


# ---------------------------------------------------------------- pagination


def test_rotating_cursor_advances_and_wraps(run_steam):
    search = SteamSearch(name="cs", params={"filter": "comingsoon"}, max_pages=2)
    config = steam_config(searches=[search], featured_categories=False, max_new_apps_per_run=0)
    routes = {
        "search:comingsoon:0": numbered_page(0, 50, 120),
        "search:comingsoon:50": numbered_page(50, 50, 120),
        "search:comingsoon:100": numbered_page(100, 20, "120"),
    }
    state = State()
    starts = []
    cursors = []
    for _ in range(3):
        run = run_steam(config=config, routes=routes, state=state)
        starts.append([k.rsplit(":", 1)[1] for k in run.fake.keys])
        cursors.append(state.meta.collector_state["steam"]["cursor:cs"])
        assert run.report.errors == []
    assert starts == [["0", "50"], ["100", "0"], ["50", "100"]]
    assert cursors == [100, 50, 0]
    assert len(run.mentions) == 70 and all("steam" not in m.extra for m in run.mentions)


def test_cursor_past_the_end_restarts_and_empty_list_stops(run_steam):
    search = SteamSearch(name="cs", params={"filter": "comingsoon"}, max_pages=3)
    config = steam_config(searches=[search], featured_categories=False, max_new_apps_per_run=0)
    state = State()
    state.meta.collector_state["steam"] = {"cursor:cs": 500}
    routes = {"search:comingsoon:500": "search_empty.json", "search:comingsoon:0": numbered_page(0, 50, None)}
    routes["search:comingsoon:50"] = search_body([], None)
    run = run_steam(config=config, routes=routes, state=state)
    assert [k.rsplit(":", 1)[1] for k in run.fake.keys] == ["500", "0", "50"]
    assert state.meta.collector_state["steam"]["cursor:cs"] == 0
    assert len(run.mentions) == 50

    empty = run_steam(config=config, routes={"search:comingsoon:0": "search_empty.json"}, state=State())
    assert empty.fake.keys == ["search:comingsoon:0"] and empty.mentions == []  # no loop on an empty list


def test_html_body_and_malformed_search(run_steam):
    html = json.loads(read_fixture("steam", "search_comingsoon_page0.json"))["results_html"]
    run = run_steam(
        routes={"search:comingsoon:0": html, "search:popularcomingsoon:0": '{"success": 1, "results_'}
    )
    assert {HAUNTED, RAGDOLL, BOG}.issubset(run.by_appid)
    assert run.collector.scratch()["cursor:cs-online-coop"] == 7  # total unknown: advance by rows read
    assert len(run.report.errors) == 1 and "search popular-coop" in run.report.errors[0]
    assert PIZZA not in run.by_appid and run.report.ok


def test_popular_list_ranks_and_paging(run_steam):
    popular = SteamSearch(name="pop", params={"filter": "popularcomingsoon"}, max_pages=3)
    config = steam_config(searches=[popular], featured_categories=False, max_new_apps_per_run=0)
    state = State()
    state.meta.collector_state["steam"] = {"cursor:pop": 400}  # ignored: popular lists start at 1
    routes = {
        "search:popularcomingsoon:0": numbered_page(0, 50, 412),
        "search:popularcomingsoon:50": numbered_page(50, 10, 412),
    }
    run = run_steam(config=config, routes=routes, state=state)
    assert run.fake.keys == ["search:popularcomingsoon:0", "search:popularcomingsoon:50"]
    ranks = sorted((m.rank, m.list_size, m.channel) for m in run.mentions)
    assert ranks[0] == (1, 60, "steam:pop") and ranks[-1] == (60, 60, "steam:pop")
    assert run.by_appid[9_000_000].rank == 1 and run.by_appid[9_000_059].rank == 60

    one_page = steam_config(
        searches=[popular.model_copy(update={"max_pages": 1})],
        featured_categories=False,
        max_new_apps_per_run=0,
    )
    single = run_steam(config=one_page, routes=routes)
    assert single.fake.keys == ["search:popularcomingsoon:0"]  # a full page, but max_pages reached
    assert {m.list_size for m in single.mentions} == {50}


def test_popular_second_page_failure_keeps_first_page(run_steam):
    popular = SteamSearch(name="pop", params={"filter": "popularcomingsoon"}, max_pages=2)
    config = steam_config(searches=[popular], featured_categories=False, max_new_apps_per_run=0)
    routes = {
        "search:popularcomingsoon:0": numbered_page(0, 50, 412),
        "search:popularcomingsoon:50": lambda request: httpx.Response(500),
    }
    run = run_steam(config=config, routes=routes)
    assert len(run.mentions) == 50 and {m.list_size for m in run.mentions} == {50}
    assert len(run.report.errors) == 1 and "HTTP 500" in run.report.errors[0]
    assert len(run.fake.requests) == 2 and run.http_sleeps == []  # Steam requests are not retried


def test_dedupe_keeps_best_rank_and_fills_gaps(run_steam):
    searches = [
        SteamSearch(name="cs-a", params={"filter": "comingsoon", "tags": "1"}),
        SteamSearch(name="pop-a", params={"filter": "popularcomingsoon", "tags": "1"}),
        SteamSearch(name="pop-b", params={"filter": "popularcomingsoon", "tags": "2"}),
    ]
    config = steam_config(searches=searches, featured_categories=False, max_new_apps_per_run=0)
    blank = '<a class="search_result_row" data-ds-appid="77"><span class="title"></span></a>'

    def popular(request: httpx.Request) -> httpx.Response:
        if request.url.params["tags"] == "1":
            rows = [row_html(1, "Other"), row_html(77, "Late Name", "Q3 2027", tagids=[492])]
        else:
            rows = [row_html(77, "Late Name", "Q3 2027", tagids=[1685])]
        return httpx.Response(200, text=search_body(rows, 2))

    run = run_steam(
        config=config,
        routes={"search:comingsoon:0": search_body([blank], 1), "search:popularcomingsoon:0": popular},
    )
    mention = run.by_appid[77]
    assert (mention.rank, mention.list_size, mention.channel) == (1, 1, "steam:pop-b")
    assert mention.title == "Late Name" and mention.text == "Q3 2027"
    assert mention.extra["release_text"] == "Q3 2027"
    assert mention.media_thumb == "https://cdn.test/77/capsule_sm_120.jpg"
    assert mention.raw_tags == ["Indie", "Co-op"] and mention.extra["tag_ids"] == [492, 1685]
    assert mention.extra["channels"] == ["steam:cs-a", "steam:pop-a", "steam:pop-b"]


def test_tag_names_fall_back_to_config_names(steam_api):
    collector = steam_api(config=steam_config(tags={"weird_tag": 999_999, "indie": 492})).collector
    assert collector._tag_names([492, 999_999, 123, 492]) == ["Indie", "weird tag"]
    assert TAG_NAMES[3843] == "Online Co-Op"


# ---------------------------------------------------------------- state reuse / caps / skips


def test_fresh_stored_details_are_reused_without_appdetails(run_steam):
    state = state_with_steam_mention(
        stored_info(HAUNTED, "Haunted Shift (stored)", age_hours=2, developers=["Ghost Co"], tags=["Spooky"])
    )
    # details without a fetch time are never "fresh": asked again, kept when Steam has none
    state_with_steam_mention(SteamInfo(appid=PROXIMITY, name="Proximity Panic (undated)"), state)
    state.games[f"steam:{BOG}"] = make_game(
        f"steam:{BOG}", "Bog Bodies", steam=stored_info(BOG, "Bog (game)", age_hours=1)
    )
    stale = stored_info(RAGDOLL, "Ragdoll (old)", age_hours=30)
    state_with_steam_mention(stale, state)
    corrupt = make_mention("steam", str(GOOSE), extra={"steam": {"appid": "not-an-int"}})
    state.mentions[corrupt.key] = corrupt
    state.mentions[f"steam:{COUCH}"] = make_mention("steam", str(COUCH))  # no details stored

    run = run_steam(state=state)
    fetched = run.fake.appdetails_ids()
    assert HAUNTED not in fetched and BOG not in fetched
    assert {RAGDOLL, GOOSE, COUCH}.issubset(fetched)
    haunted = run.by_appid[HAUNTED]
    assert haunted.title == "Haunted Shift (stored)" and haunted.author == "Ghost Co"
    assert haunted.extra["steam"]["fetched_at"] == (NOW - timedelta(hours=2)).isoformat().replace(
        "+00:00", "Z"
    )
    assert haunted.extra["steam"]["tags"] == ["Spooky"] and "Spooky" in haunted.raw_tags
    assert PROXIMITY in fetched  # appdetails says data: [] -> the undated stored details are kept
    assert run.by_appid[PROXIMITY].extra["steam"]["name"] == "Proximity Panic (undated)"
    assert run.by_appid[BOG].extra["steam"]["name"] == "Bog (game)"
    assert run.by_appid[RAGDOLL].extra["steam"]["name"] == "Ragdoll Removals & Co."  # refreshed
    assert fetched.index(RAGDOLL) > fetched.index(GOOSE)  # stale apps after never-seen ones


def test_max_new_apps_per_run_cap(run_steam):
    state = state_with_steam_mention(stored_info(BOG, "Bog (stale)", age_hours=48))
    run = run_steam(config=steam_config(max_new_apps_per_run=2), state=state)
    assert run.fake.appdetails_ids() == [PIZZA, HAUNTED]  # ranked first
    assert "steam" in run.by_appid[PIZZA].extra and "steam" in run.by_appid[HAUNTED].extra
    assert run.by_appid[BOG].extra["steam"]["name"] == "Bog (stale)"  # stale beats nothing
    for appid in (RAGDOLL, PROXIMITY, GOOSE, COUCH, MOTH):
        assert "steam" not in run.by_appid[appid].extra  # left for the pipeline to enrich
    assert run.report.requests == 5


def test_unavailable_apps_are_skipped_for_a_week(run_steam):
    state = State()
    first = run_steam(state=state)
    assert MOTH in first.fake.appdetails_ids() and MOTH not in first.by_appid
    second = run_steam(state=state)
    assert MOTH not in second.fake.appdetails_ids() and MOTH not in second.by_appid

    skip = state.meta.collector_state["steam"]["skip"]
    skip[str(MOTH)]["at"] = (NOW - timedelta(days=8)).isoformat()
    skip["1"] = "junk"
    skip["2"] = {"at": "not a date"}
    third = run_steam(state=state)
    assert MOTH in third.fake.appdetails_ids()
    assert set(state.meta.collector_state["steam"]["skip"]) == {str(MOTH)}


def test_non_games_renamed_playtests_and_mmos_are_dropped(run_steam):
    routes = {
        f"appdetails:{BOG}": appdetails(BOG, "Bog Bodies - Supporter Pack", type="dlc"),
        f"appdetails:{GOOSE}": appdetails(GOOSE, "Goose Heist Party Playtest"),
        f"appdetails:{PIZZA}": appdetails(
            PIZZA, "Pizza Goblins", genres=[{"id": "29", "description": "Massively Multiplayer"}]
        ),
        f"appdetails:{HAUNTED}": appdetails(
            HAUNTED, "Haunted Shift", categories=[{"id": 20, "description": "MMO"}]
        ),
    }
    run = run_steam(routes=routes)
    assert not {BOG, GOOSE, PIZZA, HAUNTED} & set(run.by_appid)
    assert {RAGDOLL, COUCH}.issubset(run.by_appid)
    skip = run.collector.scratch()["skip"]
    assert skip[str(BOG)]["why"] == "type:dlc" and skip[str(GOOSE)]["why"] == "name"
    assert skip[str(PIZZA)]["why"] == "mmo" and skip[str(HAUNTED)]["why"] == "mmo"


def test_alias_response_keeps_the_app_without_details_and_is_not_refetched(run_steam):
    # keyed by the requested id, but the data is another app's: never trust the outer key
    drifted = {
        str(BOG): {"success": True, "data": {"type": "dlc", "name": "Bog OST", "steam_appid": 3533999}}
    }
    state = State()
    first = run_steam(routes={f"appdetails:{BOG}": drifted}, state=state)
    bog = first.by_appid[BOG]
    assert "steam" not in bog.extra and bog.extra["appdetails_alias"] == 3533999
    assert bog.title == "Bog Bodies"
    assert any(f"alias {BOG}->3533999" in w for w in first.report.warnings)
    assert first.report.errors == []
    assert state.meta.collector_state["steam"]["skip"][str(BOG)]["why"] == "alias:3533999"

    second = run_steam(state=state)
    assert BOG not in second.fake.appdetails_ids() and "steam" not in second.by_appid[BOG].extra
    state_with_steam_mention(stored_info(BOG, "Bog Bodies (old)", age_hours=72), state)
    third = run_steam(state=state)
    assert BOG not in third.fake.appdetails_ids()
    assert third.by_appid[BOG].extra["steam"]["name"] == "Bog Bodies (old)"  # stale details still used


def test_skip_memory_is_capped(steam_api):
    collector = steam_api().collector
    old = (NOW - timedelta(days=1)).isoformat()
    collector.scratch()["skip"] = {str(i): {"at": old, "why": "unavailable"} for i in range(500)}
    collector._remember_skip(42_000_000, "unavailable")
    skip = collector.scratch()["skip"]
    assert len(skip) == 500 and "42000000" in skip


# ---------------------------------------------------------------- HTTP failures / budget


def test_403_stops_the_steam_stage_and_keeps_partial_results(run_steam):
    run = run_steam(
        routes={"search:popularcomingsoon:0": lambda request: httpx.Response(403, text="Access Denied")}
    )
    assert run.fake.keys == ["search:comingsoon:0", "search:popularcomingsoon:0"]  # nothing after the 403
    assert set(run.by_appid) == {HAUNTED, RAGDOLL, BOG, MOTH}
    assert all("steam" not in m.extra for m in run.mentions)
    assert len(run.report.errors) == 1 and "403" in run.report.errors[0]
    assert any("throttled" in w for w in run.report.warnings)
    assert run.report.ok and run.collector.throttled
    assert run.collector.fetch_appdetails(HAUNTED) is None and len(run.fake.requests) == 2


def test_403_during_appdetails_keeps_every_discovered_app(run_steam):
    run = run_steam(routes={f"appdetails:{PIZZA}": lambda request: httpx.Response(403)})
    assert run.fake.appdetails_ids() == [PIZZA]
    assert set(run.by_appid) == ALL_KEPT | {MOTH}
    assert all("steam" not in m.extra for m in run.mentions)
    assert run.report.errors and run.report.warnings


def test_first_429_stops_the_stage_without_retrying(run_steam):
    throttled = lambda request: httpx.Response(429, headers={"Retry-After": "3"})  # noqa: E731
    run = run_steam(routes={"search:popularcomingsoon:0": [throttled, "search_popular.json"]})
    assert run.fake.keys == ["search:comingsoon:0", "search:popularcomingsoon:0"]  # no retry, nothing after
    assert run.http_sleeps == []
    assert set(run.by_appid) == {HAUNTED, RAGDOLL, BOG, MOTH}  # partial results kept
    assert len(run.report.errors) == 1 and "429" in run.report.errors[0]
    assert any("throttled" in w for w in run.report.warnings)
    assert run.collector.throttled and run.report.ok


def test_429_on_the_first_request_fails_the_source(run_steam):
    run = run_steam(routes={"search:comingsoon:0": lambda request: httpx.Response(429)})
    assert run.fake.keys == ["search:comingsoon:0"] and run.mentions == []
    assert run.collector.throttled and not run.report.ok


def test_429_during_appdetails_stops_enrichment(run_steam):
    run = run_steam(routes={f"appdetails:{HAUNTED}": lambda request: httpx.Response(429)})
    assert run.fake.appdetails_ids() == [PIZZA, HAUNTED]
    assert "steam" in run.by_appid[PIZZA].extra and "steam" not in run.by_appid[HAUNTED].extra
    assert set(run.by_appid) == ALL_KEPT | {MOTH}


def test_500_records_error_and_the_rest_continues(run_steam):
    run = run_steam(routes={"search:popularcomingsoon:0": lambda request: httpx.Response(500)})
    assert len(run.report.errors) == 1 and "search popular-coop" in run.report.errors[0]
    assert run.report.ok and not run.collector.throttled
    assert {HAUNTED, RAGDOLL, BOG, GOOSE, COUCH} == set(run.by_appid)
    assert all("steam" in m.extra for m in run.mentions)


def test_featured_and_appdetails_errors_are_isolated(run_steam):
    routes = {
        "featured": "[]",
        f"appdetails:{BOG}": lambda request: httpx.Response(500),
        f"appdetails:{HAUNTED}": "<html>maintenance</html>",
    }
    run = run_steam(routes=routes)
    assert len(run.report.errors) == 3
    assert any(e.startswith("featuredcategories") for e in run.report.errors)
    assert "steam" not in run.by_appid[BOG].extra and "steam" not in run.by_appid[HAUNTED].extra
    assert "steam" in run.by_appid[RAGDOLL].extra


def test_budget_is_enforced_and_run_never_raises(run_steam):
    run = run_steam(budget=Budget("steam", 3))
    assert run.report.requests == 3 and len(run.fake.requests) == 3
    assert any("budget" in w for w in run.report.warnings)
    assert set(run.by_appid) == ALL_KEPT | {MOTH}  # discovered apps survive the budget stop
    assert run.sleeps == [INTERVAL] * 2  # no pointless sleep before the refused request


def test_budget_stop_during_search_still_applies_stored_details(run_steam):
    state = state_with_steam_mention(stored_info(HAUNTED, "Haunted Shift", age_hours=1))
    run = run_steam(budget=Budget("steam", 1), state=state)
    assert run.fake.keys == ["search:comingsoon:0"]
    assert run.by_appid[HAUNTED].extra["steam"]["name"] == "Haunted Shift"
    assert "steam" not in run.by_appid[RAGDOLL].extra
    assert run.report.warnings and run.sleeps == []


# ---------------------------------------------------------------- fetch_appdetails (enrichment API)


def test_fetch_appdetails_quirk_and_none_paths(steam_api):
    routes = {
        "appdetails:7": lambda request: httpx.Response(500),
        "appdetails:8": "<html>oops</html>",
        "appdetails:9": "null",
        "appdetails:10": {"10": {"success": True, "data": {"steam_appid": 11, "name": "Other app"}}},
    }
    run = steam_api(routes=routes)
    collector = run.collector
    info = collector.fetch_appdetails(RAGDOLL)
    assert info is not None and info.appid == RAGDOLL and info.name == "Ragdoll Removals & Co."
    assert collector.fetch_appdetails(MOTH) is None  # success: false
    assert collector.fetch_appdetails(PROXIMITY) is None  # data: []
    assert collector.report.warnings == []
    assert collector.fetch_appdetails(7) is None  # HTTP 500 (Steam requests are not retried)
    assert collector.fetch_appdetails(8) is None  # not JSON
    assert collector.fetch_appdetails(9) is None  # JSON null
    assert collector.fetch_appdetails(10) is None  # only another app's data: alias 10->11
    assert collector.fetch_appdetails("not-an-id") is None  # type: ignore[arg-type]
    assert len(collector.report.warnings) == 5
    assert "alias 10->11" in collector.report.warnings[3]
    assert collector.report.requests == collector.budget.used == 7
    assert run.sleeps == [INTERVAL] * 6  # paced like every other Steam request


def test_fetch_appdetails_raises_only_budget_exceeded(steam_api):
    run = steam_api(budget=Budget("steam", 1))
    assert run.collector.fetch_appdetails(HAUNTED) is not None
    with pytest.raises(BudgetExceeded):
        run.collector.fetch_appdetails(COUCH)


def test_fetch_appdetails_after_403_makes_no_more_requests(steam_api):
    run = steam_api(routes={f"appdetails:{HAUNTED}": lambda request: httpx.Response(403)})
    assert run.collector.fetch_appdetails(HAUNTED) is None
    assert run.collector.throttled and "throttled" in run.collector.report.warnings[0]
    assert run.collector.fetch_appdetails(COUCH) is None
    assert len(run.fake.requests) == 1


def test_game_model_accepts_collected_steam_info(run_steam):
    """The pipeline stores ``extra["steam"]`` on games; make sure it round-trips."""
    run = run_steam()
    info = SteamInfo.model_validate(run.by_appid[COUCH].extra["steam"])
    game = Game(
        game_id=f"steam:{COUCH}",
        title=info.name,
        first_seen=NOW,
        last_seen=NOW,
        steam_appid=info.appid,
        steam=info,
    )
    assert game.best_url() == info.store_url == f"{STORE}/app/{COUCH}/"
