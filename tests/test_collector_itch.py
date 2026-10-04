"""itch.io collector: RSS fixtures -> Mentions, feed merging, Cloudflare handling, paging."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from gembot.collectors import itch as itch_mod
from gembot.collectors.base import CollectContext
from gembot.collectors.itch import (
    ACCEPT,
    ItchCollector,
    ItchFeedError,
    canonical_game,
    classify_response,
    feed_tags,
    is_price_label,
    item_matches,
    keyword_pattern,
    looks_like_rss,
    parse_date,
    parse_feed,
    parse_price,
    short_text,
    title_tokens,
)
from gembot.config import ItchFeed, ItchSource
from gembot.http import Budget
from gembot.models import Engagement, Mention, State
from tests.factories import NOW, fixture_path, make_config, make_http

BASE = "https://itch.io/games"
NP = f"{BASE}/new-and-popular.xml"
NEWEST = f"{BASE}/newest.xml"
COOP = f"{BASE}/new-and-popular/tag-co-op.xml"
COOP_FB = f"{BASE}/tag-co-op.xml"
MULTI = f"{BASE}/new-and-popular/tag-multiplayer.xml"
HORROR = f"{BASE}/new-and-popular/tag-horror.xml"
HORROR_FB = f"{BASE}/tag-horror.xml"

FEED_NP = {"name": "new-and-popular", "url": NP}
FEED_NEWEST = {"name": "newest", "url": NEWEST, "ranked": False}
# the "rank within tag" fallback: single-facet /games/tag-X.xml, all-time Top order
FEED_COOP = {"name": "tag-co-op", "url": COOP, "fallback_url": COOP_FB, "fallback_ranked": True}
FEED_HORROR = {"name": "tag-horror", "url": HORROR, "fallback_url": HORROR_FB, "fallback_ranked": True}
# the default in sources.yaml: sort-only newest feed filtered by tag keywords, no rank
COOP_VIA_NEWEST = {
    "name": "tag-co-op",
    "url": COOP,
    "fallback_url": NEWEST,
    "fallback_keywords": ["co-op", "coop"],
}
HORROR_VIA_NEWEST = {
    "name": "tag-horror",
    "url": HORROR,
    "fallback_url": NEWEST,
    "fallback_keywords": ["horror"],
}


# ---------------------------------------------------------------- helpers


def fx(name: str) -> bytes:
    return fixture_path("itch", name).read_bytes()


def rss(content: bytes | str, status: int = 200, **headers: str) -> httpx.Response:
    body = fx(content) if isinstance(content, str) else content
    hdrs = {"content-type": "application/rss+xml; charset=utf-8", "server": "cloudflare", **headers}
    return httpx.Response(status, content=body, headers=hdrs)


def challenge(status: int = 403, header: bool = True) -> httpx.Response:
    headers = {
        "server": "cloudflare",
        "content-type": "text/html; charset=UTF-8",
        "cf-ray": "8c1f0000abcd-IAD",
    }
    if header:
        headers["cf-mitigated"] = "challenge"
    return httpx.Response(status, content=fx("cloudflare_challenge.html"), headers=headers)


OPENRESTY_403 = httpx.Response(
    403,
    text="<html><head><title>403 Forbidden</title></head><body><center><h1>403 Forbidden</h1>"
    "</center><hr><center>openresty</center></body></html>",
    headers={"content-type": "text/html"},
)


def items_of(name: str) -> list[bytes]:
    return re.findall(rb"<item>.*?</item>", fx(name), re.S)


def feed_of(items: list[bytes]) -> bytes:
    head = b'<?xml version="1.0" encoding="UTF-8" ?><rss version="2.0"><channel><title>t</title>'
    return head + b"\n".join(items) + b"</channel></rss>"


class FakeItch:
    """Serves itch URLs from ``routes``: url -> Response, or a list consumed one per call."""

    def __init__(self) -> None:
        self.routes: dict[str, httpx.Response | list[httpx.Response]] = {}
        self.calls: list[str] = []
        self.requests: list[httpx.Request] = []
        self.unexpected: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        self.requests.append(request)
        route = self.routes.get(url)
        if route is None:
            self.unexpected.append(url)
            return httpx.Response(418, text="no route")
        if isinstance(route, list):
            response = route.pop(0) if len(route) > 1 else route[0]
        else:
            response = route
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)


@pytest.fixture
def itch():
    fake = FakeItch()
    with respx.mock(assert_all_called=False) as router:
        router.route(host="itch.io").mock(side_effect=fake)
        router.route(host__regex=r".+\.itch\.io$").mock(side_effect=fake)
        yield fake
    assert not fake.unexpected, f"unexpected itch requests: {fake.unexpected}"


def itch_config(feeds: list[dict], **settings):
    base = make_config()
    source = ItchSource(feeds=[ItchFeed(**feed) for feed in feeds], **settings)
    return base.model_copy(update={"sources": base.sources.model_copy(update={"itch": source})})


class Sleeps(list):
    def __call__(self, seconds: float) -> None:
        self.append(seconds)


def collector(config=None, *, state=None, budget=None, sleep=None, http=None) -> ItchCollector:
    ctx = CollectContext(config=config or make_config(), http=http or make_http(), now=NOW, state=state)
    return ItchCollector(
        ctx, Budget("itch", budget) if budget else None, sleep=Sleeps() if sleep is None else sleep
    )


def by_key(mentions: list[Mention]) -> dict[str, Mention]:
    return {m.source_id: m for m in mentions}


def thumb(code: str) -> str:
    return f"https://img.itch.zone/aW1nLz{code}LnBuZw==/315x250%23c/{code}.png"


# ---------------------------------------------------------------- config


def test_sources_yaml_itch_feeds_follow_the_research():
    source = make_config().sources.itch
    assert source.enabled and source.popular_feed == "new-and-popular"
    assert source.request_interval_s == 1.0 and source.max_challenges == 2
    feeds = {feed.name: feed for feed in source.feeds}
    assert feeds["new-and-popular"].url == NP and feeds["new-and-popular"].ranked
    assert feeds["newest"].url == NEWEST and not feeds["newest"].ranked and feeds["newest"].max_pages >= 1
    slugs = ["multiplayer", "co-op", "local-multiplayer", "horror", "physics"]
    for slug in slugs:
        feed = feeds[f"tag-{slug}"]
        assert feed.url == f"{BASE}/new-and-popular/tag-{slug}.xml"
        assert feed.fallback_url == NEWEST and not feed.fallback_ranked and feed.fallback_keywords
        assert feed.ranked and feed_tags(feed.url) == [slug]
    assert "co-op" in feeds["tag-co-op"].fallback_keywords
    assert "horror" in feeds["tag-horror"].fallback_keywords
    assert "tag-coop" not in " ".join(f.url for f in source.feeds)


def test_disabled_or_empty_config_is_skipped(itch):
    mentions, report = collector(itch_config([FEED_NP], enabled=False)).run()
    assert mentions == [] and report.skipped and "disabled" in (report.skip_reason or "")
    mentions, report = collector(itch_config([])).run()
    assert report.skipped and "no itch feeds" in (report.skip_reason or "")
    assert itch.calls == []


# ---------------------------------------------------------------- parsing a feed


def test_new_and_popular_fixture_becomes_exact_mentions(itch):
    itch.routes[NP] = rss("new_and_popular.xml")
    mentions, report = collector(itch_config([FEED_NP])).run()

    assert [m.source_id for m in mentions] == [
        "bananabros/gorilla-pizza-panic",
        "moonmilk/lethal-lunch-shift",
        "spreadteam/spread",
        "redcircle/red-dot",
        "some_dev/co-op-cavern",  # the jam link at position 5 is skipped but keeps its slot
        "ropeworks/ragdoll-rope-bridge",
        "hauntco/mimic-motel",
    ]
    gorilla_url = "https://bananabros.itch.io/gorilla-pizza-panic"
    assert mentions[0] == Mention(
        source="itch",
        source_id="bananabros/gorilla-pizza-panic",
        url=gorilla_url,
        title="Gorilla Pizza Panic",
        text="Deliver pizzas as a gorilla with up to 4 friends & proximity chat",
        author="bananabros",
        created_at=datetime(2026, 10, 1, 18, 12, tzinfo=UTC),
        engagement=Engagement(),
        links=[gorilla_url],
        media_thumb=thumb("GpZa01"),
        raw_tags=["Action", "windows", "linux"],
        channel="itch:new-and-popular",
        rank=1,
        list_size=8,
        extra={
            "itch_ranks": {"new-and-popular": 1},
            "price": "$4.99",
            "price_value": 4.99,
            "currency": "USD",
            "is_free": False,
            "genre": "Action",
            "updated_at": "2026-10-03T09:01:44+00:00",
            "platforms": ["windows", "linux"],
        },
    )
    expected = {
        # key: (rank, title, created_at, raw_tags, price, currency, is_free)
        "moonmilk/lethal-lunch-shift": (
            2,
            "Lethal Lunch Shift",
            datetime(2026, 10, 2, 7, 30, tzinfo=UTC),
            ["Simulation", "windows", "html"],
            "$0.00",
            "USD",
            True,
        ),
        "spreadteam/spread": (
            3,
            "[Spread]",
            datetime(2026, 9, 30, 12, tzinfo=UTC),
            ["Platformer", "html"],
            "$0.00",
            "USD",
            True,
        ),
        "redcircle/red-dot": (
            4,
            "🔴",
            datetime(2026, 9, 29, 23, 59, 59, tzinfo=UTC),
            [],
            "$0.00",
            "USD",
            True,
        ),
        "some_dev/co-op-cavern": (
            6,
            "Co Op Cavern",
            datetime(2026, 10, 3, 8, 12, tzinfo=UTC),
            ["Action", "windows", "osx", "linux"],
            "3.39€",
            "EUR",
            False,
        ),
        "ropeworks/ragdoll-rope-bridge": (
            7,
            "Ragdoll Rope Bridge",
            datetime(2026, 10, 1, 3, 4, 5, tzinfo=UTC),
            ["Puzzle", "windows"],
            "$2.00",
            "USD",
            False,
        ),
        "hauntco/mimic-motel": (8, "Mimic Motel", NOW, ["Survival", "windows"], "$1.99", "USD", False),
    }
    got = by_key(mentions)
    for key, (rank, title, created, tags, price, currency, free) in expected.items():
        m = got[key]
        assert (m.rank, m.list_size, m.title, m.created_at, m.raw_tags) == (rank, 8, title, created, tags), (
            key
        )
        assert (m.extra["price"], m.extra["currency"], m.extra["is_free"]) == (price, currency, free), key
        assert m.channel == "itch:new-and-popular" and m.author == key.split("/")[0]
        assert m.url == f"https://{key.split('/')[0]}.itch.io/{key.split('/')[1]}"
        assert m.engagement.total == 0 and m.extra["itch_ranks"] == {"new-and-popular": rank}

    assert (
        got["moonmilk/lethal-lunch-shift"].text
        == "Co-op cafeteria horror for 1–4 players. Don't let the soup win."
    )
    assert got["spreadteam/spread"].media_thumb.endswith("/original/Spr3ad.gif")
    assert got["spreadteam/spread"].extra["genre"] == "Platformer"
    assert (
        got["redcircle/red-dot"].extra["genre"] is None and got["redcircle/red-dot"].extra["platforms"] == []
    )
    cavern = got["some_dev/co-op-cavern"]
    assert cavern.extra["price_value"] == pytest.approx(3.39)
    assert cavern.extra["updated_at"] == "2026-10-03T10:01:44+00:00"
    rope = got["ropeworks/ragdoll-rope-bridge"]  # no <imageurl>, no <createDate>
    assert rope.media_thumb == "https://img.itch.zone/aW1nLzEwMDAwMDA3LnBuZw==/315x250%23c/R0pe07.png"
    assert rope.text == "Physics-based co-op bridge building & ragdoll chaos"
    motel = got["hauntco/mimic-motel"]  # unparseable dates -> now / None; empty <android> ignored
    assert motel.extra["updated_at"] is None and motel.extra["platforms"] == ["windows"]

    assert report.ok and report.errors == [] and report.ok_units == 1
    assert report.mentions == 7 and report.requests == 1
    request = itch.requests[0]
    assert request.headers["accept"] == ACCEPT
    assert request.headers["user-agent"] == "GemBot-test/0.1"


def test_multi_feed_dedupe_prefers_popular_feed_and_merges(itch):
    itch.routes[NP] = rss("new_and_popular.xml")
    itch.routes[NEWEST] = rss("newest_page1.xml")
    itch.routes[COOP] = rss("tag_coop.xml")
    sleeps = Sleeps()
    # config order puts the popular feed last; the collector still reads it first
    config = itch_config([FEED_NEWEST, FEED_COOP, FEED_NP])
    mentions, report = collector(config, sleep=sleeps).run()

    assert itch.calls == [NP, NEWEST, COOP]
    assert sleeps == [1.0, 1.0]
    assert len(mentions) == 7 + 36 + 1  # game-03 (newest + tag-co-op) merged, couch-kart only in tag-co-op
    assert len({m.key for m in mentions}) == len(mentions)
    got = by_key(mentions)

    lunch = got["moonmilk/lethal-lunch-shift"]  # popular rank 2 beats tag-co-op rank 1
    assert (lunch.channel, lunch.rank, lunch.list_size) == ("itch:new-and-popular", 2, 8)
    assert lunch.extra["itch_ranks"] == {"new-and-popular": 2, "tag-co-op": 1}
    assert lunch.raw_tags == ["Simulation", "windows", "html", "co-op"]

    gorilla = got["bananabros/gorilla-pizza-panic"]
    assert (gorilla.channel, gorilla.rank) == ("itch:new-and-popular", 1)
    assert gorilla.extra["itch_ranks"] == {"new-and-popular": 1, "tag-co-op": 2}

    game03 = got["newdev03/game-03"]  # unranked in newest, ranked 5 in tag-co-op -> tag rank wins
    assert (game03.channel, game03.rank, game03.list_size) == ("itch:tag-co-op", 5, 5)
    assert game03.extra["itch_ranks"] == {"newest": None, "tag-co-op": 5}
    assert game03.raw_tags == ["Platformer", "html", "co-op"]

    kart = got["tinycrew/couch-kart-chaos"]
    assert (kart.channel, kart.rank, kart.list_size) == ("itch:tag-co-op", 4, 5)
    assert kart.raw_tags == ["Racing", "co-op", "windows"]

    newest_only = got["newdev01/game-01"]
    assert (newest_only.channel, newest_only.rank, newest_only.list_size) == ("itch:newest", None, None)
    assert newest_only.extra["itch_ranks"] == {"newest": None}
    assert report.ok_units == 3 and report.mentions == 44 and report.requests == 3


def test_best_rank_wins_between_non_popular_feeds(itch):
    coop_items = items_of("tag_coop.xml")
    itch.routes[MULTI] = rss(feed_of(list(reversed(coop_items))))
    itch.routes[COOP] = rss("tag_coop.xml")
    config = itch_config([{"name": "tag-multiplayer", "url": MULTI}, FEED_COOP])
    mentions, _ = collector(config).run()
    got = by_key(mentions)
    kart = got["tinycrew/couch-kart-chaos"]  # multiplayer rank 2 vs co-op rank 4
    assert (kart.channel, kart.rank) == ("itch:tag-multiplayer", 2)
    lunch = got["moonmilk/lethal-lunch-shift"]  # multiplayer rank 5 vs co-op rank 1
    assert (lunch.channel, lunch.rank) == ("itch:tag-co-op", 1)
    assert lunch.extra["itch_ranks"] == {"tag-multiplayer": 5, "tag-co-op": 1}
    assert lunch.raw_tags == ["Simulation", "multiplayer", "windows", "html", "co-op"]


def test_unranked_feed_never_replaces_a_ranked_position(itch):
    itch.routes[COOP] = rss("tag_coop.xml")
    itch.routes[NEWEST] = rss(feed_of(items_of("newest_page1.xml")[:5]))
    mentions, _ = collector(itch_config([FEED_COOP, FEED_NEWEST])).run()
    game03 = by_key(mentions)["newdev03/game-03"]
    assert (game03.channel, game03.rank) == ("itch:tag-co-op", 5)
    assert game03.extra["itch_ranks"] == {"tag-co-op": 5, "newest": None}


def test_popular_feed_position_wins_even_when_it_arrives_later():
    c = collector(itch_config([FEED_NP]))
    tag = Mention(source="itch", source_id="a/b", url="u", created_at=NOW, channel="itch:tag-co-op", rank=1)
    c._add(tag)
    c._add(tag.model_copy(update={"channel": "itch:new-and-popular", "rank": 9, "extra": {"itch_ranks": {}}}))
    assert (c.found[0].channel, c.found[0].rank) == ("itch:new-and-popular", 9)


# ---------------------------------------------------------------- Cloudflare / blocking


def test_challenge_uses_fallback_then_later_feeds_go_straight_to_fallback(itch):
    itch.routes[NP] = rss("new_and_popular.xml")
    itch.routes[COOP] = challenge()
    itch.routes[COOP_FB] = rss("tag_coop.xml")
    itch.routes[HORROR_FB] = rss("empty_channel.xml")
    state = State()
    config = itch_config([FEED_NP, FEED_COOP, FEED_HORROR])
    mentions, report = collector(config, state=state).run()

    assert itch.calls == [NP, COOP, COOP_FB, HORROR_FB]  # the horror primary is never challenged
    assert report.errors == [] and report.ok and report.ok_units == 3
    assert any(
        "itch:tag-co-op: Cloudflare challenge (HTTP 403)" in w and f"used fallback {COOP_FB}" in w
        for w in report.warnings
    )
    kart = by_key(mentions)["tinycrew/couch-kart-chaos"]
    assert (kart.channel, kart.rank, kart.extra["itch_fallback"]) == ("itch:tag-co-op", 4, ["tag-co-op"])
    assert by_key(mentions)["moonmilk/lethal-lunch-shift"].extra["itch_fallback"] == ["tag-co-op"]
    assert state.meta.collector_state["itch"]["fallback_until"] == (NOW + timedelta(hours=6)).isoformat()

    # next run inside the hold: straight to the fallbacks, no challenged request at all
    itch.calls.clear()
    _, report = collector(config, state=state).run()
    assert itch.calls == [NP, COOP_FB, HORROR_FB] and report.warnings[-1].startswith("itch:tag-horror")

    # after the hold the primary URLs are tried again and the hold is cleared
    itch.calls.clear()
    itch.routes[COOP] = rss("tag_coop.xml")
    itch.routes[HORROR] = rss("tag_coop.xml")
    later = CollectContext(config=config, http=make_http(), now=NOW + timedelta(hours=7), state=state)
    ItchCollector(later, sleep=Sleeps()).run()
    assert itch.calls == [NP, COOP, HORROR]
    assert "fallback_until" not in state.meta.collector_state["itch"]


def newest_mix() -> bytes:
    """A newest page holding lunch (co-op + horror), gorilla, cavern (co-op slug), kart, game-03."""
    return feed_of(items_of("tag_coop.xml") + items_of("new_and_popular.xml")[2:4])


def test_challenge_falls_back_to_newest_filtered_by_keywords_without_extra_requests(itch):
    itch.routes[NP] = rss("new_and_popular.xml")
    itch.routes[NEWEST] = rss(newest_mix())
    itch.routes[COOP] = challenge()
    config = itch_config([FEED_NP, FEED_NEWEST, COOP_VIA_NEWEST, HORROR_VIA_NEWEST])
    mentions, report = collector(config).run()

    # newest.xml page 1 was already read by the "newest" feed: both fallbacks reuse it
    assert itch.calls == [NP, NEWEST, COOP]
    assert report.errors == [] and report.ok_units == 4
    assert report.warnings == [
        f"itch:tag-co-op: Cloudflare challenge (HTTP 403) for {COOP}; used fallback {NEWEST} (2 game(s))"
    ]
    got = by_key(mentions)
    lunch = got["moonmilk/lethal-lunch-shift"]  # "Co-op cafeteria horror": matches both tags
    assert (lunch.channel, lunch.rank) == ("itch:new-and-popular", 2)
    assert lunch.extra["itch_ranks"] == {
        "new-and-popular": 2,
        "newest": None,
        "tag-co-op": None,
        "tag-horror": None,
    }
    assert lunch.extra["itch_fallback"] == ["tag-co-op", "tag-horror"]
    assert lunch.raw_tags == ["Simulation", "windows", "html", "co-op", "horror"]
    cavern = got["some_dev/co-op-cavern"]  # "Couch co-op cave crawler"
    assert "co-op" in cavern.raw_tags and cavern.extra["itch_ranks"]["tag-co-op"] is None
    kart = got["tinycrew/couch-kart-chaos"]  # no keyword -> only the newest listing
    assert (kart.channel, kart.rank, kart.raw_tags) == ("itch:newest", None, ["Racing", "windows"])
    assert "itch_fallback" not in kart.extra


def test_newest_fallback_filters_every_page_already_read(itch):
    itch.routes[NEWEST] = rss("newest_page1.xml")
    itch.routes[f"{NEWEST}?page=2"] = rss("newest_page2.xml")
    itch.routes[COOP] = challenge()
    puzzle = {**COOP_VIA_NEWEST, "fallback_keywords": ["puzzle"]}  # genre token of every 6th game
    mentions, report = collector(itch_config([{**FEED_NEWEST, "max_pages": 2}, puzzle])).run()
    assert itch.calls == [NEWEST, f"{NEWEST}?page=2", COOP]
    tagged = sorted(m.source_id for m in mentions if "tag-co-op" in m.extra["itch_ranks"])
    assert tagged == [f"newdev{i:02d}/game-{i:02d}" for i in range(2, 73, 6)]  # 12 games over 2 pages
    assert "(12 game(s))" in report.warnings[0]


def test_newest_fallback_is_fetched_when_not_read_yet(itch):
    itch.routes[COOP] = challenge()
    itch.routes[NEWEST] = rss(newest_mix())
    sleeps = Sleeps()
    mentions, _ = collector(itch_config([COOP_VIA_NEWEST]), sleep=sleeps).run()
    assert itch.calls == [COOP, NEWEST] and sleeps == [1.0]
    assert sorted(m.source_id for m in mentions) == ["moonmilk/lethal-lunch-shift", "some_dev/co-op-cavern"]
    assert {(m.channel, m.rank, m.list_size) for m in mentions} == {("itch:tag-co-op", None, None)}


def test_a_failed_page_is_not_requested_again_in_the_same_run(itch):
    itch.routes[NEWEST] = challenge()
    itch.routes[COOP] = challenge()
    mentions, report = collector(itch_config([FEED_NEWEST, COOP_VIA_NEWEST], max_challenges=3)).run()
    assert mentions == [] and itch.calls == [NEWEST, COOP]  # the cached newest failure is reused
    assert report.errors[1] == (
        f"itch:tag-co-op: Cloudflare challenge (HTTP 403) for {COOP}; "
        f"fallback failed too: Cloudflare challenge (HTTP 403) for {NEWEST}"
    )


def test_two_challenges_stop_all_remaining_feeds(itch):
    itch.routes[NP] = challenge()
    itch.routes[NEWEST] = challenge(header=False)  # no cf-mitigated header: body markers only
    config = itch_config([FEED_NP, FEED_NEWEST, FEED_COOP, FEED_HORROR])
    mentions, report = collector(config).run()

    assert mentions == [] and itch.calls == [NP, NEWEST]
    assert report.errors[0] == f"itch:new-and-popular: Cloudflare challenge (HTTP 403) for {NP}"
    assert report.errors[1] == f"itch:newest: Cloudflare challenge (HTTP 403) for {NEWEST}"
    assert report.errors[2] == (
        "itch: blocked by itch.io 2 times (Cloudflare challenge / HTTP 403); "
        "skipped 2 feed(s) this run: tag-co-op, tag-horror"
    )
    assert not report.ok and report.failed_units == 2 and report.ok_units == 0
    assert "FAILED" in report.summary()


def test_primary_and_fallback_both_challenged(itch):
    itch.routes[COOP] = challenge()
    itch.routes[COOP_FB] = challenge()
    mentions, report = collector(itch_config([FEED_COOP, FEED_HORROR])).run()
    assert mentions == [] and itch.calls == [COOP, COOP_FB]
    assert report.errors[0] == (
        f"itch:tag-co-op: Cloudflare challenge (HTTP 403) for {COOP}; "
        f"fallback failed too: Cloudflare challenge (HTTP 403) for {COOP_FB}"
    )
    assert "skipped 1 feed(s) this run: tag-horror" in report.errors[1]


def test_plain_403_is_a_block_and_fallback_is_tried(itch):
    itch.routes[COOP] = OPENRESTY_403
    itch.routes[COOP_FB] = rss("tag_coop.xml")
    mentions, report = collector(itch_config([FEED_COOP])).run()
    assert len(mentions) == 5 and report.errors == []
    assert "blocked without a Cloudflare challenge (HTTP 403)" in report.warnings[0]


def test_503_challenge_is_retried_by_http_client_then_classified(itch):
    itch.routes[NP] = challenge(status=503)
    mentions, report = collector(itch_config([FEED_NP])).run()
    assert mentions == [] and itch.calls == [NP, NP, NP]  # HttpClient retries 5xx before we see it
    assert report.errors == [f"itch:new-and-popular: Cloudflare challenge (HTTP 503) for {NP}"]


def test_503_without_challenge_is_an_error_but_not_a_block(itch):
    itch.routes[NP] = httpx.Response(503, text="Service Unavailable")
    itch.routes[NEWEST] = rss("newest_page3.xml")
    mentions, report = collector(itch_config([FEED_NP, FEED_NEWEST], max_challenges=1)).run()
    assert len(mentions) == 10
    assert report.errors == [f"itch:new-and-popular: HTTP 503 for {NP} after retries"]


# ---------------------------------------------------------------- other HTTP failures


@pytest.mark.parametrize("status", [404, 410])
def test_gone_feed_error_names_the_feed(itch, status):
    itch.routes[COOP] = httpx.Response(status, text="not found")
    itch.routes[NP] = rss("new_and_popular.xml")
    config = itch_config([FEED_NP, {"name": "tag-co-op", "url": COOP}])
    mentions, report = collector(config).run()
    assert len(mentions) == 7
    assert report.errors == [
        f"itch:tag-co-op: HTTP {status} for {COOP}: feed not found, its URL in config/sources.yaml needs updating"
    ]
    assert report.ok  # the popular feed still worked


def test_gone_feed_with_fallback_warns_and_uses_it(itch):
    itch.routes[COOP] = httpx.Response(404)
    itch.routes[COOP_FB] = rss("tag_coop.xml")
    itch.routes[HORROR] = rss("empty_channel.xml")
    mentions, report = collector(itch_config([FEED_COOP, FEED_HORROR])).run()
    assert len(mentions) == 5 and report.errors == []
    assert itch.calls == [COOP, COOP_FB, HORROR]  # a 404 is not a block: no fallback mode
    assert "HTTP 404" in report.warnings[0] and "used fallback" in report.warnings[0]


def test_429_is_retried_by_http_client_then_ok(itch):
    itch.routes[NP] = [httpx.Response(429, headers={"Retry-After": "3"}), rss("new_and_popular.xml")]
    http_sleeps, sleeps = Sleeps(), Sleeps()
    mentions, report = collector(
        itch_config([FEED_NP]), http=make_http(sleep=http_sleeps), sleep=sleeps
    ).run()
    assert len(mentions) == 7 and report.errors == []
    assert http_sleeps == [3.0] and sleeps == [] and report.requests == 2


def test_429_that_cannot_be_waited_out_stops_itch(itch):
    itch.routes[NP] = httpx.Response(429, headers={"Retry-After": "600"})
    mentions, report = collector(itch_config([FEED_NP, FEED_NEWEST])).run()
    assert mentions == [] and itch.calls == [NP]
    assert "rate limited (429)" in report.errors[0]
    assert report.errors[1] == "itch: rate limited by itch.io (HTTP 429); skipped 1 feed(s) this run: newest"


def test_500_is_recorded_and_other_feeds_still_collected(itch):
    itch.routes[NP] = rss("new_and_popular.xml")
    itch.routes[NEWEST] = httpx.Response(500)
    itch.routes[COOP] = rss("tag_coop.xml")
    mentions, report = collector(itch_config([FEED_NP, FEED_NEWEST, FEED_COOP])).run()
    assert itch.calls == [NP, NEWEST, NEWEST, NEWEST, COOP]  # HttpClient: 2 retries on 5xx
    assert len(mentions) == 9  # 7 popular + couch-kart + game-03 from tag-co-op
    assert report.errors == [f"itch:newest: itch: HTTP 500 for {NEWEST}"]
    assert report.ok and report.ok_units == 2 and report.failed_units == 1


def test_malformed_xml_and_empty_channel_do_not_crash(itch):
    itch.routes[NP] = rss("malformed.xml")
    itch.routes[NEWEST] = rss("empty_channel.xml")
    mentions, report = collector(itch_config([FEED_NP, FEED_NEWEST])).run()
    assert mentions == []
    assert len(report.errors) == 1 and report.errors[0].startswith("itch:new-and-popular: malformed XML")
    assert report.warnings == [f"itch:newest: feed returned no items ({NEWEST})"]
    assert report.ok_units == 1 and report.failed_units == 1


def test_html_page_with_200_is_not_parsed(itch):
    itch.routes[NP] = httpx.Response(
        200, text="<html><body>Browse games</body></html>", headers={"content-type": "text/html"}
    )
    _, report = collector(itch_config([FEED_NP])).run()
    assert report.errors == [f"itch:new-and-popular: expected RSS, got text/html from {NP}"]


def test_redirect_inside_itch_warns_so_config_can_be_updated(itch):
    alias = f"{BASE}/new-and-popular/tag-coop.xml"
    itch.routes[alias] = httpx.Response(301, headers={"location": COOP})
    itch.routes[COOP] = rss("tag_coop.xml")
    mentions, report = collector(itch_config([{"name": "tag-coop", "url": alias}])).run()
    assert len(mentions) == 5 and report.errors == []
    assert report.warnings == [f"itch: {alias} redirected to {COOP}; update config/sources.yaml"]


def test_redirect_off_itch_is_refused():
    with respx.mock(assert_all_called=False) as router:
        router.get(NP).mock(
            return_value=httpx.Response(302, headers={"location": "https://evil.example/x.xml"})
        )
        router.get("https://evil.example/x.xml").mock(return_value=rss("new_and_popular.xml"))
        mentions, report = collector(itch_config([FEED_NP])).run()
    assert mentions == []
    assert report.errors == ["itch:new-and-popular: redirected off itch.io to https://evil.example/x.xml"]


# ---------------------------------------------------------------- pagination


def test_pagination_stops_on_a_short_page(itch):
    itch.routes[NEWEST] = rss("newest_page1.xml")
    itch.routes[f"{NEWEST}?page=2"] = rss("newest_page2.xml")
    itch.routes[f"{NEWEST}?page=3"] = rss("newest_page3.xml")
    sleeps = Sleeps()
    mentions, report = collector(itch_config([{**FEED_NEWEST, "max_pages": 5}]), sleep=sleeps).run()
    assert itch.calls == [NEWEST, f"{NEWEST}?page=2", f"{NEWEST}?page=3"]
    assert sleeps == [1.0, 1.0]
    assert len(mentions) == 82 and report.ok_units == 1
    assert {m.rank for m in mentions} == {None} and {m.list_size for m in mentions} == {None}
    assert mentions[0].source_id == "newdev01/game-01" and mentions[-1].source_id == "newdev82/game-82"


def test_pagination_stops_on_a_repeated_page(itch):
    itch.routes[NEWEST] = rss("newest_page1.xml")
    itch.routes[f"{NEWEST}?page=2"] = rss("newest_page1.xml")  # itch recycles pages past the end
    mentions, _ = collector(itch_config([{**FEED_NEWEST, "max_pages": 5}])).run()
    assert itch.calls == [NEWEST, f"{NEWEST}?page=2"] and len(mentions) == 36


def test_pagination_respects_max_pages(itch):
    itch.routes[NEWEST] = rss("newest_page1.xml")
    itch.routes[f"{NEWEST}?page=2"] = rss("newest_page2.xml")
    mentions, _ = collector(itch_config([{**FEED_NEWEST, "max_pages": 2}])).run()
    assert len(itch.calls) == 2 and len(mentions) == 72


def test_pagination_end_of_feed_404_or_block_keeps_page_one(itch):
    itch.routes[NEWEST] = rss("newest_page1.xml")
    itch.routes[f"{NEWEST}?page=2"] = httpx.Response(404)
    mentions, report = collector(itch_config([{**FEED_NEWEST, "max_pages": 3}])).run()
    assert len(mentions) == 36 and report.errors == [] and report.warnings == []

    itch.routes[f"{NEWEST}?page=2"] = challenge()
    mentions, report = collector(itch_config([{**FEED_NEWEST, "max_pages": 3}])).run()
    assert len(mentions) == 36 and report.errors == []
    assert report.warnings == [f"itch:newest: page 2: Cloudflare challenge (HTTP 403) for {NEWEST}?page=2"]


def test_pagination_stops_when_state_already_knows_every_game(itch):
    itch.routes[NEWEST] = rss("newest_page1.xml")
    state = State(seen={f"itch:newdev{i:02d}/game-{i:02d}": NOW for i in range(1, 37)})
    mentions, _ = collector(itch_config([{**FEED_NEWEST, "max_pages": 3}]), state=state).run()
    assert itch.calls == [NEWEST] and len(mentions) == 36  # still reported, just no deeper paging


def test_ranked_feed_pages_offset_the_rank(itch):
    itch.routes[NP] = rss("newest_page1.xml")
    itch.routes[f"{NP}?page=2"] = rss("newest_page2.xml")
    state = State(seen={f"itch:newdev{i:02d}/game-{i:02d}": NOW for i in range(1, 37)})
    mentions, _ = collector(itch_config([{**FEED_NP, "max_pages": 2}]), state=state).run()
    got = by_key(mentions)
    assert (got["newdev01/game-01"].rank, got["newdev01/game-01"].list_size) == (1, 36)
    assert (got["newdev37/game-37"].rank, got["newdev37/game-37"].list_size) == (37, 72)
    assert (got["newdev72/game-72"].rank, got["newdev72/game-72"].list_size) == (72, 72)
    assert got["newdev72/game-72"].extra["itch_ranks"] == {"new-and-popular": 72}


# ---------------------------------------------------------------- budget and pacing


def test_budget_is_enforced_and_partial_results_kept(itch):
    itch.routes[NP] = rss("new_and_popular.xml")
    itch.routes[NEWEST] = rss("newest_page1.xml")
    itch.routes[COOP] = rss("tag_coop.xml")
    sleeps = Sleeps()
    c = collector(itch_config([FEED_NP, FEED_NEWEST, FEED_COOP]), budget=2, sleep=sleeps)
    mentions, report = c.run()
    assert itch.calls == [NP, NEWEST]
    assert len(mentions) == 7 + 36
    assert report.requests == 2 and report.errors == []
    assert report.warnings and "budget of 2 used up" in report.warnings[0]
    assert sleeps == [1.0]  # no pointless sleep before the request the budget refuses


def test_budget_runs_out_while_paging(itch):
    itch.routes[NP] = rss("new_and_popular.xml")
    itch.routes[NEWEST] = rss("newest_page1.xml")
    mentions, report = collector(itch_config([FEED_NP, {**FEED_NEWEST, "max_pages": 3}]), budget=2).run()
    assert len(mentions) == 43 and "budget" in report.warnings[0]


def test_sleep_interval_is_configurable_and_defaults_to_http_sleep(itch):
    itch.routes[NP] = rss("new_and_popular.xml")
    itch.routes[NEWEST] = rss("empty_channel.xml")
    sleeps = Sleeps()
    collector(itch_config([FEED_NP, FEED_NEWEST], request_interval_s=0), sleep=sleeps).run()
    assert sleeps == []
    http_sleeps = Sleeps()
    ctx = CollectContext(
        config=itch_config([FEED_NP, FEED_NEWEST], request_interval_s=2.5),
        http=make_http(sleep=http_sleeps),
        now=NOW,
    )
    ItchCollector(ctx).run()
    assert http_sleeps == [2.5]


def test_invalid_fallback_hold_in_scratch_is_ignored(itch):
    itch.routes[COOP] = rss("tag_coop.xml")
    state = State()
    state.meta.collector_state["itch"] = {"fallback_until": "not-a-date"}
    collector(itch_config([FEED_COOP]), state=state).run()
    assert itch.calls == [COOP] and state.meta.collector_state["itch"] == {}


def test_challenge_without_hold_hours_does_not_persist(itch):
    itch.routes[COOP] = challenge()
    itch.routes[COOP_FB] = rss("tag_coop.xml")
    state = State()
    collector(itch_config([FEED_COOP], fallback_hold_hours=0), state=state).run()
    assert "fallback_until" not in state.meta.collector_state.get("itch", {})


# ---------------------------------------------------------------- pure helpers


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://bananabros.itch.io/gorilla-pizza-panic", ("bananabros", "gorilla-pizza-panic")),
        ("http://Some_Dev.itch.io/Co-Op-Cavern/", ("some_dev", "co-op-cavern")),
        ("  https://dev.itch.io/game?utm=x#top ", ("dev", "game")),
        ("https://itch.io/jam/friendslop-jam-2026", None),
        ("https://itch.io/games/tag-co-op", None),
        ("https://dev.itch.io/", None),
        ("https://dev.itch.io/game/devlog/123", None),
        ("https://www.itch.io/game", None),
        ("https://a.b.itch.io/game", None),
        ("https://dev.itch.io.evil.com/game", None),
        ("ftp://dev.itch.io/game", None),
        ("https://dev.itch.io/g%20ame", None),
        ("https://[::1/x", None),
        ("", None),
    ],
)
def test_canonical_game(url, expected):
    assert canonical_game(url) == expected


@pytest.mark.parametrize(
    ("title", "plain", "expected"),
    [
        ("Gorilla Pizza Panic [$4.99] [Action]", "Gorilla Pizza Panic", ("$4.99", "Action")),
        ("[Spread] [Free] [Platformer]", "[Spread]", ("Free", "Platformer")),
        ("[Spread] [Free] [Platformer]", None, ("Free", "Platformer")),
        ("🔴 [Free]", "🔴", ("Free", None)),
        ("Co-op Cavern [3.39€] [Action]", None, ("3.39€", "Action")),
        ("Pay [$1] To Win [US$4.99] [Card Game]", None, ("US$4.99", "Card Game")),
        ("Thing [Top 10] [Puzzle]", "Thing", (None, "Puzzle")),
        ("Thing [Top 10]", None, (None, None)),
        ("Mismatch [$2.00] [Racing]", "Other Name", ("$2.00", "Racing")),
        ("No decoration", "No decoration", (None, None)),
    ],
)
def test_title_tokens(title, plain, expected):
    assert title_tokens(title, plain) == expected


@pytest.mark.parametrize("token", ["Free", "free", "$4.99", "3.39€", "3,99 £", "US$4.99", "CHF 5.00", "¥500"])
def test_price_labels(token):
    assert is_price_label(token)


@pytest.mark.parametrize("token", ["Action", "Top 10", "1-4 Players", "Role Playing", ""])
def test_not_price_labels(token):
    assert not is_price_label(token)


def test_parse_price_and_dates():
    assert parse_price("$9.99") == pytest.approx(9.99)
    assert parse_price("3,39€") == pytest.approx(3.39)
    assert parse_price("free") is None and parse_price(None) is None
    assert parse_date("Fri, 11 Dec 2020 02:30:01 GMT") == datetime(2020, 12, 11, 2, 30, 1, tzinfo=UTC)
    assert parse_date("Fri, 11 Dec 2020 04:30:01 +0200") == datetime(2020, 12, 11, 2, 30, 1, tzinfo=UTC)
    assert parse_date("nonsense") is None and parse_date("") is None


def test_short_text_strips_markup_and_truncates():
    html_text = 'Tom &amp; <i>Jerry</i>&#39;s<br/>\n<img src="x.png" alt="a"/>  &lt;3'
    assert short_text(html_text) == "Tom & Jerry 's <3"
    long = short_text("word " * 300)
    assert len(long) == itch_mod.MAX_TEXT_CHARS and long.endswith("…")


def test_parse_feed_guards():
    with pytest.raises(ItchFeedError, match="DOCTYPE"):
        parse_feed(
            b'<?xml version="1.0"?><!DOCTYPE lol [<!ENTITY a "aaaa">]><rss><channel>&a;</channel></rss>',
            now=NOW,
        )
    with pytest.raises(ItchFeedError, match="not an RSS document"):
        parse_feed(b"<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'/>", now=NOW)
    with pytest.raises(ItchFeedError, match="no <channel>"):
        parse_feed(b"<rss version='2.0'></rss>", now=NOW)
    with pytest.raises(ItchFeedError, match="too large"):
        parse_feed(b" " * (itch_mod.MAX_FEED_BYTES + 1), now=NOW)


def test_parse_feed_caps_items_and_skips_in_page_duplicates(monkeypatch):
    items = items_of("newest_page1.xml")
    page = parse_feed(feed_of([items[0], items[0], *items[1:]]), now=NOW)
    assert page.size == 37 and len(page.items) == 36 and page.title == "t"
    assert page.items[1].position == 3  # the duplicate keeps the first position
    monkeypatch.setattr(itch_mod, "MAX_ITEMS_PER_PAGE", 3)
    page = parse_feed(feed_of(items), now=NOW)
    assert page.size == 36 and len(page.items) == 3


def test_parse_feed_tolerates_odd_items():
    odd = (
        b"<item><guid>https://dev.itch.io/odd-one</guid><title>Odd One [Free] [Other]</title>"
        b"<plainTitle>  Odd   &amp;amp; One </plainTitle><imageurl>not a url</imageurl>"
        b"<description><![CDATA[text <img src='//img.itch.zone/a.png'>]]></description></item>"
        b"<item><link>https://dev.itch.io/bare</link><price>$5</price></item>"
    )
    page = parse_feed(feed_of([odd]), now=NOW)
    odd_item, bare = page.items
    assert odd_item.name == "Odd & One" and odd_item.image == "https://img.itch.zone/a.png"
    assert odd_item.is_free and odd_item.price is None and odd_item.currency is None
    assert odd_item.created_at == NOW and odd_item.platforms == []
    assert bare.name == "Bare" and bare.text == "" and bare.image is None
    assert (bare.price, bare.price_value, bare.is_free, bare.genre) == ("$5", 5.0, False, None)


def test_price_from_title_when_price_element_missing():
    item = (
        b"<item><link>https://dev.itch.io/x</link><title>X [$3.00] [Action]</title>"
        b"<plainTitle>X</plainTitle></item>"
        b"<item><link>https://dev.itch.io/y</link><title>Y</title><plainTitle>Y</plainTitle></item>"
    )
    x, y = parse_feed(feed_of([item]), now=NOW).items
    assert (x.price, x.price_value, x.is_free) == ("$3.00", 3.0, False)
    assert (y.price, y.is_free) == (None, None)


def test_classify_response_and_sniffing():
    assert looks_like_rss(b"\xef\xbb\xbf\n  <?xml version='1.0'?><rss/>")
    assert looks_like_rss(b"<rss version='2.0'><channel/></rss>")
    assert not looks_like_rss(b"<!DOCTYPE html><html></html>")
    page = fx("cloudflare_challenge.html")
    assert classify_response(httpx.Response(200, content=page)) == "challenge"
    assert classify_response(httpx.Response(403, content=page)) == "challenge"
    assert classify_response(httpx.Response(403, text="nope")) == "blocked"
    assert classify_response(httpx.Response(410)) == "gone"
    assert classify_response(httpx.Response(503, text="down")) == "unavailable"
    assert classify_response(httpx.Response(200, text="")) == "not_rss"
    # an RSS feed whose game text happens to say "Just a moment" is still RSS
    feed = feed_of([b"<item><description>Just a moment...</description></item>"])
    assert classify_response(httpx.Response(200, content=feed)) == "rss"


def test_keyword_matching_is_whole_word_and_covers_slug_and_genre():
    page = parse_feed(fx("new_and_popular.xml"), now=NOW)
    items = {item.slug: item for item in page.items}
    coop = keyword_pattern(["Co-op", " ", "coop"])
    assert item_matches(items["lethal-lunch-shift"], coop)  # description
    assert item_matches(items["co-op-cavern"], coop)  # description and slug
    assert not item_matches(items["gorilla-pizza-panic"], coop)
    assert item_matches(items["spread"], keyword_pattern(["platformer"]))  # genre token
    assert not item_matches(items["red-dot"], keyword_pattern(["dot com"]))
    assert not item_matches(items["mimic-motel"], keyword_pattern(["friend"]))  # "friends" is another word
    assert keyword_pattern([]) is None and keyword_pattern(["  "]) is None
    assert item_matches(items["red-dot"], None)


def test_feed_tags():
    assert feed_tags(COOP) == ["co-op"]
    assert feed_tags(f"{BASE}/newest/tag-horror/tag-physics.xml?page=2") == ["horror", "physics"]
    assert feed_tags(NP) == []
