"""Bluesky collector: session reuse, search routes, logged-out probe, parsing, threads, profiles.

No network: every request is answered by respx from tests/fixtures/bluesky/.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

import httpx
import pytest
import respx

from gembot.collectors.base import CollectContext
from gembot.collectors.bluesky import (
    LOGIN_LIMIT_WARNING,
    PUBLIC_BLOCKED_REASON,
    BlueskyCollector,
    BlueskySession,
    SessionBox,
    parse_at_uri,
    parse_post,
    post_url,
)
from gembot.config import load_config
from gembot.http import Budget
from gembot.models import Engagement, State
from tests.factories import NOW, ROOT, make_config, make_http, make_mention, read_fixture

ENTRY = "https://bsky.social"
PDS = "https://amanita.us-east.host.bsky.network"
CREATE = f"{ENTRY}/xrpc/com.atproto.server.createSession"
REFRESH = f"{ENTRY}/xrpc/com.atproto.server.refreshSession"
PDS_SEARCH = f"{PDS}/xrpc/app.bsky.feed.searchPosts"
ENTRY_SEARCH = f"{ENTRY}/xrpc/app.bsky.feed.searchPosts"
APPVIEW_SEARCH = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"
PUBLIC = "https://public.api.bsky.app"
PUBLIC_SEARCH = f"{PUBLIC}/xrpc/app.bsky.feed.searchPosts"
THREAD = f"{PUBLIC}/xrpc/app.bsky.feed.getPostThread"
PROFILE = f"{PUBLIC}/xrpc/app.bsky.actor.getProfile"
PROFILES = f"{PUBLIC}/xrpc/app.bsky.actor.getProfiles"
PROXY = "did:web:api.bsky.app#bsky_appview"

HANDLE = "gembot-test.bsky.social"
PASSWORD = "abcd-efgh-ijkl-mnop"
ENV = {"BLUESKY_HANDLE": HANDLE, "BLUESKY_APP_PASSWORD": PASSWORD}
ME = "did:plc:gembottest4xq2m7kz3d5vbn"
ACCESS1 = "eyJhbGciOiJFUzI1NksifQ.FAKE-ACCESS-JWT-1.sig"  # create_session.json
REFRESH1 = "eyJhbGciOiJFUzI1NksifQ.FAKE-REFRESH-JWT-1.sig"
ACCESS2 = "eyJhbGciOiJFUzI1NksifQ.FAKE-ACCESS-JWT-2.sig"  # refresh_session.json
REFRESH2 = "eyJhbGciOiJFUzI1NksifQ.FAKE-REFRESH-JWT-2.sig"
ACCESS0 = "stored-access-token-0"
REFRESH0 = "stored-refresh-token-0"
TERMS = ["friendslop", '"proximity chat"']
GP = "did:plc:gpzdev2jq7kx4mnb3vwa5ty6"
GP_URI = f"at://{GP}/app.bsky.feed.post/3m2ykq7xfjc2s"


# ---------------------------------------------------------------- helpers


def fx(name: str) -> dict:
    return json.loads(read_fixture("bluesky", name))


def ok(name: str = "search_empty.json") -> httpx.Response:
    return httpx.Response(200, json=fx(name))


def err(status: int, body: str | dict) -> httpx.Response:
    return httpx.Response(status, json=fx(body) if isinstance(body, str) else body)


def cdn_403() -> httpx.Response:
    return httpx.Response(
        403,
        text=read_fixture("bluesky", "cdn_403.html"),
        headers={"content-type": "text/html; charset=utf-8"},
    )


def by_term(mapping: dict[str, httpx.Response], default: httpx.Response | None = None) -> Callable:
    def handler(request: httpx.Request) -> httpx.Response:
        return mapping.get(request.url.params["q"], default or ok())

    return handler


def iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_collector(
    *,
    env: dict[str, str] | None = None,
    state: State | None = None,
    budget: int = 40,
    now: datetime = NOW,
    slept: list[float] | None = None,
    **bluesky: object,
) -> BlueskyCollector:
    config = make_config(env=ENV if env is None else env)
    config.sources.bluesky = config.sources.bluesky.model_copy(update={"terms": TERMS, **bluesky})
    http = make_http(sleep=slept.append if slept is not None else (lambda _s: None))
    ctx = CollectContext(config=config, http=http, now=now, state=state if state is not None else State())
    return BlueskyCollector(ctx, budget=Budget("bluesky", budget))


def scratch(state: State) -> dict:
    return state.meta.collector_state.setdefault("bluesky", {})


def seed_session(
    state: State,
    *,
    access_exp: datetime,
    password: str = PASSWORD,
    handle: str = HANDLE,
    pds: str = PDS,
) -> None:
    session = BlueskySession(
        access_jwt=ACCESS0, refresh_jwt=REFRESH0, access_exp=access_exp, did=ME, handle=HANDLE, pds=pds
    )
    scratch(state)["session"] = SessionBox(handle, password).seal(session)


def stored(state: State) -> BlueskySession:
    session = SessionBox(HANDLE, PASSWORD).open(scratch(state).get("session"))
    assert session is not None
    return session


def no_token(route: respx.Route) -> bool:
    return all("authorization" not in call.request.headers for call in route.calls)


@pytest.fixture
def mock():
    with respx.mock(assert_all_called=False) as router:
        # anything not explicitly mocked fails loudly instead of touching the network
        yield router


# ---------------------------------------------------------------- config / enabled


def test_sources_yaml_has_bluesky_session_settings():
    bsky = load_config(ROOT / "config", env={}).sources.bluesky
    assert bsky.max_sessions_per_day == 8
    assert bsky.unauth_probe_hours == 24
    assert bsky.search_pages == 1
    assert bsky.appview == "https://api.bsky.app"
    assert bsky.appview_proxy == PROXY
    assert bsky.pds_host == ENTRY and bsky.public_appview == PUBLIC
    assert bsky.terms and bsky.limit <= 100


def test_enabled_needs_the_flag_and_at_least_one_term():
    assert make_collector().enabled() == (True, None)
    assert make_collector(env={}).enabled() == (True, None)  # logged out still probes
    off, reason = make_collector(enabled=False).enabled()
    assert not off and "disabled" in reason
    off, reason = make_collector(terms=["  ", ""]).enabled()
    assert not off and "terms" in reason
    mentions, report = make_collector(enabled=False).run()
    assert mentions == [] and report.skipped and report.ok


# ---------------------------------------------------------------- login + encrypted session


def test_first_run_logs_in_searches_via_pds_and_stores_session_encrypted(mock, caplog):
    caplog.set_level(logging.DEBUG)
    state = State()
    create = mock.post(CREATE).mock(return_value=ok("create_session.json"))
    search = mock.get(PDS_SEARCH).mock(side_effect=by_term({"friendslop": ok("search_indiedev.json")}))
    public = mock.route(host="public.api.bsky.app").mock(return_value=httpx.Response(500))
    appview = mock.route(host="api.bsky.app").mock(return_value=httpx.Response(500))

    mentions, report = make_collector(state=state).run()

    assert json.loads(create.calls.last.request.content) == {"identifier": HANDLE, "password": PASSWORD}
    assert report.ok and report.errors == [] and report.warnings == []
    assert report.ok_units == 2 and report.requests == 3
    assert len(mentions) == 5
    assert search.call_count == 2
    for call in search.calls:
        assert call.request.headers["authorization"] == f"Bearer {ACCESS1}"
        assert call.request.headers["atproto-proxy"] == PROXY
    assert not public.called and not appview.called

    sc = scratch(state)
    assert sc["created"] == ["2026-10-03T12:00:00Z"]
    assert "route" not in sc and "login_error" not in sc
    public_state = json.dumps(state.meta.model_dump(mode="json"))
    for secret in (ACCESS1, REFRESH1, PASSWORD, "FAKE-ACCESS", "FAKE-REFRESH"):
        assert secret not in public_state
        assert secret not in caplog.text
    session = stored(state)
    assert (session.access_jwt, session.refresh_jwt) == (ACCESS1, REFRESH1)
    assert session.pds == PDS and session.did == ME and session.handle == HANDLE
    assert session.access_exp == NOW + timedelta(minutes=110)
    assert ACCESS1 not in repr(session)
    # survives the trip through the state files
    again = State.model_validate_json(state.model_dump_json())
    assert SessionBox(HANDLE, PASSWORD).open(scratch(again)["session"]) == session
    # useless without the app password
    assert SessionBox(HANDLE, "zzzz-zzzz-zzzz-zzzz").open(sc["session"]) is None


def test_valid_access_token_is_reused_without_any_auth_call(mock):
    state = State()
    seed_session(state, access_exp=NOW + timedelta(minutes=30))
    auth = mock.route(host="bsky.social").mock(return_value=httpx.Response(500))
    search = mock.get(PDS_SEARCH).mock(return_value=ok())

    _, report = make_collector(state=state).run()

    assert not auth.called
    assert search.calls[0].request.headers["authorization"] == f"Bearer {ACCESS0}"
    assert report.requests == 2 and report.ok
    assert stored(state).access_jwt == ACCESS0


@pytest.mark.parametrize("left", [timedelta(minutes=4), timedelta(hours=-3)])
def test_expiring_access_token_is_refreshed_and_rotated_tokens_are_persisted(mock, left):
    state = State()
    seed_session(state, access_exp=NOW + left)
    create = mock.post(CREATE).mock(return_value=ok("create_session.json"))
    refresh = mock.post(REFRESH).mock(return_value=ok("refresh_session.json"))
    search = mock.get(PDS_SEARCH).mock(return_value=ok())

    _, report = make_collector(state=state).run()

    assert report.ok and not create.called
    sent = refresh.calls.last.request
    assert sent.headers["authorization"] == f"Bearer {REFRESH0}" and sent.content == b""
    assert search.calls[0].request.headers["authorization"] == f"Bearer {ACCESS2}"
    session = stored(state)
    assert (session.access_jwt, session.refresh_jwt) == (ACCESS2, REFRESH2)
    assert session.access_exp == NOW + timedelta(minutes=110)
    assert scratch(state).get("created", []) == []


@pytest.mark.parametrize(
    "rejection",
    [
        err(400, "error_expired.json"),
        err(400, {"error": "ExpiredToken", "message": "Token has been revoked"}),
        err(400, {"error": "InvalidToken", "message": "Token could not be verified"}),
        err(401, {"error": "AuthenticationRequired"}),
    ],
)
def test_dead_refresh_token_falls_back_to_a_new_login(mock, rejection):
    state = State()
    seed_session(state, access_exp=NOW - timedelta(hours=1))
    refresh = mock.post(REFRESH).mock(return_value=rejection)
    create = mock.post(CREATE).mock(return_value=ok("create_session.json"))
    search = mock.get(PDS_SEARCH).mock(return_value=ok())

    _, report = make_collector(state=state).run()

    assert refresh.call_count == 1 and create.call_count == 1
    assert report.ok and report.errors == []
    assert search.calls[0].request.headers["authorization"] == f"Bearer {ACCESS1}"
    assert stored(state).refresh_jwt == REFRESH1
    assert scratch(state)["created"] == [iso(NOW)]


def test_refresh_server_error_keeps_the_session_and_does_not_log_in(mock):
    state = State()
    seed_session(state, access_exp=NOW - timedelta(hours=1))
    refresh = mock.post(REFRESH).mock(return_value=httpx.Response(502))
    create = mock.post(CREATE).mock(return_value=ok("create_session.json"))
    search = mock.get(PDS_SEARCH).mock(return_value=ok())

    mentions, report = make_collector(state=state).run()

    assert refresh.call_count == 3  # 5xx: the HTTP layer retries
    assert not create.called and not search.called
    assert mentions == [] and not report.ok
    assert "login" in report.errors[0] and "502" in report.errors[0]
    assert stored(state).refresh_jwt == REFRESH0  # try again next run


@pytest.mark.parametrize("bad", ["not-a-fernet-token", "other-password"])
def test_session_that_cannot_be_decrypted_means_a_new_login(mock, bad):
    state = State()
    if bad == "other-password":  # the app password was rotated
        seed_session(state, access_exp=NOW + timedelta(hours=1), password="old0-old0-old0-old0")
    else:
        scratch(state)["session"] = bad
    create = mock.post(CREATE).mock(return_value=ok("create_session.json"))
    mock.get(PDS_SEARCH).mock(return_value=ok())

    _, report = make_collector(state=state).run()

    assert create.call_count == 1 and report.ok
    assert stored(state).access_jwt == ACCESS1


def test_handle_spelling_does_not_change_the_key():
    session = BlueskySession(access_jwt="a", refresh_jwt="r", access_exp=NOW, did=ME, handle=HANDLE, pds=PDS)
    token = SessionBox("GemBot-Test.bsky.social", PASSWORD).seal(session)
    assert SessionBox("@gembot-test.bsky.social", PASSWORD).open(token) == session
    assert SessionBox("someone-else.bsky.social", PASSWORD).open(token) is None
    assert SessionBox(HANDLE, PASSWORD).fingerprint != SessionBox(HANDLE, "x").fingerprint
    assert SessionBox(HANDLE, PASSWORD).open(None) is None


def test_daily_login_cap_skips_search_with_a_warning_and_no_request(mock, caplog):
    state = State()
    sc = scratch(state)
    sc["created"] = [iso(NOW - timedelta(hours=h)) for h in (1, 2, 3, 4, 5, 6, 7, 23)]
    sc["created"].append(iso(NOW - timedelta(hours=25)))  # outside the window: pruned
    anything = mock.route().mock(return_value=httpx.Response(500))

    mentions, report = make_collector(state=state).run()

    assert not anything.called
    assert mentions == [] and report.warnings == [LOGIN_LIMIT_WARNING]
    assert report.ok and report.errors == [] and not report.skipped
    assert len(sc["created"]) == 8
    assert LOGIN_LIMIT_WARNING in caplog.text


def test_login_cap_is_configurable_and_counts_only_the_last_24h(mock):
    state = State()
    scratch(state)["created"] = [iso(NOW - timedelta(hours=h)) for h in (1, 2, 30)]
    create = mock.post(CREATE).mock(return_value=ok("create_session.json"))
    mock.get(PDS_SEARCH).mock(return_value=ok())

    _, report = make_collector(state=state, max_sessions_per_day=3).run()

    assert create.call_count == 1 and report.ok
    assert scratch(state)["created"] == [
        iso(NOW - timedelta(hours=1)),
        iso(NOW - timedelta(hours=2)),
        iso(NOW),
    ]


def test_login_cap_after_a_failed_login_keeps_reporting_the_failure(mock):
    state = State()
    sc = scratch(state)
    sc["created"] = [iso(NOW - timedelta(hours=1))]
    sc["login_error"] = {
        "at": iso(NOW - timedelta(hours=1)),
        "message": "createSession failed: HTTP 502",
        "fp": "whatever",
        "fatal": False,
    }
    _, report = make_collector(state=state, max_sessions_per_day=1).run()
    assert report.warnings == [LOGIN_LIMIT_WARNING]
    assert report.errors == ["login: createSession failed: HTTP 502"] and not report.ok


def test_rejected_app_password_is_explained_and_not_retried_with_the_same_secret(mock):
    state = State()
    create = mock.post(CREATE).mock(return_value=err(401, "error_auth.json"))
    search = mock.get(PDS_SEARCH).mock(return_value=ok())

    mentions, report = make_collector(state=state).run()

    assert mentions == [] and not report.ok and not search.called
    assert "BLUESKY_APP_PASSWORD rejected — create a new App Password" in report.errors[0]
    assert create.call_count == 1
    sc = scratch(state)
    assert sc["login_error"]["fatal"] is True and sc["created"] == [iso(NOW)]
    assert PASSWORD not in json.dumps(sc)

    # same secret, next run: no request, still reported as broken
    _, again = make_collector(state=state, now=NOW + timedelta(minutes=30)).run()
    assert create.call_count == 1
    assert "rejected" in again.errors[0] and "when the secret changes" in again.errors[0]

    # after a day the same secret gets one more try
    make_collector(state=state, now=NOW + timedelta(hours=25)).run()
    assert create.call_count == 2

    # a new secret is tried right away, and success clears the error
    create.mock(return_value=ok("create_session.json"))
    fixed = {**ENV, "BLUESKY_APP_PASSWORD": "wxyz-wxyz-wxyz-wxyz"}
    _, healed = make_collector(env=fixed, state=state, now=NOW + timedelta(hours=25, minutes=30)).run()
    assert create.call_count == 3 and healed.ok
    assert "login_error" not in sc


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            err(401, {"error": "AuthFactorTokenRequired", "message": "A sign in code has been sent"}),
            "use an App Password, not your main password",
        ),
        (err(400, {"error": "AccountTakedown", "message": "Account has been taken down"}), "taken down"),
        (err(400, {"error": "InvalidRequest", "message": "bad identifier"}), "HTTP 400 InvalidRequest: bad"),
        (
            httpx.Response(200, json={**fx("create_session.json"), "active": False, "status": "deactivated"}),
            "not active (deactivated)",
        ),
        (httpx.Response(200, json={"did": ME, "handle": HANDLE}), "no tokens"),
    ],
)
def test_login_errors_are_explained(mock, response, expected):
    state = State()
    mock.post(CREATE).mock(return_value=response)
    search = mock.get(PDS_SEARCH).mock(return_value=ok())
    _, report = make_collector(state=state).run()
    assert expected in report.errors[0]
    assert not search.called and not report.ok
    assert scratch(state)["login_error"]["fatal"] is True
    assert "session" not in scratch(state)


def test_create_session_is_never_retried_by_the_http_layer(mock):
    state = State()
    create = mock.post(CREATE).mock(
        return_value=httpx.Response(
            429,
            json={"error": "RateLimitExceeded", "message": "Rate Limit Exceeded"},
            headers={"ratelimit-limit": "10", "ratelimit-remaining": "0", "ratelimit-reset": "1791100000"},
        )
    )
    slept: list[float] = []
    collector = make_collector(state=state, slept=slept)

    _, report = collector.run()

    assert create.call_count == 1 and slept == []
    assert collector.http.retries == 2  # restored
    assert "429" in report.errors[0]
    assert scratch(state)["login_error"]["fatal"] is False
    assert scratch(state)["created"] == [iso(NOW)]


def test_login_without_diddoc_uses_the_login_server_as_pds(mock):
    state = State()
    body = {k: v for k, v in fx("create_session.json").items() if k != "didDoc"}
    mock.post(CREATE).mock(return_value=httpx.Response(200, json=body))
    search = mock.get(ENTRY_SEARCH).mock(return_value=ok())
    _, report = make_collector(state=state).run()
    assert report.ok and search.call_count == 2
    assert search.calls[0].request.headers["atproto-proxy"] == PROXY
    assert stored(state).pds == ENTRY


# ---------------------------------------------------------------- search routes


@pytest.mark.parametrize(
    "refusal",
    [
        err(401, {"error": "AuthenticationRequired", "message": "Bad token scope"}),
        err(403, {"error": "Forbidden"}),
    ],
)
def test_pds_refusal_falls_back_to_appview_and_remembers_it(mock, refusal):
    state = State()
    seed_session(state, access_exp=NOW + timedelta(hours=1))
    pds = mock.get(PDS_SEARCH).mock(return_value=refusal)
    appview = mock.get(APPVIEW_SEARCH).mock(side_effect=by_term({"friendslop": ok("search_indiedev.json")}))

    mentions, report = make_collector(state=state).run()

    assert report.ok and report.errors == [] and len(mentions) == 5
    assert pds.call_count == 1 and appview.call_count == 2
    sent = appview.calls[0].request.headers
    assert sent["authorization"] == f"Bearer {ACCESS0}" and "atproto-proxy" not in sent
    assert scratch(state)["route"] == "appview"

    _, second = make_collector(state=state).run()
    assert second.ok and pds.call_count == 1 and appview.call_count == 4


def test_remembered_appview_route_switches_back_to_pds_when_refused(mock):
    state = State()
    seed_session(state, access_exp=NOW + timedelta(hours=1))
    scratch(state)["route"] = "appview"
    mock.get(APPVIEW_SEARCH).mock(return_value=err(403, {"error": "Forbidden"}))
    pds = mock.get(PDS_SEARCH).mock(return_value=ok())
    _, report = make_collector(state=state).run()
    assert report.ok and pds.call_count == 2
    assert scratch(state)["route"] == "pds"


def test_both_routes_refusing_stops_bluesky_for_the_run(mock):
    state = State()
    seed_session(state, access_exp=NOW + timedelta(hours=1))
    pds = mock.get(PDS_SEARCH).mock(return_value=err(401, {"error": "AuthenticationRequired"}))
    appview = mock.get(APPVIEW_SEARCH).mock(return_value=err(401, {"error": "AuthenticationRequired"}))

    mentions, report = make_collector(state=state).run()

    assert mentions == [] and not report.ok
    assert pds.call_count == 1 and appview.call_count == 1  # second term never tried
    assert len(report.errors) == 1 and "refused on both routes" in report.errors[0]
    assert stored(state).access_exp == NOW  # next run starts with a refresh


def test_expired_token_during_search_refreshes_once_and_retries(mock):
    state = State()
    seed_session(state, access_exp=NOW + timedelta(hours=1))
    search = mock.get(PDS_SEARCH).mock(
        side_effect=[err(400, "error_expired.json"), ok("search_indiedev.json"), ok()]
    )
    refresh = mock.post(REFRESH).mock(return_value=ok("refresh_session.json"))

    mentions, report = make_collector(state=state).run()

    assert report.ok and report.errors == [] and len(mentions) == 5
    assert refresh.call_count == 1
    assert [c.request.headers["authorization"] for c in search.calls] == [
        f"Bearer {ACCESS0}",
        f"Bearer {ACCESS2}",
        f"Bearer {ACCESS2}",
    ]
    assert stored(state).refresh_jwt == REFRESH2


def test_token_still_expired_after_refresh_fails_the_term_without_looping(mock):
    state = State()
    seed_session(state, access_exp=NOW + timedelta(hours=1))
    search = mock.get(PDS_SEARCH).mock(return_value=err(400, "error_expired.json"))
    refresh = mock.post(REFRESH).mock(return_value=ok("refresh_session.json"))
    _, report = make_collector(state=state, terms=["friendslop"]).run()
    assert search.call_count == 2 and refresh.call_count == 1
    assert "ExpiredToken" in report.errors[0] and not report.ok


def test_failed_renewal_during_search_stops_the_run(mock):
    state = State()
    seed_session(state, access_exp=NOW + timedelta(hours=1))
    search = mock.get(PDS_SEARCH).mock(return_value=err(401, {"error": "ExpiredToken"}))
    mock.post(REFRESH).mock(return_value=err(400, "error_expired.json"))
    create = mock.post(CREATE).mock(return_value=err(401, "error_auth.json"))

    _, report = make_collector(state=state).run()

    assert search.call_count == 1 and create.call_count == 1
    assert len(report.errors) == 1
    assert "could not be renewed" in report.errors[0] and "App Password" in report.errors[0]


def test_cdn_refusal_mid_run_stops_and_keeps_partial_results(mock):
    state = State()
    seed_session(state, access_exp=NOW + timedelta(hours=1))
    scratch(state)["route"] = "appview"
    appview = mock.get(APPVIEW_SEARCH).mock(side_effect=[ok("search_indiedev.json"), cdn_403()])
    pds = mock.get(PDS_SEARCH).mock(return_value=cdn_403())

    mentions, report = make_collector(state=state, terms=["friendslop", "co-op", "proximity"]).run()

    assert len(mentions) == 5  # kept from the first term
    assert appview.call_count == 2 and pds.call_count == 1  # third term never tried
    assert len(report.errors) == 1 and "HTML page from the CDN" in report.errors[0]
    assert report.ok  # partial success


def test_never_sends_a_token_to_the_public_appview(mock):
    state = State()
    seed_session(state, access_exp=NOW + timedelta(hours=1))
    scratch(state)["route"] = "appview"
    public = mock.route(host="public.api.bsky.app").mock(return_value=ok())
    mock.get(PDS_SEARCH).mock(return_value=err(403, {"error": "Forbidden"}))
    _, report = make_collector(state=state, appview=PUBLIC, terms=["friendslop"]).run()
    assert no_token(public) and not public.called
    assert "refusing to send a Bluesky token" in report.errors[0]


# ---------------------------------------------------------------- logged out


@pytest.mark.parametrize(
    "refusal", [cdn_403(), err(403, {"error": "Forbidden"}), err(401, {"error": "AuthMissing"})]
)
def test_logged_out_probe_refused_is_a_skip_not_a_failure(mock, refusal, caplog):
    state = State()
    public = mock.get(PUBLIC_SEARCH).mock(return_value=refusal)

    mentions, report = make_collector(env={}, state=state).run()

    assert mentions == [] and public.call_count == 1 and no_token(public)
    assert report.skipped and report.skip_reason == PUBLIC_BLOCKED_REASON
    assert report.ok and report.errors == [] and report.warnings == [PUBLIC_BLOCKED_REASON]
    assert scratch(state) == {"public_ok": False, "public_probe_at": iso(NOW)}
    assert "BLUESKY_APP_PASSWORD" in caplog.text


def test_logged_out_between_probes_skips_quietly_without_a_request(mock):
    state = State()
    scratch(state).update(public_ok=False, public_probe_at=iso(NOW - timedelta(hours=23)))

    mentions, report = make_collector(env={}, state=state).run()

    assert mock.calls.call_count == 0 and mentions == []
    assert report.skipped and report.skip_reason == PUBLIC_BLOCKED_REASON
    assert report.warnings == [] and report.errors == []

    # a day later it probes again
    public = mock.get(PUBLIC_SEARCH).mock(return_value=cdn_403())
    make_collector(env={}, state=state, now=NOW + timedelta(hours=2)).run()
    assert public.call_count == 1
    assert scratch(state)["public_probe_at"] == iso(NOW + timedelta(hours=2))


def test_logged_out_probe_ok_collects_every_term_without_a_token(mock):
    state = State()
    seed_session(state, access_exp=NOW + timedelta(hours=1))  # left over from removed credentials
    scratch(state)["route"] = "appview"
    public = mock.get(PUBLIC_SEARCH).mock(side_effect=by_term({"friendslop": ok("search_indiedev.json")}))

    mentions, report = make_collector(env={}, state=state).run()

    assert report.ok and not report.skipped and report.ok_units == 2
    assert len(mentions) == 5 and public.call_count == 2 and no_token(public)
    assert scratch(state) == {"public_ok": True, "public_probe_at": iso(NOW)}  # old session forgotten

    # still open next run: search again straight away
    make_collector(env={}, state=state, now=NOW + timedelta(minutes=30)).run()
    assert public.call_count == 4


def test_logged_out_probe_server_error_is_a_failure_and_no_verdict(mock):
    state = State()
    mock.get(PUBLIC_SEARCH).mock(return_value=httpx.Response(503))
    _, report = make_collector(env={}, state=state).run()
    assert not report.ok and not report.skipped and "503" in report.errors[0]
    assert "public_probe_at" not in scratch(state)


def test_logged_out_cdn_refusal_mid_run_keeps_results_and_closes_the_window(mock):
    state = State()
    public = mock.get(PUBLIC_SEARCH).mock(side_effect=[ok("search_indiedev.json"), cdn_403(), ok()])
    mentions, report = make_collector(env={}, state=state, terms=["a", "b", "c"]).run()
    assert len(mentions) == 5 and public.call_count == 2
    assert len(report.errors) == 1 and "HTML page from the CDN" in report.errors[0] and report.ok
    assert scratch(state)["public_ok"] is False


def test_logged_out_bad_first_term_still_proves_search_is_reachable(mock):
    state = State()
    public = mock.get(PUBLIC_SEARCH).mock(
        side_effect=[
            err(400, {"error": "InvalidRequest", "message": "bad query"}),
            ok("search_indiedev.json"),
        ]
    )
    mentions, report = make_collector(env={}, state=state).run()
    assert len(mentions) == 5 and public.call_count == 2 and report.ok and not report.skipped
    assert report.failed_units == 1 and "InvalidRequest: bad query" in report.errors[0]
    assert scratch(state) == {"public_ok": True, "public_probe_at": iso(NOW)}


# ---------------------------------------------------------------- parsing

EXPECTED = [
    {
        "source_id": "did:plc:bonkbrigade7r5w3xk2mq4a6z/3m2ylb4cz7s2k",
        "url": "https://bsky.app/profile/did:plc:bonkbrigade7r5w3xk2mq4a6z/post/3m2ylb4cz7s2k",
        "title": "First trailer for Bonk Brigade! Ragdoll couch co-op for up to 4 players. "
        "https://bonkbrigade.itch.io/bonk-brigade",
        "author": "bonkbrigade.bsky.social",
        "hours_ago": 1,
        "engagement": (8, 1, 2),
        "links": ["https://bonkbrigade.itch.io/bonk-brigade"],
        "tags": ["gamedev"],
        "thumb": "https://video.bsky.app/watch/did%3Aplc%3Abonkbrigade7r5w3xk2mq4a6z/bafkreibbvideo/thumbnail.jpg",
    },
    {
        "source_id": f"{GP}/3m2ykq7xfjc2s",
        "url": f"https://bsky.app/profile/{GP}/post/3m2ykq7xfjc2s",
        "title": "Gorilla Pizza Panic is a chaotic 1-4 player co-op pizza delivery game with proximity chat 🦍🍕",
        "author": "gorillapizza.bsky.social",
        "hours_ago": 3,
        "engagement": (47, 12, 11),
        "links": ["https://store.steampowered.com/app/3990120/Gorilla_Pizza_Panic/"],
        "tags": ["indiedev", "coop"],
        "thumb": f"https://cdn.bsky.app/img/feed_thumbnail/plain/{GP}/bafkreigpzthumb@jpeg",
    },
    {
        "source_id": "did:plc:hollowcrew5nq2ztbz3kq7d4m/3m2xw7tbq2k2a",
        "url": "https://bsky.app/profile/did:plc:hollowcrew5nq2ztbz3kq7d4m/post/3m2xw7tbq2k2a",
        "title": "Hollow Crew screenshot dump! Four friends, one haunted submarine, zero working lights.",
        "author": "hollowcrew.bsky.social",
        "hours_ago": 20,
        "engagement": (120, 30, 14),
        "links": ["https://forms.example.com/hollowcrew-playtest"],
        "tags": ["screenshotsaturday", "coophorror"],
        "thumb": "https://cdn.bsky.app/img/feed_thumbnail/plain/did:plc:hollowcrew5nq2ztbz3kq7d4m/bafkreihc1@jpeg",
    },
    {
        "source_id": "did:plc:squeakysoup3v7m2dqk5zxw4b/3m2wq2d7m3c2p",
        "url": "https://bsky.app/profile/did:plc:squeakysoup3v7m2dqk5zxw4b/post/3m2wq2d7m3c2p",
        "title": "squeaky soup devlog #3: the ladle now has physics and my friends will not stop throwing it",
        "author": "squeakysoup.games",
        "hours_ago": 30,
        "engagement": (3, 0, 2),
        "links": [],
        "tags": [],
        "thumb": "https://cdn.bsky.app/img/feed_thumbnail/plain/did:plc:squeakysoup3v7m2dqk5zxw4b/bafkreisq1@jpeg",
    },
    {
        "source_id": "did:plc:curatorbee2xq4kd7mzt3vh5n/3m2vzk3pk7d2t",
        "url": "https://bsky.app/profile/did:plc:curatorbee2xq4kd7mzt3vh5n/post/3m2vzk3pk7d2t",
        "title": "Friendslop pick of the week: Lantern Lads, a proximity-chat cave crawler where the only light "
        "source is whoever is",
        "author": "curatorbee.bsky.social",
        "hours_ago": 50,
        "engagement": (15, 4, 5),
        "links": ["https://lanternlads.itch.io/lantern-lads"],
        "tags": [],
        "thumb": "https://cdn.bsky.app/img/feed_thumbnail/plain/did:plc:curatorbee2xq4kd7mzt3vh5n/bafkreillthumb@jpeg",
    },
]


def test_search_fixture_parses_into_exact_mentions(mock):
    posts = {p["uri"]: p for p in fx("search_indiedev.json")["posts"]}
    mock.get(PDS_SEARCH).mock(side_effect=by_term({"friendslop": ok("search_indiedev.json")}))
    mock.post(CREATE).mock(return_value=ok("create_session.json"))

    mentions, _ = make_collector().run()

    assert len(mentions) == len(EXPECTED)  # opted-out, porn-labelled and 100h-old posts skipped
    for mention, want in zip(mentions, EXPECTED, strict=True):
        did, rkey = want["source_id"].split("/")
        uri = f"at://{did}/app.bsky.feed.post/{rkey}"
        assert mention.source == "bluesky" and mention.channel == "bluesky"
        assert mention.source_id == want["source_id"] and mention.key == f"bluesky:{want['source_id']}"
        assert mention.url == want["url"]
        assert mention.title == want["title"] and len(mention.title) <= 120
        assert mention.text == posts[uri]["record"]["text"]
        assert mention.author == want["author"] and mention.author_audience is None
        assert mention.created_at == NOW - timedelta(hours=want["hours_ago"])
        assert mention.engagement == Engagement(
            likes=want["engagement"][0], comments=want["engagement"][1], shares=want["engagement"][2]
        )
        assert mention.links == want["links"]
        assert mention.raw_tags == want["tags"]
        assert mention.media_thumb == want["thumb"]
        assert mention.extra == {"uri": uri, "cid": f"bafyreicid{rkey}", "did": did, "term": "friendslop"}


def test_search_request_parameters(mock):
    search = mock.get(PDS_SEARCH).mock(return_value=ok())
    mock.post(CREATE).mock(return_value=ok("create_session.json"))
    make_collector().run()
    params = search.calls[1].request.url.params
    assert dict(params) == {
        "q": '"proximity chat"',
        "sort": "latest",
        "limit": "50",
        "since": "2026-09-30T12:00:00Z",
        "lang": "en",
    }

    search.reset()
    make_collector(limit=500, lang=None, sort="top", max_post_age_hours=24).run()
    params = search.calls[0].request.url.params
    assert params["limit"] == "100" and "lang" not in params and params["sort"] == "top"
    assert params["since"] == "2026-10-02T12:00:00Z"


def test_extra_pages_follow_the_cursor(mock):
    search = mock.get(PDS_SEARCH).mock(side_effect=[ok("search_indiedev.json"), ok()])
    mock.post(CREATE).mock(return_value=ok("create_session.json"))
    mentions, report = make_collector(terms=["friendslop"], search_pages=3).run()
    assert search.call_count == 2 and len(mentions) == 5 and report.ok
    assert "cursor" not in search.calls[0].request.url.params
    assert search.calls[1].request.url.params["cursor"] == fx("search_indiedev.json")["cursor"]


def test_same_post_from_two_terms_is_kept_once_with_the_first_term(mock):
    mock.get(PDS_SEARCH).mock(return_value=ok("search_indiedev.json"))
    mock.post(CREATE).mock(return_value=ok("create_session.json"))
    collector = make_collector()
    mentions, _ = collector.run()
    assert len(mentions) == 5 and len(collector.found) == 5
    assert {m.extra["term"] for m in mentions} == {"friendslop"}


def test_one_bad_term_does_not_stop_the_others(mock):
    mock.post(CREATE).mock(return_value=ok("create_session.json"))
    mock.get(PDS_SEARCH).mock(
        side_effect=by_term(
            {"friendslop": err(400, {"error": "InvalidRequest", "message": "bad query"})},
            default=ok("search_indiedev.json"),
        )
    )
    mentions, report = make_collector().run()
    assert len(mentions) == 5 and report.ok
    assert report.failed_units == 1 and report.ok_units == 1
    assert "InvalidRequest: bad query" in report.errors[0]


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            httpx.Response(200, text="<html>oops</html>", headers={"content-type": "text/html"}),
            "invalid JSON",
        ),
        (httpx.Response(200, text="{not json"), "invalid JSON"),
        (httpx.Response(200, json={"posts": "nope"}), "no 'posts' list"),
        (httpx.Response(500, json={"error": "InternalServerError"}), "HTTP 500"),
    ],
)
def test_malformed_and_server_error_responses_are_recorded(mock, response, expected):
    mock.post(CREATE).mock(return_value=ok("create_session.json"))
    mock.get(PDS_SEARCH).mock(return_value=response)
    mentions, report = make_collector(terms=["friendslop"]).run()
    assert mentions == [] and expected in report.errors[0] and not report.ok


def test_a_post_that_fails_validation_is_skipped_not_the_whole_term(mock, monkeypatch):
    import gembot.collectors.bluesky as bluesky

    real = bluesky.parse_post

    def flaky(post, **kwargs):
        if post["uri"].endswith("3m2ykq7xfjc2s"):
            raise ValueError("odd post")
        return real(post, **kwargs)

    monkeypatch.setattr(bluesky, "parse_post", flaky)
    mock.post(CREATE).mock(return_value=ok("create_session.json"))
    mock.get(PDS_SEARCH).mock(return_value=ok("search_indiedev.json"))
    mentions, report = make_collector(terms=["friendslop"]).run()
    assert len(mentions) == 4 and report.ok and report.errors == []


def test_malformed_posts_are_skipped_one_by_one(mock):
    good = fx("search_indiedev.json")["posts"][1]
    weird = [
        "not a dict",
        {"uri": "at://did:plc:x/app.bsky.feed.like/abc"},
        {"uri": "at://did:plc:x/app.bsky.feed.post/abc", "record": {"createdAt": "yesterday-ish"}},
        {"uri": "at://did:plc:x/app.bsky.feed.post/abc", "record": {"text": 5, "createdAt": iso(NOW)}},
    ]
    mock.post(CREATE).mock(return_value=ok("create_session.json"))
    mock.get(PDS_SEARCH).mock(return_value=httpx.Response(200, json={"posts": [*weird, good]}))
    mentions, report = make_collector(terms=["friendslop"]).run()
    assert report.ok
    assert [m.source_id for m in mentions] == ["did:plc:x/abc", f"{GP}/3m2ykq7xfjc2s"]
    assert mentions[0].text == "5" and mentions[0].author == "did:plc:x"


def test_parse_post_edge_cases():
    base = fx("search_indiedev.json")["posts"][1]

    def variant(**changes):
        post = json.loads(json.dumps(base))
        for path, value in changes.items():
            target = post
            *parents, leaf = path.split("__")
            for key in parents:
                target = target[key]
            target[leaf] = value
        return parse_post(post, now=NOW, max_age_hours=72)

    # a future-dated createdAt cannot fake freshness: Bluesky's sortAt = min(createdAt, indexedAt)
    future = variant(record__createdAt="2030-01-01T00:00:00Z")
    assert future.created_at == datetime.fromisoformat(base["indexedAt"])
    # naive timestamps are UTC; a missing createdAt falls back to indexedAt
    assert variant(record__createdAt="2026-10-03T08:00:00").created_at == NOW - timedelta(hours=4)
    assert variant(record__createdAt=None) is not None
    assert variant(record__createdAt=None, indexedAt=None) is None
    # negated labels do not count; other blocked labels do
    neg = [{"val": "porn", "neg": True}]
    assert variant(labels=neg) is not None
    assert variant(labels=[{"val": "graphic-media"}]) is None
    assert variant(author__labels=[{"val": "!no-unauthenticated"}]) is None
    # handle.invalid -> fall back to the DID
    assert variant(author__handle="handle.invalid").author == GP
    # no embed -> no thumb; unknown embed type -> no thumb
    assert variant(embed=None).media_thumb is None
    assert variant(embed={"$type": "app.bsky.embed.record#view", "record": {}}).media_thumb is None
    assert variant(embed={"$type": "app.bsky.embed.images#view", "images": []}).media_thumb is None
    # shortened display links in text are ignored; real ones are cleaned of trailing punctuation
    text = "see https://example.com/very/lo... and (https://ok.example/path)."
    m = variant(record__text=text, record__facets=[], record__embed=None, embed=None)
    assert m.links == ["https://ok.example/path"]
    # long first line -> cut at a word boundary
    m = variant(record__text="word " * 40)
    assert len(m.title) <= 120 and not m.title.endswith(" ")
    m = variant(record__text="x" * 300)
    assert m.title == "x" * 120
    assert variant(record__text="\n\n  ").title == ""
    assert parse_post(None, now=NOW, max_age_hours=72) is None


def test_uri_helpers():
    assert parse_at_uri(GP_URI) == (GP, "3m2ykq7xfjc2s")
    for bad in (
        None,
        "",
        "https://bsky.app/x",
        f"at://{GP}/app.bsky.feed.post",
        "at:///app.bsky.feed.post/x",
    ):
        assert parse_at_uri(bad) is None
    assert post_url(GP, "abc") == f"https://bsky.app/profile/{GP}/post/abc"


# ---------------------------------------------------------------- rate limits / budget


def test_429_is_retried_by_the_http_layer(mock):
    slept: list[float] = []
    mock.post(CREATE).mock(return_value=ok("create_session.json"))
    mock.get(PDS_SEARCH).mock(
        side_effect=[
            httpx.Response(429, json={"error": "RateLimitExceeded", "message": "Rate Limit Exceeded"}),
            ok("search_indiedev.json"),
        ]
    )
    mentions, report = make_collector(terms=["friendslop"], slept=slept).run()
    assert len(mentions) == 5 and report.ok and slept == [2.0]


def test_persistent_429_stops_the_remaining_terms(mock):
    slept: list[float] = []
    mock.post(CREATE).mock(return_value=ok("create_session.json"))
    search = mock.get(PDS_SEARCH).mock(
        return_value=httpx.Response(
            429, json={"error": "RateLimitExceeded", "message": "Rate Limit Exceeded"}
        )
    )
    mentions, report = make_collector(slept=slept).run()
    assert mentions == [] and search.call_count == 3 and slept == [2.0, 4.0]
    assert len(report.errors) == 1 and "rate limited" in report.errors[0]


def test_budget_is_enforced_and_run_never_raises(mock):
    mock.post(CREATE).mock(return_value=ok("create_session.json"))
    search = mock.get(PDS_SEARCH).mock(return_value=ok("search_indiedev.json"))
    mentions, report = make_collector(budget=2).run()
    assert search.call_count == 1 and len(mentions) == 5
    assert report.requests == 2 and any("budget" in w for w in report.warnings)


def test_empty_budget_does_not_count_a_login_attempt(mock):
    state = State()
    create = mock.post(CREATE).mock(return_value=ok("create_session.json"))
    mentions, report = make_collector(state=state, budget=0).run()
    assert mentions == [] and not create.called
    assert scratch(state).get("created", []) == []
    assert any("budget" in w for w in report.warnings)


def test_empty_search_results(mock):
    mock.post(CREATE).mock(return_value=ok("create_session.json"))
    mock.get(PDS_SEARCH).mock(return_value=ok("search_empty.json"))
    mentions, report = make_collector().run()
    assert mentions == [] and report.ok and report.ok_units == 2 and report.errors == []


# ---------------------------------------------------------------- replies + audience


def _gp_mention(with_extra: bool = True):
    extra = {"uri": GP_URI, "did": GP} if with_extra else {}
    return make_mention("bluesky", f"{GP}/3m2ykq7xfjc2s", author="gorillapizza.bsky.social", extra=extra)


@pytest.mark.parametrize("with_extra", [True, False])
def test_fetch_comments_reads_the_public_thread_without_a_token(mock, with_extra):
    state = State()
    seed_session(state, access_exp=NOW + timedelta(hours=1))  # logged in, but replies never use the token
    thread = mock.get(THREAD).mock(return_value=ok("thread.json"))
    collector = make_collector(state=state)

    comments = collector.fetch_comments(_gp_mention(with_extra), 10)

    assert no_token(thread)
    assert dict(thread.calls[0].request.url.params) == {"uri": GP_URI, "depth": "1", "parentHeight": "0"}
    # notFound / blocked / opted-out replies skipped; most liked first
    assert [(c.id, c.author, c.score) for c in comments] == [
        ("3m2yl3def2k2b", "lagspike.bsky.social", 9),
        ("3m2yl2abc2k2a", "pixelpal.bsky.social", 5),
    ]
    assert comments[0].text == "looks like a roblox game lol. day one though"
    assert comments[1].created_at == NOW - timedelta(hours=2.5)
    assert [c.id for c in collector.fetch_comments(_gp_mention(), 1)] == ["3m2yl3def2k2b"]


def test_fetch_comments_edge_cases(mock):
    collector = make_collector()
    route = mock.get(THREAD).mock(return_value=err(400, {"error": "NotFound", "message": "Post not found"}))
    assert collector.fetch_comments(_gp_mention(), 10) == []
    assert collector.fetch_comments(_gp_mention(), 0) == []
    assert collector.fetch_comments(make_mention("bluesky", "not-a-did"), 10) == []
    route.mock(
        return_value=httpx.Response(200, json={"thread": {"$type": "app.bsky.feed.defs#notFoundPost"}})
    )
    assert collector.fetch_comments(_gp_mention(), 10) == []
    route.mock(return_value=err(400, {"error": "InvalidRequest"}))
    assert collector.safe_fetch_comments(_gp_mention(), 10) == []
    route.mock(return_value=httpx.Response(500))
    assert collector.safe_fetch_comments(_gp_mention(), 10) == []
    assert len(collector.report.warnings) == 2 and "InvalidRequest" in collector.report.warnings[0]
    assert route.call_count == 1 + 1 + 1 + 3


def test_fetch_audience_reads_the_public_profile_without_a_token(mock):
    profile = mock.get(PROFILE).mock(return_value=ok("profile.json"))
    collector = make_collector()
    assert collector.fetch_audience(_gp_mention()) == 812
    assert collector.fetch_audience(_gp_mention(with_extra=False)) == 812
    assert no_token(profile) and profile.calls[0].request.url.params["actor"] == GP
    assert collector.fetch_audience(make_mention("bluesky", "nodid/abc")) is None
    profile.mock(return_value=httpx.Response(200, json={"did": GP}))
    assert collector.fetch_audience(_gp_mention()) is None
    profile.mock(return_value=err(400, {"error": "InvalidRequest", "message": "Profile not found"}))
    assert collector.safe_fetch_audience(_gp_mention()) is None
    assert collector.report.warnings and collector.report.requests == 4


def test_fetch_audiences_batches_25_authors_per_request(mock):
    def profiles(request: httpx.Request) -> httpx.Response:
        actors = request.url.params.get_list("actors")
        return httpx.Response(
            200,
            json={
                "profiles": [{"did": d, "followersCount": int(d.rsplit("x", 1)[1])} for d in actors]
                + [{"did": "did:plc:unknown", "followersCount": 1}, {"did": actors[0]}]
            },
        )

    route = mock.get(PROFILES).mock(side_effect=profiles)
    mentions = [make_mention("bluesky", f"did:plc:x{i}/post{i}") for i in range(27)]
    mentions.append(make_mention("bluesky", "did:plc:x3/another"))  # same author twice
    mentions.append(make_mention("reddit", "did:plc:x99/nope"))  # not Bluesky

    collector = make_collector()
    assert collector.fetch_audiences(mentions) == 28
    assert route.call_count == 2 and no_token(route)
    assert len(route.calls[0].request.url.params.get_list("actors")) == 25
    assert route.calls[1].request.url.params.get_list("actors") == ["did:plc:x25", "did:plc:x26"]
    assert mentions[3].author_audience == 3 and mentions[27].author_audience == 3
    assert mentions[28].author_audience is None
    assert collector.report.requests == 2


def test_fetch_audiences_never_raises(mock):
    route = mock.get(PROFILES).mock(return_value=httpx.Response(500))
    mentions = [make_mention("bluesky", f"did:plc:x{i}/p") for i in range(60)]
    collector = make_collector(budget=4)
    assert collector.fetch_audiences(mentions) == 0  # batch 1: 3 attempts; batch 2: 1 attempt, then budget
    assert route.call_count == 4
    assert any("HTTP 500" in w for w in collector.report.warnings)
    assert any("budget" in w for w in collector.report.warnings)
