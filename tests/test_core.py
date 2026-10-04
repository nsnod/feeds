"""Tests for the frozen core: config loading, models, HTTP client, collector base."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from gembot.collectors.base import CollectContext, Collector
from gembot.config import ConfigError, Secrets, Settings, load_config
from gembot.http import Budget, BudgetExceeded, HttpError, RateLimited
from gembot.models import Engagement, Game, Mention, State, ensure_utc, platform_label
from tests.factories import NOW, ROOT, make_config, make_http, make_mention

# ---------------------------------------------------------------- config


def test_default_config_loads_and_weights_are_complete():
    config = load_config(ROOT / "config", env={})
    assert config.settings.weights == pytest.approx(
        {
            "velocity": 0.25,
            "underdog": 0.15,
            "cross": 0.15,
            "fit": 0.15,
            "hype": 0.15,
            "meme": 0.05,
            "fresh": 0.10,
        }
    )
    assert config.settings.decisions.alarm_score == 72
    assert "IndieDev" in config.sources.reddit.subreddits
    assert config.feeds.feeds == []
    assert config.blocklist.companies


def test_missing_config_dir_uses_defaults(tmp_path):
    config = load_config(tmp_path, env={})
    assert config.settings == Settings()
    assert config.sources.reddit.subreddits == []


def test_invalid_yaml_and_unknown_keys_raise(tmp_path):
    (tmp_path / "settings.yaml").write_text("weights: [1, 2\n")
    with pytest.raises(ConfigError, match=r"settings\.yaml"):
        load_config(tmp_path, env={})
    (tmp_path / "settings.yaml").write_text("decisions:\n  alarm_scor: 70\n")
    with pytest.raises(ConfigError, match="alarm_scor"):
        load_config(tmp_path, env={})
    (tmp_path / "settings.yaml").write_text("- just\n- a list\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(tmp_path, env={})


def test_weights_validation(tmp_path):
    (tmp_path / "settings.yaml").write_text("weights: {velocity: 1}\n")
    with pytest.raises(ConfigError, match="missing weight"):
        load_config(tmp_path, env={})
    (tmp_path / "settings.yaml").write_text(
        "weights: {velocity: 1, underdog: 0, cross: 0, fit: 0, hype: 0, meme: 0, fresh: 0, vibes: 1}\n"
    )
    with pytest.raises(ConfigError, match="unknown weight"):
        load_config(tmp_path, env={})
    (tmp_path / "settings.yaml").write_text(
        "weights: {velocity: 0, underdog: 0, cross: 0, fit: 0, hype: 0, meme: 0, fresh: 0}\n"
    )
    with pytest.raises(ConfigError, match="non-negative"):
        load_config(tmp_path, env={})


def test_empty_feeds_and_blocklist_files(tmp_path):
    (tmp_path / "feeds.yaml").write_text("feeds:\n")
    (tmp_path / "blocklist.yaml").write_text("companies:\nkeywords:\n")
    config = load_config(tmp_path, env={})
    assert config.feeds.feeds == []
    assert config.blocklist.companies == []


def test_feed_source_is_lowercased(tmp_path):
    (tmp_path / "feeds.yaml").write_text(
        "feeds:\n  - {name: a, url: 'https://x/y.xml', source: ' Instagram '}\n"
    )
    assert load_config(tmp_path, env={}).feeds.feeds[0].source == "instagram"


def test_secrets_from_env_and_repr_hides_values():
    secrets = Secrets.from_env(
        {"DISCORD_BOT_TOKEN": "super-secret", "REDDIT_CLIENT_ID": "id", "REDDIT_CLIENT_SECRET": " ", "X": "y"}
    )
    assert secrets.discord_bot_token == "super-secret"
    assert secrets.reddit_client_secret is None
    assert not secrets.has_reddit_oauth
    assert not secrets.has_bluesky_login
    assert "super-secret" not in repr(secrets)
    assert "discord_bot_token" in str(secrets)


def test_config_dir_from_env(tmp_path):
    (tmp_path / "settings.yaml").write_text("decisions: {alarm_score: 80}\n")
    config = load_config(env={"GEMBOT_CONFIG_DIR": str(tmp_path)})
    assert config.settings.decisions.alarm_score == 80


# ---------------------------------------------------------------- models


def test_mention_key_age_and_utc():
    m = make_mention("bluesky", "did:plc:x/3k", hours_ago=5)
    assert m.key == "bluesky:did:plc:x/3k"
    assert m.age_hours(NOW) == pytest.approx(5)
    naive = Mention(source="x", source_id="1", url="u", created_at=datetime(2026, 1, 1))
    assert naive.created_at.tzinfo is UTC
    assert ensure_utc(datetime(2026, 1, 1, tzinfo=UTC) + timedelta(0)).tzinfo is UTC


def test_engagement_total_ignores_negative_scores():
    assert Engagement(likes=-3, comments=4, shares=1).total == 5


def test_game_best_url_preference():
    m1 = make_mention("reddit", "a", likes=5, url="https://reddit.com/a")
    m2 = make_mention("bluesky", "b", likes=50, url="https://bsky.app/b")
    g = Game(game_id="t:x", title="X", first_seen=NOW, last_seen=NOW, mention_keys=[m1.key, m2.key])
    assert g.best_url({m1.key: m1, m2.key: m2}) == "https://bsky.app/b"
    assert g.best_url() is None
    g.itch_url = "https://dev.itch.io/x"
    assert g.best_url() == "https://dev.itch.io/x"
    g.steam_appid = 42
    assert g.best_url() == "https://store.steampowered.com/app/42/"


def test_state_round_trips_through_json():
    state = State()
    m = make_mention()
    state.mentions[m.key] = m
    state.seen[m.key] = NOW
    state.meta.daily_alarms["2026-10-03"] = 2
    again = State.model_validate_json(state.model_dump_json())
    assert again.mentions[m.key].title == m.title
    assert again.meta.alarms_on(NOW.date()) == 2
    assert platform_label("bluesky") == "Bluesky"
    assert platform_label("mastodon") == "Mastodon"


# ---------------------------------------------------------------- http


def test_budget_take_and_exhaust():
    b = Budget("s", 2)
    b.take()
    b.take()
    assert b.exhausted and b.remaining == 0
    with pytest.raises(BudgetExceeded):
        b.take()


@respx.mock
def test_get_json_sends_user_agent_and_charges_budget():
    route = respx.get("https://api.test/x").mock(return_value=httpx.Response(200, json={"ok": 1}))
    b = Budget("s", 5)
    with make_http() as http:
        assert http.get_json("https://api.test/x", budget=b) == {"ok": 1}
    assert route.calls.last.request.headers["user-agent"] == "GemBot-test/0.1"
    assert b.used == 1


@respx.mock
def test_429_retries_after_retry_after_then_succeeds():
    respx.get("https://api.test/x").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1.5"}),
            httpx.Response(429, json={"retry_after": 0.25, "global": False}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    slept: list[float] = []
    b = Budget("s", 5)
    http = make_http(sleep=slept.append, retries=2)
    assert http.get_json("https://api.test/x", budget=b) == {"ok": True}
    assert slept == [1.5, 0.25]
    assert b.used == 3


@respx.mock
def test_429_with_long_wait_raises_rate_limited_without_sleeping():
    respx.get("https://api.test/x").mock(return_value=httpx.Response(429, headers={"Retry-After": "600"}))
    slept: list[float] = []
    with pytest.raises(RateLimited) as info:
        make_http(sleep=slept.append).get("https://api.test/x", budget=Budget("s", 5))
    assert info.value.status == 429
    assert slept == []


@respx.mock
def test_5xx_retries_then_raises_http_error():
    respx.get("https://api.test/x").mock(return_value=httpx.Response(503))
    b = Budget("s", 10)
    with pytest.raises(HttpError) as info:
        make_http(retries=2).get("https://api.test/x", budget=b)
    assert info.value.status == 503
    assert b.used == 3


@respx.mock
def test_4xx_raises_immediately_and_expect_allows_other_codes():
    respx.get("https://api.test/x").mock(return_value=httpx.Response(403, text="blocked"))
    respx.put("https://api.test/r").mock(return_value=httpx.Response(204))
    b = Budget("s", 10)
    http = make_http()
    with pytest.raises(HttpError) as info:
        http.get("https://api.test/x", budget=b)
    assert info.value.status == 403 and b.used == 1
    assert http.request("PUT", "https://api.test/r", budget=b, expect=(204,)).status_code == 204


@respx.mock
def test_transport_error_retries_then_raises():
    respx.get("https://api.test/x").mock(side_effect=httpx.ConnectError("boom"))
    b = Budget("s", 10)
    with pytest.raises(HttpError, match="ConnectError"):
        make_http(retries=1).get("https://api.test/x", budget=b)
    assert b.used == 2


@respx.mock
def test_invalid_json_raises_http_error():
    respx.get("https://api.test/x").mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(HttpError, match="invalid JSON"):
        make_http().get_json("https://api.test/x", budget=Budget("s", 2))


@respx.mock
def test_conditional_get_uses_etag_cache():
    route = respx.get("https://feed.test/a.xml").mock(
        side_effect=[
            httpx.Response(200, text="<rss/>", headers={"ETag": '"v1"', "Last-Modified": "Sat, 03 Oct 2026"}),
            httpx.Response(304),
        ]
    )
    http = make_http()
    b = Budget("s", 5)
    assert http.get("https://feed.test/a.xml", budget=b, conditional=True).status_code == 200
    assert http.cache["https://feed.test/a.xml"].etag == '"v1"'
    assert http.get("https://feed.test/a.xml", budget=b, conditional=True).status_code == 304
    sent = route.calls.last.request.headers
    assert sent["if-none-match"] == '"v1"'
    assert sent["if-modified-since"] == "Sat, 03 Oct 2026"


@respx.mock
def test_post_and_budget_exceeded_before_sending():
    route = respx.post("https://api.test/p").mock(return_value=httpx.Response(200, json={}))
    b = Budget("s", 1)
    http = make_http()
    http.post("https://api.test/p", budget=b, json={"a": 1})
    with pytest.raises(BudgetExceeded):
        http.post("https://api.test/p", budget=b, json={"a": 1})
    assert route.call_count == 1


# ---------------------------------------------------------------- collector base


class _Demo(Collector):
    name = "reddit"

    def __init__(self, ctx, plan, **kw):
        super().__init__(ctx, **kw)
        self.plan = plan

    def collect(self):
        for label, action in self.plan:
            with self.guard(label):
                result = action(self)
                if result is not None:
                    self.found.append(result)
        return self.found


def _ctx(config=None):
    return CollectContext(config=config or make_config(), http=make_http(), now=NOW)


def _raise(exc):
    def action(_self):
        raise exc

    return action


def test_guard_isolates_failures_and_dedupes():
    m = make_mention("reddit", "1")
    plan = [
        ("a", lambda s: m),
        ("b", _raise(HttpError("HTTP 403", 403))),
        ("c", _raise(ValueError("bad payload"))),
        ("d", lambda s: m),
    ]
    mentions, report = _Demo(_ctx(), plan).run()
    assert [x.key for x in mentions] == ["reddit:1"]
    assert report.ok_units == 2 and report.failed_units == 2
    assert report.ok
    assert any("bad payload" in e for e in report.errors)
    assert "2 error(s)" in report.summary()


def test_all_units_failing_marks_report_failed():
    plan = [("a", _raise(HttpError("HTTP 500", 500)))]
    mentions, report = _Demo(_ctx(), plan).run()
    assert mentions == [] and not report.ok
    assert "FAILED" in report.summary()


def test_budget_exceeded_keeps_partial_results():
    def spend(s):
        s.budget.take()
        return make_mention("reddit", str(s.budget.used))

    collector = _Demo(_ctx(), [("x", spend)] * 5, budget=Budget("reddit", 2))
    mentions, report = collector.run()
    assert len(mentions) == 2
    assert report.warnings and "budget" in report.warnings[0]
    assert report.requests == 2


def test_crash_outside_guard_is_isolated():
    class Crashy(Collector):
        name = "itch"

        def collect(self):
            self.found.append(make_mention("itch", "dev/one"))
            raise RuntimeError("kaboom")

    mentions, report = Crashy(_ctx()).run()
    assert len(mentions) == 1
    assert "kaboom" in report.errors[0]


def test_disabled_collector_is_skipped_not_failed():
    class Off(Collector):
        name = "x"

        def enabled(self):
            return False, "X_BEARER_TOKEN not set"

        def collect(self):  # pragma: no cover - must not be called
            raise AssertionError

    mentions, report = Off(_ctx()).run()
    assert mentions == [] and report.skipped and report.ok
    assert "skipped" in report.summary()


def test_safe_fetch_helpers_never_raise():
    class C(Collector):
        name = "bluesky"

        def collect(self):
            return []

        def fetch_comments(self, mention, limit):
            raise HttpError("HTTP 500", 500)

        def fetch_audience(self, mention):
            raise ValueError("nope")

    c = C(_ctx())
    m = make_mention("bluesky", "x")
    assert c.safe_fetch_comments(m, 10) == []
    assert c.safe_fetch_audience(m) is None
    assert len(c.report.warnings) == 2
    assert Collector.fetch_comments(c, m, 1) == [] and Collector.fetch_audience(c, m) is None


def test_scratch_is_persisted_in_state_or_throwaway():
    from gembot.models import State

    class S(Collector):
        name = "steam"

        def collect(self):
            self.scratch()["cursor"] = 7
            return []

    state = State()
    S(CollectContext(config=make_config(), http=make_http(), now=NOW, state=state)).run()
    assert state.meta.collector_state["steam"] == {"cursor": 7}
    c = S(_ctx())
    c.run()
    assert c.scratch() == {"cursor": 7}


@respx.mock
def test_per_request_retry_override():
    respx.get("https://api.test/x").mock(return_value=httpx.Response(429, headers={"Retry-After": "1"}))
    slept: list[float] = []
    b = Budget("s", 10)
    with pytest.raises(RateLimited):
        make_http(sleep=slept.append, retries=3).get("https://api.test/x", budget=b, retries=0)
    assert slept == [] and b.used == 1


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"x-ratelimit-reset": "12"}, 12.0),  # Reddit: seconds until reset
        ({"ratelimit-reset": str(int(NOW.timestamp()) + 9)}, 9.0),  # Bluesky: unix time
        ({"x-rate-limit-reset": str(int(NOW.timestamp()) - 5)}, 0.0),  # already passed
        ({"x-ratelimit-reset": "soon"}, None),
    ],
)
def test_retry_after_from_rate_limit_reset_headers(headers, expected):
    from gembot.http import _retry_after_seconds

    response = httpx.Response(429, headers=headers, text="slow down")
    assert _retry_after_seconds(response, NOW.timestamp()) == expected
    if expected is not None and "ratelimit-reset" in headers:
        assert _retry_after_seconds(response) is None  # epoch values need a clock


@respx.mock
def test_429_waits_for_reddit_style_reset_header():
    respx.get("https://api.test/x").mock(
        side_effect=[httpx.Response(429, headers={"x-ratelimit-reset": "3"}), httpx.Response(200, json={})]
    )
    slept: list[float] = []
    make_http(sleep=slept.append).get("https://api.test/x", budget=Budget("s", 5))
    assert slept == [3.0]


# ---------------------------------------------------------------- http hardening (review findings)


def test_secrets_drop_inner_whitespace_from_pasted_tokens():
    secrets = Secrets.from_env({"X_BEARER_TOKEN": " AAAA\nBBBB \t", "DISCORD_BOT_TOKEN": "\n"})
    assert secrets.x_bearer_token == "AAAABBBB"
    assert secrets.discord_bot_token is None


def test_protocol_errors_never_echo_header_values():
    def handler(request):
        raise httpx.LocalProtocolError("Illegal header value b'Bearer SECRET-TOKEN'", request=request)

    http = make_http(httpx.MockTransport(handler), retries=3)
    budget = Budget("x", 10)
    with pytest.raises(HttpError) as info:
        http.get("https://api.test/x", budget=budget)
    assert "SECRET-TOKEN" not in str(info.value) and "LocalProtocolError" in str(info.value)
    assert budget.used == 1  # deterministic failure: not retried


def test_redirect_hops_are_budgeted_and_loops_stop():
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://api.test/loop"})

    budget = Budget("itch", 12)
    with pytest.raises(HttpError, match="too many redirects"):
        make_http(httpx.MockTransport(handler)).get("https://api.test/start", budget=budget)
    assert len(calls) == budget.used == 6  # first request + 5 hops, each one charged


def test_redirects_are_followed_and_recorded():
    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(301, headers={"Location": "https://api.test/new"})
        return httpx.Response(200, json={"ok": 1})

    budget = Budget("s", 5)
    response = make_http(httpx.MockTransport(handler)).get("https://api.test/old", budget=budget)
    assert response.json() == {"ok": 1} and str(response.url) == "https://api.test/new"
    assert len(response.history) == 1 and budget.used == 2
    raw = make_http(httpx.MockTransport(handler)).get(
        "https://api.test/old", budget=Budget("s", 5), follow_redirects=False, expect=(301,)
    )
    assert raw.status_code == 301 and raw.headers["location"] == "https://api.test/new"


def test_retry_after_as_http_date_and_nonsense_values():
    from gembot.http import _retry_after_seconds

    hour_later = httpx.Response(429, headers={"Retry-After": "Sat, 03 Oct 2026 13:00:00 GMT"})
    assert _retry_after_seconds(hour_later, NOW.timestamp()) == 3600.0
    for value in ("nan", "inf", "-inf"):
        assert _retry_after_seconds(httpx.Response(429, headers={"Retry-After": value})) == float("inf")
    answers = iter([hour_later, httpx.Response(200)])
    slept: list[float] = []
    http = make_http(httpx.MockTransport(lambda r: next(answers)), sleep=slept.append)
    with pytest.raises(RateLimited):
        http.get("https://api.test/x", budget=Budget("s", 5))
    assert slept == []


def test_response_size_cap_stops_reading():
    class Big(httpx.SyncByteStream):
        pulled = 0

        def __iter__(self):
            for _ in range(64):
                Big.pulled += 1 << 20
                yield b" " * (1 << 20)

    transport = httpx.MockTransport(lambda r: httpx.Response(200, stream=Big()))
    with pytest.raises(HttpError, match="larger than"):
        make_http(transport).get("https://api.test/big", budget=Budget("s", 2), max_bytes=2 << 20)
    assert Big.pulled <= 3 << 20
    declared = httpx.MockTransport(lambda r: httpx.Response(200, headers={"Content-Length": "999999999"}))
    with pytest.raises(HttpError, match="too large"):
        make_http(declared).get("https://api.test/big", budget=Budget("s", 2))


def test_trickling_response_hits_the_deadline():
    import time as _time

    class Drip(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(40):
                _time.sleep(0.02)
                yield b" "

    http = make_http(httpx.MockTransport(lambda r: httpx.Response(200, stream=Drip())), deadline_s=0.1)
    started = _time.monotonic()
    with pytest.raises(HttpError, match="too slow"):
        http.get("https://api.test/slow", budget=Budget("s", 2), retries=0)
    assert _time.monotonic() - started < 0.5


def test_budget_deadline_stops_new_requests():
    import time as _time

    budget = Budget("steam", 100, deadline=_time.monotonic() - 1)
    assert budget.exhausted and budget.remaining == 0
    with pytest.raises(BudgetExceeded, match="out of time"):
        budget.take()


def test_gzip_body_is_decoded_once():
    import gzip

    body = gzip.compress(b'{"ok": true}')
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, content=body, headers={"Content-Encoding": "gzip"})
    )
    assert make_http(transport).get_json("https://api.test/z", budget=Budget("s", 2)) == {"ok": True}
