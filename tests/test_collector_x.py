"""X (Twitter) collector: recent search, since_id, monthly cost guard, error mapping."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from gembot.collectors.base import CollectContext
from gembot.collectors.x import (
    MAX_QUERY_CHARS,
    TWEET_FIELDS,
    XCollector,
    post_to_mention,
    query_key,
)
from gembot.config import Config
from gembot.http import Budget
from gembot.models import Engagement, Mention, State
from tests.factories import NOW, make_config, make_http, read_fixture

TOKEN = "AAAAtest-bearer-token-never-log-me"
SEARCH = "https://api.x.com/2/tweets/search/recent"
QUERY = '("proximity chat" OR friendslop) (game OR steam) -is:retweet lang:en'
RESET_EPOCH = int(datetime(2026, 10, 3, 12, 14, tzinfo=UTC).timestamp())


def body(name: str) -> dict[str, Any]:
    return json.loads(read_fixture("x", name))


def reply(name: str, status: int = 200, **kwargs: Any) -> httpx.Response:
    return httpx.Response(status, json=body(name), **kwargs)


def x_config(token: str | None = TOKEN, **x: Any) -> Config:
    config = make_config({"X_BEARER_TOKEN": token} if token else {})
    x.setdefault("queries", [QUERY])
    sources = config.sources.model_copy(update={"x": config.sources.x.model_copy(update=x)})
    return config.model_copy(update={"sources": sources})


def make_collector(
    config: Config | None = None, *, state: State | None = None, budget: Budget | None = None, **x: Any
) -> XCollector:
    ctx = CollectContext(
        config=config or x_config(**x),
        http=make_http(),
        now=NOW,
        state=state if state is not None else State(),
    )
    return XCollector(ctx, budget=budget)


def params(call: Any) -> dict[str, str]:
    return dict(call.request.url.params)


def scratch(collector: XCollector) -> dict[str, Any]:
    return collector.ctx.state.meta.collector_state["x"]


# ---------------------------------------------------------------- config / enabled


def test_default_config_has_cost_guards():
    x = make_config().sources.x
    assert x.enabled and len(x.queries) == 1 and len(x.queries[0]) <= MAX_QUERY_CHARS
    assert x.max_results == 25 and x.sort_order == "recency"
    assert x.expand_authors is False and x.monthly_read_budget == 3000


@respx.mock
def test_disabled_without_token_makes_no_request():
    mentions, report = make_collector(x_config(token=None)).run()
    assert mentions == [] and report.skipped and report.ok
    assert report.skip_reason == "X_BEARER_TOKEN not set (the X API is paid; see README)"
    assert len(respx.calls) == 0 and report.requests == 0


@respx.mock
@pytest.mark.parametrize(
    ("overrides", "reason"),
    [({"enabled": False}, "x.enabled: false"), ({"queries": []}, "x.queries")],
)
def test_disabled_by_config(overrides, reason):
    mentions, report = make_collector(**overrides).run()
    assert mentions == [] and report.skipped and reason in report.skip_reason
    assert len(respx.calls) == 0


@respx.mock
def test_disabled_when_monthly_budget_used_up():
    state = State()
    state.meta.collector_state["x"] = {"reads": {"2026-10": 3000}}
    mentions, report = make_collector(state=state).run()
    assert mentions == [] and report.skipped
    assert "monthly X read budget used up (3000/3000 posts+users in 2026-10)" in report.skip_reason
    assert len(respx.calls) == 0
    # last month's count does not block this month
    state.meta.collector_state["x"] = {"reads": {"2026-09": 9999}}
    assert make_collector(state=state).enabled() == (True, None)
    # no cap at all
    state.meta.collector_state["x"] = {"reads": {"2026-10": 10**6}}
    assert make_collector(state=state, monthly_read_budget=None).enabled() == (True, None)


# ---------------------------------------------------------------- request shape


@respx.mock
def test_first_request_params_use_start_time_and_never_follow_next_token():
    route = respx.get(SEARCH).mock(return_value=reply("search_two_posts.json"))
    collector = make_collector()
    collector.run()
    assert route.call_count == 1  # meta.next_token present, but never followed
    request = route.calls.last.request
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert "query=%28%22proximity+chat%22+OR+friendslop%29" in str(request.url)
    assert params(route.calls.last) == {
        "query": QUERY,
        "max_results": "25",
        "sort_order": "recency",
        "tweet.fields": TWEET_FIELDS,
        "start_time": "2026-10-02T12:00:00Z",
    }
    assert "next_token" not in str(request.url) and "pagination_token" not in str(request.url)


@respx.mock
def test_expansions_only_when_expand_authors():
    route = respx.get(SEARCH).mock(return_value=reply("search_empty.json"))
    make_collector(expand_authors=True, sort_order="relevancy").run()
    sent = params(route.calls.last)
    assert sent["expansions"] == "author_id"
    assert sent["user.fields"] == "username,name,public_metrics"
    assert sent["sort_order"] == "relevancy"


@respx.mock
def test_since_id_replaces_start_time():
    route = respx.get(SEARCH).mock(return_value=reply("search_empty.json"))
    state = State()
    state.meta.collector_state["x"] = {"since_id": {query_key(QUERY): "2106300000000000000"}}
    make_collector(state=state).run()
    sent = params(route.calls.last)
    assert sent["since_id"] == "2106300000000000000"
    assert "start_time" not in sent


@pytest.mark.parametrize(("configured", "sent"), [(1, 10), (10, 10), (25, 25), (100, 100), (500, 100)])
@pytest.mark.parametrize("monthly", [3000, None])
def test_max_results_is_clamped(configured, sent, monthly):
    collector = make_collector(max_results=configured, monthly_read_budget=monthly)
    assert collector.search_params(QUERY)["max_results"] == sent


def test_max_results_shrinks_to_what_is_left_of_the_month():
    state = State()
    state.meta.collector_state["x"] = {"reads": {"2026-10": 2985}}
    assert make_collector(state=state).max_results() == 15
    state.meta.collector_state["x"] = {"reads": {"2026-10": 2998}}
    assert make_collector(state=state).max_results() == 10  # the API minimum


# ---------------------------------------------------------------- parsing


@respx.mock
def test_parses_two_posts_into_exact_mentions():
    respx.get(SEARCH).mock(return_value=reply("search_two_posts.json"))
    mentions, report = make_collector(expand_authors=True).run()
    long_text = body("search_two_posts.json")["data"][0]["note_tweet"]["text"]
    assert mentions == [
        Mention(
            source="x",
            source_id="2106339003595999950",
            url="https://x.com/pixel_pals_studio/status/2106339003595999950",
            title=(
                "Two years of nights and weekends later: STACK OVERFLOWERS is a 2-4 player physics co-op "
                "about stacking a greenhouse tow…"
            ),
            text=long_text,
            author="pixel_pals_studio",
            author_audience=2350,
            created_at=datetime(2026, 10, 3, 11, 2, 7, tzinfo=UTC),
            engagement=Engagement(likes=612, comments=37, shares=41 + 8),
            links=[
                "https://store.steampowered.com/app/3567890/Stack_Overflowers/",  # unwound_url wins
                "https://youtu.be/abc123XYZ00",
                "https://discord.gg/stackoverflowers",  # only in note_tweet.entities
            ],
            raw_tags=["indiedev", "coop", "physicsgame"],
            channel="x",
            extra={"impressions": 30215, "conversation_id": "2106339003595999950"},
        ),
        Mention(
            source="x",
            source_id="2106327322458248839",
            url="https://x.com/crumbgames_dev/status/2106327322458248839",
            title="Our proximity chat co-op horror game BACKROOM BAKERS just got its Steam page!",
            text=body("search_two_posts.json")["data"][1]["text"],
            author="crumbgames_dev",
            author_audience=812,
            created_at=datetime(2026, 10, 3, 10, 15, 42, tzinfo=UTC),
            engagement=Engagement(likes=154, comments=9, shares=15),
            links=["https://store.steampowered.com/app/3456780/Backroom_Bakers/"],
            raw_tags=["indiedev", "screenshotsaturday"],
            channel="x",
            extra={"impressions": 8410, "conversation_id": "2106327322458248839"},
        ),
    ]
    assert len(mentions[0].title) == 120
    assert report.ok and report.ok_units == 1 and report.requests == 1 and report.mentions == 2
    assert not report.errors and not report.warnings


@respx.mock
def test_missing_author_falls_back_to_i_web_url_and_author_id():
    respx.get(SEARCH).mock(return_value=reply("search_missing_author.json"))
    collector = make_collector(expand_authors=True)
    mentions, report = collector.run()
    known, missing = mentions
    assert known.url == "https://x.com/goblin_grill_game/status/2106318669611319061"
    assert known.author == "goblin_grill_game" and known.author_audience == 64
    assert known.links == ["https://store.steampowered.com/app/3678901/Goblin_Grill/"]
    assert missing.url == "https://x.com/i/web/status/2106296204917860494"
    assert missing.author == "1700000000000000099" and missing.author_audience is None
    assert missing.engagement == Engagement(likes=11, comments=4, shares=1)
    assert missing.raw_tags == []
    # the errors[] array next to data is a warning, not a failure
    assert report.ok and not report.errors
    assert report.warnings == [
        f'query "{QUERY[:39]}…": X returned 1 partial error(s), e.g. '
        "Could not find user with author_id: [1700000000000000099]."
    ]
    assert collector.monthly_reads() == 2 + 1  # posts + expanded users


@respx.mock
def test_post_vocabulary_and_sparse_posts():
    respx.get(SEARCH).mock(return_value=reply("search_post_vocabulary.json"))
    mentions, report = make_collector().run()
    full, sparse = mentions
    assert full.engagement == Engagement(likes=48, comments=6, shares=5 + 2)  # repost_count
    assert full.author == "toolbox_towers" and full.author_audience == 431
    assert full.links == [
        "https://store.steampowered.com/app/3789012/Toolbox_Towers/",
        "https://t.co/Dv1Og22222",  # no expanded_url: keep t.co, the resolver expands shorteners
    ]
    # no created_at -> time decoded from the snowflake id; no author/metrics/entities at all
    assert sparse.created_at == datetime(2026, 10, 2, 7, 0, tzinfo=UTC)
    assert sparse.author is None and sparse.url == "https://x.com/i/web/status/2105915685074290716"
    assert sparse.engagement == Engagement() and sparse.links == [] and sparse.raw_tags == []
    assert sparse.extra == {"conversation_id": "2105900000000000000"}
    assert sparse.title == "#Friendslop is a genre now and I will die on this hill"
    assert report.ok and not report.warnings


def test_post_to_mention_edge_cases():
    assert post_to_mention({"text": "no id"}) is None
    assert post_to_mention({"id": "not-a-snowflake", "text": "no time"}) is None
    odd = post_to_mention(
        {
            "id": "2106327322458248839",
            "created_at": "garbage",
            "text": "\n\n  first real line  \nsecond",
            "public_metrics": {
                "like_count": "7",
                "retweet_count": None,
                "quote_count": True,
                "reply_count": "lots",
            },
            "entities": {
                "urls": [
                    "not-a-dict",
                    {"url": ""},
                    {"expanded_url": "https://mobile.twitter.com/a/status/1"},
                    {"expanded_url": "https://store.steampowered.com/app/1/"},
                    {"expanded_url": "https://store.steampowered.com/app/1/"},
                ],
                "hashtags": [{"tag": "Coop"}, {"tag": "coop"}, "bad", {"tag": ""}],
            },
            "note_tweet": "not-a-dict",
        },
        {"1": {"username": "ignored"}},
    )
    assert odd is not None
    assert odd.created_at == datetime(2026, 10, 3, 10, 15, 42, tzinfo=UTC)  # from the snowflake id
    assert odd.title == "first real line"
    assert odd.engagement == Engagement(likes=7)
    assert odd.links == ["https://store.steampowered.com/app/1/"]
    assert odd.raw_tags == ["coop"]


@respx.mock
def test_unparseable_posts_are_skipped_and_since_id_still_saved():
    payload = body("search_two_posts.json")
    payload["data"].insert(0, "not-a-post")
    payload["data"].insert(
        1, {"id": "2106339003595999951", "created_at": "2026-10-03T11:00:00Z", "author_id": 5}
    )
    respx.get(SEARCH).mock(return_value=httpx.Response(200, json=payload))
    collector = make_collector()
    mentions, report = collector.run()
    assert len(mentions) == 3  # the dict with an int author_id still parses
    assert report.warnings == [f'query "{QUERY[:39]}…": skipped 1 unparseable post(s)']
    assert scratch(collector)["since_id"][query_key(QUERY)] == "2106339003595999950"


@respx.mock
def test_a_post_that_crashes_the_parser_is_skipped(monkeypatch):
    import gembot.collectors.x as xmod

    def boom(post, users):
        raise RuntimeError("parser bug")

    monkeypatch.setattr(xmod, "post_to_mention", boom)
    respx.get(SEARCH).mock(return_value=reply("search_two_posts.json"))
    collector = make_collector()
    mentions, report = collector.run()
    assert mentions == [] and "skipped 2 unparseable post(s)" in report.warnings[0]
    assert scratch(collector)["since_id"][query_key(QUERY)] == "2106339003595999950"


@respx.mock
def test_invalid_json_and_wrong_shape_are_recorded():
    respx.get(SEARCH).mock(
        side_effect=[httpx.Response(200, text="<html>oops</html>"), httpx.Response(200, json=[1, 2])]
    )
    mentions, report = make_collector(queries=[QUERY, "friendslop"]).run()
    assert mentions == [] and len(report.errors) == 2 and not report.ok
    assert "JSONDecodeError" in report.errors[0]
    assert "unexpected X response (list" in report.errors[1]


# ---------------------------------------------------------------- since_id + monthly counter


@respx.mock
def test_since_id_saved_and_reads_counted():
    respx.get(SEARCH).mock(return_value=reply("search_two_posts.json"))
    state = State()
    state.meta.collector_state["x"] = {"since_id": {"stale-query-key": "1"}, "reads": {"2026-09": 500}}
    collector = make_collector(state=state, expand_authors=True)
    collector.run()
    assert state.meta.collector_state["x"] == {
        "since_id": {query_key(QUERY): "2106339003595999950"},  # removed queries are pruned
        "reads": {"2026-10": 2 + 2},  # 2 posts + 2 expanded users; last month dropped
    }


@respx.mock
def test_empty_result_keeps_since_id():
    route = respx.get(SEARCH).mock(return_value=reply("search_empty.json"))
    state = State()
    state.meta.collector_state["x"] = {"since_id": {query_key(QUERY): "2106300000000000000"}}
    mentions, report = make_collector(state=state).run()
    assert mentions == [] and report.ok and report.ok_units == 1
    assert scratch_state(state)["since_id"] == {query_key(QUERY): "2106300000000000000"}
    assert scratch_state(state)["reads"] == {"2026-10": 0}
    # and with no since_id yet, an empty result does not invent one
    route.reset()
    state = State()
    make_collector(state=state).run()
    assert scratch_state(state)["since_id"] == {}


def scratch_state(state: State) -> dict[str, Any]:
    return state.meta.collector_state["x"]


@respx.mock
def test_monthly_counter_accumulates_and_then_disables_the_collector():
    respx.get(SEARCH).mock(return_value=reply("search_two_posts.json"))
    state = State()
    state.meta.collector_state["x"] = {"reads": {"2026-10": 2990}}
    first = make_collector(state=state)
    assert first.search_params(QUERY)["max_results"] == 10
    mentions, report = first.run()
    # 2 posts + the 2 users in includes: whatever X returns is billed, so it is counted
    assert len(mentions) == 2 and scratch_state(state)["reads"] == {"2026-10": 2990 + 4}
    state.meta.collector_state["x"]["reads"] = {"2026-10": 2999}
    make_collector(state=state).run()  # 2 posts + 2 users more -> over the cap
    assert scratch_state(state)["reads"] == {"2026-10": 3003}
    mentions, report = make_collector(state=state).run()
    assert mentions == [] and report.skipped and "3003/3000" in report.skip_reason
    assert len(respx.calls) == 2


@respx.mock
def test_monthly_budget_reached_mid_run_skips_remaining_queries():
    route = respx.get(SEARCH).mock(return_value=reply("search_two_posts.json"))
    state = State()
    state.meta.collector_state["x"] = {"reads": {"2026-10": 2999}}
    mentions, report = make_collector(state=state, queries=[QUERY, "friendslop", "proximity chat"]).run()
    assert route.call_count == 1 and len(mentions) == 2
    assert report.warnings == [
        "monthly X read budget used up (3003/3000 posts+users in 2026-10); raise x.monthly_read_budget "
        "in config/sources.yaml or wait for next month; remaining queries skipped"
    ]


# ---------------------------------------------------------------- errors


@respx.mock
def test_stale_since_id_400_retries_once_with_start_time():
    route = respx.get(SEARCH).mock(
        side_effect=[reply("error_400_since_id.json", 400), reply("search_two_posts.json")]
    )
    state = State()
    state.meta.collector_state["x"] = {"since_id": {query_key(QUERY): "2101582130184379605"}}
    mentions, report = make_collector(state=state).run()
    assert route.call_count == 2
    first, second = (params(call) for call in route.calls)
    assert first["since_id"] == "2101582130184379605" and "start_time" not in first
    assert second["start_time"] == "2026-10-02T12:00:00Z" and "since_id" not in second
    assert len(mentions) == 2 and report.ok and not report.errors
    assert "retried once with start_time" in report.warnings[0]
    assert scratch_state(state)["since_id"] == {query_key(QUERY): "2106339003595999950"}


@respx.mock
def test_stale_since_id_retry_happens_only_once():
    route = respx.get(SEARCH).mock(return_value=reply("error_400_since_id.json", 400))
    state = State()
    state.meta.collector_state["x"] = {"since_id": {query_key(QUERY): "2101582130184379605"}}
    mentions, report = make_collector(state=state).run()
    assert route.call_count == 2 and mentions == []
    assert "X refused the query (HTTP 400): Invalid 'since_id'" in report.errors[0]
    assert scratch_state(state)["since_id"] == {}


@respx.mock
def test_400_blaming_the_query_is_not_retried_and_next_query_runs():
    route = respx.get(SEARCH).mock(side_effect=[reply("error_400.json", 400), reply("search_two_posts.json")])
    state = State()
    state.meta.collector_state["x"] = {"since_id": {query_key("bad and query"): "2106300000000000000"}}
    mentions, report = make_collector(state=state, queries=["bad and query", QUERY]).run()
    assert route.call_count == 2 and len(mentions) == 2
    assert params(route.calls[0])["query"] == "bad and query"
    assert report.errors == [
        'query "bad and query": X refused the query (HTTP 400): There were errors processing your request: '
        'Ambiguous use of and as a keyword. Use a space to logically join two clauses, or "AND" to treat as a '
        "keyword. (at position 17), Missing closing parenthesis (at position 30)"
    ]
    assert report.ok  # one of two queries worked
    assert scratch_state(state)["since_id"][query_key("bad and query")] == "2106300000000000000"


@respx.mock
@pytest.mark.parametrize(
    ("fixture", "status", "message"),
    [
        ("error_401.json", 401, "X rejected X_BEARER_TOKEN (HTTP 401)"),
        (
            "error_402.json",
            402,
            "X API credits used up — buy credits / raise the spending limit in the X Developer Console",
        ),
        ("error_403.json", 403, "put the app in the Pay-per-use package, Production environment"),
    ],
)
def test_account_errors_are_clear_and_stop_the_run(fixture, status, message, caplog):
    route = respx.get(SEARCH).mock(return_value=reply(fixture, status))
    caplog.set_level(logging.DEBUG)
    mentions, report = make_collector(queries=[QUERY, "friendslop"]).run()
    assert mentions == [] and not report.ok and report.failed_units == 1
    assert route.call_count == 1  # the other query is not even tried
    assert len(report.errors) == 1 and message in report.errors[0]
    assert TOKEN not in json.dumps([report.errors, report.warnings]) and TOKEN not in caplog.text


@respx.mock
def test_other_403_reasons_are_reported_with_the_reason():
    payload = {"title": "Forbidden", "reason": "official-client-forbidden", "detail": "Not allowed."}
    respx.get(SEARCH).mock(return_value=httpx.Response(403, json=payload))
    _, report = make_collector().run()
    assert "X refused the request (HTTP 403 official-client-forbidden): Not allowed." in report.errors[0]
    respx.get(SEARCH).mock(return_value=httpx.Response(403, text="nope"))
    _, report = make_collector().run()
    assert "X refused the request (HTTP 403 forbidden): no details" in report.errors[0]


@respx.mock
def test_429_is_not_retried_and_reports_the_reset_time():
    headers = {
        "x-rate-limit-limit": "450",
        "x-rate-limit-remaining": "0",
        "x-rate-limit-reset": str(RESET_EPOCH),
    }
    route = respx.get(SEARCH).mock(return_value=reply("error_429.json", 429, headers=headers))
    collector = make_collector(queries=[QUERY, "friendslop"])
    mentions, report = collector.run()
    assert mentions == [] and route.call_count == 1 and report.requests == 1
    assert report.errors == [
        f'query "{QUERY[:39]}…": X rate limit reached (HTTP 429); skipped this run, '
        "the limit resets at 2026-10-03 12:14 UTC (in 14 min)"
    ]
    assert collector.http.client.event_hooks["response"] == []  # the header hook is removed again


@respx.mock
def test_429_without_reset_header():
    respx.get(SEARCH).mock(return_value=reply("error_429.json", 429))
    _, report = make_collector().run()
    assert report.errors[0].endswith("X rate limit reached (HTTP 429); skipped this run, reset time unknown")


@respx.mock
def test_5xx_is_reported_without_retry_and_next_query_runs():
    route = respx.get(SEARCH).mock(side_effect=[httpx.Response(503), reply("search_empty.json")])
    mentions, report = make_collector(queries=[QUERY, "friendslop"]).run()
    assert route.call_count == 2 and mentions == []
    assert report.errors == [f'query "{QUERY[:39]}…": X API server error (HTTP 503); skipped this run']
    assert report.ok and report.ok_units == 1


@respx.mock
def test_other_http_errors_and_transport_errors_are_recorded():
    respx.get(SEARCH).mock(side_effect=[httpx.Response(404), httpx.ConnectError("no route")])
    mentions, report = make_collector(queries=[QUERY, "friendslop"]).run()
    assert mentions == [] and len(report.errors) == 2 and not report.ok
    assert "HTTP 404" in report.errors[0] and "ConnectError" in report.errors[1]


@respx.mock
def test_query_over_512_chars_is_rejected_without_a_request():
    route = respx.get(SEARCH).mock(return_value=reply("search_empty.json"))
    too_long = "friendslop " * 50  # 550 chars
    mentions, report = make_collector(queries=[too_long, "   ", QUERY]).run()
    assert mentions == []
    assert route.call_count == 1 and params(route.calls.last)["query"] == QUERY
    assert len(report.errors) == 1
    assert "query is 550 characters but X allows at most 512" in report.errors[0]
    assert report.failed_units == 1 and report.ok_units == 1


@respx.mock
def test_request_budget_is_enforced():
    route = respx.get(SEARCH).mock(return_value=reply("search_empty.json"))
    queries = [QUERY, "friendslop", "proximity chat"]
    _, report = make_collector(queries=queries, budget=Budget("x", 2)).run()
    assert route.call_count == 2 and report.requests == 2
    assert report.warnings and "budget of 2 used up" in report.warnings[0]


@respx.mock
def test_run_without_state_uses_throwaway_scratch_and_custom_api_base():
    route = respx.get("https://x.example/2/tweets/search/recent").mock(
        return_value=reply("search_two_posts.json")
    )
    ctx = CollectContext(config=x_config(api_base="https://x.example/2/"), http=make_http(), now=NOW)
    collector = XCollector(ctx)
    mentions, _ = collector.run()
    assert route.called and len(mentions) == 2
    assert collector.scratch()["since_id"] == {query_key(QUERY): "2106339003595999950"}
