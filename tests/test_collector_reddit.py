"""Reddit collector: OAuth multireddit listings + comments, anonymous RSS fallback."""

from __future__ import annotations

import base64
import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx

from gembot.collectors.base import CollectContext
from gembot.collectors.reddit import (
    BAD_CREDENTIALS,
    IP_BLOCKED,
    MODE_ANONYMOUS,
    MODE_OAUTH,
    RSS_RATE_LIMITED,
    RedditCollector,
    _clean_links,
    _html_to_text,
    _text_urls,
    parse_comments,
)
from gembot.http import Budget
from gembot.models import Engagement, Mention, State
from tests.factories import NOW, make_config, make_http, make_mention, read_fixture

OAUTH_ENV = {"REDDIT_CLIENT_ID": "cid", "REDDIT_CLIENT_SECRET": "csecret", "REDDIT_USERNAME": "gem_owner"}
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
OAUTH = "oauth.reddit.com"
UA = "python:gembot:0.1.0 (by /u/gem_owner)"
SUBS = make_config().sources.reddit.subreddits
MULTI = "+".join(SUBS)


def fx(name: str) -> str:
    return read_fixture("reddit", name)


def fx_json(name: str):
    return json.loads(fx(name))


def make_collector(env=None, *, now=NOW, budget=None, state=None, sleep=None, **reddit) -> RedditCollector:
    config = make_config(env)
    for key, value in reddit.items():
        setattr(config.sources.reddit, key, value)
    http = make_http(sleep=sleep) if sleep else make_http()
    return RedditCollector(CollectContext(config=config, http=http, now=now, state=state), budget=budget)


def oauth_collector(**kwargs) -> RedditCollector:
    return make_collector(OAUTH_ENV, **kwargs)


def token_route(*responses: httpx.Response):
    route = respx.post(TOKEN_URL)
    if responses:
        return route.mock(side_effect=list(responses))
    return route.mock(return_value=httpx.Response(200, text=fx("token.json")))


def listing_route(listing: str, multi: str = MULTI):
    return respx.get(host=OAUTH, path=f"/r/{multi}/{listing}")


def ok_json(name: str, **headers: str) -> httpx.Response:
    return httpx.Response(200, text=fx(name), headers={"content-type": "application/json", **headers})


def blocked() -> httpx.Response:
    return httpx.Response(403, text=fx("blocked.html"), headers={"content-type": "text/html; charset=utf-8"})


def rss_route(multi: str = MULTI):
    return respx.get(host="www.reddit.com", path=f"/r/{multi}/new/.rss")


def post(post_id: str, created: datetime, **extra) -> dict:
    data = {
        "id": post_id,
        "name": f"t3_{post_id}",
        "title": f"Post {post_id}",
        "selftext": "",
        "author": "someone",
        "subreddit": "IndieDev",
        "subreddit_subscribers": 263114,
        "created_utc": created.timestamp(),
        "score": 1,
        "num_comments": 0,
        "upvote_ratio": 1.0,
        "url": f"https://www.reddit.com/r/IndieDev/comments/{post_id}/x/",
        "permalink": f"/r/IndieDev/comments/{post_id}/x/",
        "is_self": True,
        "domain": "self.IndieDev",
    }
    data.update(extra)
    return {"kind": "t3", "data": data}


def listing(*children: dict, after: str | None = None) -> httpx.Response:
    return httpx.Response(200, json={"kind": "Listing", "data": {"after": after, "children": list(children)}})


# ---------------------------------------------------------------- mode decision


def test_mode_and_enabled_flags():
    assert oauth_collector().mode == MODE_OAUTH
    anon = make_collector()
    assert anon.mode == MODE_ANONYMOUS and anon.enabled() == (True, None)
    assert make_collector(enabled=False).enabled() == (False, "disabled in sources.yaml")
    off, reason = make_collector(subreddits=[]).enabled()
    assert not off and "no subreddits" in reason


# ---------------------------------------------------------------- OAuth listings


@respx.mock
def test_oauth_end_to_end_requests_and_mentions():
    token = token_route()
    new = listing_route("new").mock(return_value=ok_json("listing_new.json"))
    rising = listing_route("rising").mock(return_value=ok_json("listing_rising.json"))

    mentions, report = oauth_collector().run()

    # token request: HTTP Basic client_id:secret, client_credentials, Reddit-style UA
    req = token.calls.last.request
    assert req.headers["authorization"] == "Basic " + base64.b64encode(b"cid:csecret").decode()
    assert req.content == b"grant_type=client_credentials"
    assert req.headers["user-agent"] == UA
    # one multireddit request per listing, bearer token, raw_json
    for route, name in ((new, "new"), (rising, "rising")):
        assert route.call_count == 1
        lreq = route.calls.last.request
        assert lreq.url.path == f"/r/{MULTI}/{name}" and "+" in lreq.url.path
        assert dict(lreq.url.params) == {"limit": "50", "raw_json": "1"}
        assert lreq.headers["authorization"] == "bearer test-token-abc123"
        assert lreq.headers["user-agent"] == UA

    assert [m.source_id for m in mentions] == [
        "1xa1b2c",
        "1xa0abc",
        "1xa0zz9",
        "1x9zz2z",
        "1x9zq3k",
        "1xa1h3y",
        "1xa1x99",
    ]
    assert report.ok and report.errors == [] and report.warnings == []
    assert report.requests == 3 and report.mentions == 7 and report.ok_units == 2

    bellhop = mentions[0]
    assert bellhop == Mention(
        source="reddit",
        source_id="1xa1b2c",
        url="https://www.reddit.com/r/IndieDev/comments/1xa1b2c/we_just_announced_our_coop_horror_game_bellhop/",
        title="We just announced our co-op horror game BELLHOP PANIC - proximity chat, ragdolls, the lot",
        text=fx_json("listing_new.json")["data"]["children"][0]["data"]["selftext"],
        author="bellhop_studio",
        author_audience=263114,
        created_at=datetime(2026, 10, 3, 10, 0, tzinfo=UTC),
        engagement=Engagement(likes=160, comments=25, ratio=0.95),  # fresher numbers from /rising
        links=[
            "https://store.steampowered.com/app/3456780/Bellhop_Panic/",
            "https://www.youtube.com/watch?v=abc123XYZ_0",
            "https://bellhop.example.com/devlog",
        ],
        media_thumb="https://preview.redd.it/bellhop-gif.gif?width=640&format=png&auto=webp&s=9f8e7d",
        raw_tags=["Promotion"],
        channel="r/IndieDev",
        extra={
            "name": "t3_1xa1b2c",
            "is_self": True,
            "domain": "self.IndieDev",
            "is_video": False,
            "listings": ["new", "rising"],
        },
    )


@respx.mock
def test_oauth_listing_parsing_details():
    token_route()
    listing_route("new").mock(return_value=ok_json("listing_new.json"))
    listing_route("rising").mock(return_value=ok_json("listing_rising.json"))
    by_id = {m.source_id: m for m in oauth_collector().run()[0]}

    # stickied mod post, NSFW post and the 4-day-old post are skipped
    assert {"1x5mod1", "1xa0nsf", "1x7old1"}.isdisjoint(by_id)

    steam = by_id["1xa0abc"]
    assert steam.links == ["https://store.steampowered.com/app/2988000/Gorilla_Pizza_Panic/"]
    assert steam.media_thumb == "https://b.thumbs.redditmedia.com/gpp_thumb.jpg"  # no preview -> thumbnail
    assert steam.raw_tags == [] and steam.text == ""
    assert steam.engagement == Engagement(likes=41, comments=9, ratio=0.88)
    assert steam.channel == "r/IndieGaming" and steam.author_audience == 390512

    itch = by_id["1xa0zz9"]
    assert itch.links == ["https://raccoondev.itch.io/raccoon-rumble"]
    assert itch.media_thumb is None  # thumbnail "default" is not a URL
    assert itch.created_at == datetime(2026, 10, 3, 8, 30, tzinfo=UTC)

    help_post = by_id["1x9zz2z"]  # escaped underscore and closing parenthesis are cleaned
    assert help_post.links == ["https://docs.godotengine.org/en/stable/classes/class_characterbody3d.html"]
    assert help_post.raw_tags == ["help me"] and help_post.channel == "r/godot"

    video = by_id["1x9zq3k"]  # v.redd.it is Reddit's own media host: not an outbound link
    assert video.links == []
    assert (
        video.media_thumb
        == "https://external-preview.redd.it/fox.png?width=1280&format=png&auto=webp&s=0f1e2d"
    )
    assert video.extra["is_video"] is True and video.extra["domain"] == "v.redd.it"

    assert by_id["1xa1h3y"].links == ["https://discord.gg/heistcrew"]
    assert by_id["1xa1h3y"].extra["listings"] == ["rising"]
    assert by_id["1xa1x99"].links == []  # crosspost: relative Reddit URL
    assert by_id["1xa1x99"].media_thumb is None  # "spoiler"


@respx.mock
def test_post_in_new_and_rising_becomes_one_mention():
    token_route()
    listing_route("new").mock(return_value=ok_json("listing_new.json"))
    listing_route("rising").mock(return_value=ok_json("listing_rising.json"))
    collector = oauth_collector()
    mentions, _report = collector.run()
    keys = [m.key for m in mentions]
    assert keys.count("reddit:1xa1b2c") == 1 and len(keys) == len(set(keys))
    assert len(collector.found) == len(mentions)  # deduped while collecting, not only by run()


@respx.mock
def test_token_401_is_a_credentials_error():
    token = token_route(httpx.Response(401, json={"message": "Unauthorized", "error": 401}))
    collector = oauth_collector()
    mentions, report = collector.run()
    assert mentions == [] and not report.ok
    assert report.errors == [f"reddit token: {BAD_CREDENTIALS} (HTTP 401)"]
    assert token.call_count == 1  # no listing requests (respx would fail on unmocked calls)
    # enrichment does not retry broken credentials
    assert collector.fetch_comments(make_mention("reddit", "1xa1b2c"), 10) == []
    assert token.call_count == 1


@respx.mock
def test_token_200_with_error_field_is_a_credentials_error():
    token_route(httpx.Response(200, json={"error": "invalid_grant"}))
    _mentions, report = oauth_collector().run()
    assert report.errors == [f"reddit token: {BAD_CREDENTIALS} (invalid_grant)"]


@respx.mock
def test_token_without_access_token_is_reported():
    token_route(httpx.Response(200, json={"token_type": "bearer"}))
    _mentions, report = oauth_collector().run()
    assert "no access_token" in report.errors[0]


@respx.mock
def test_token_403_html_is_an_ip_block_not_credentials():
    token_route(blocked())
    collector = oauth_collector()
    _mentions, report = collector.run()
    assert report.errors == [f"reddit token: {IP_BLOCKED}"]
    assert "not a credentials problem" in report.errors[0]
    assert collector.fetch_comments(make_mention("reddit", "x"), 10) == []


@respx.mock
def test_token_403_json_means_app_not_approved():
    token_route(httpx.Response(403, json={"message": "Forbidden", "error": 403}))
    _mentions, report = oauth_collector().run()
    assert "not be approved" in report.errors[0]


@respx.mock
def test_listing_ip_block_stops_further_requests():
    token_route()
    new = listing_route("new").mock(return_value=blocked())
    rising = listing_route("rising").mock(return_value=ok_json("listing_rising.json"))
    collector = oauth_collector()
    mentions, report = collector.run()
    assert mentions == [] and not report.ok
    assert report.errors == [f"reddit/new: {IP_BLOCKED}"]
    assert new.call_count == 1 and rising.call_count == 0
    assert collector.fetch_comments(make_mention("reddit", "x"), 10) == []


@respx.mock
def test_listing_401_refreshes_the_token_and_retries_once():
    token = token_route()
    new = listing_route("new").mock(
        side_effect=[
            httpx.Response(401, json={"message": "Unauthorized", "error": 401}),
            ok_json("listing_new.json"),
        ]
    )
    listing_route("rising").mock(return_value=ok_json("listing_rising.json"))
    mentions, report = oauth_collector().run()
    assert token.call_count == 2 and new.call_count == 2
    assert report.ok and report.errors == [] and len(mentions) == 7


@respx.mock
def test_listing_401_twice_is_an_error_for_that_listing_only():
    token = token_route()
    listing_route("new").mock(return_value=httpx.Response(401, json={"error": 401}))
    listing_route("rising").mock(return_value=ok_json("listing_rising.json"))
    mentions, report = oauth_collector().run()
    assert token.call_count == 2  # first token + one refresh after /new 401; /rising reuses it
    assert len(report.errors) == 1 and "401 even with a fresh token" in report.errors[0]
    assert {m.source_id for m in mentions} == {"1xa1b2c", "1xa1h3y", "1xa1x99"}
    assert report.ok  # one listing still worked


@respx.mock
def test_low_ratelimit_remaining_stops_further_requests():
    token_route()
    listing_route("new").mock(
        return_value=ok_json(
            "listing_new.json", **{"x-ratelimit-remaining": "3.0", "x-ratelimit-reset": "240"}
        )
    )
    rising = listing_route("rising").mock(return_value=ok_json("listing_rising.json"))
    collector = oauth_collector()
    mentions, report = collector.run()
    assert rising.call_count == 0
    assert len(mentions) == 5 and report.errors == []
    assert len(report.warnings) == 1
    assert "rate limit" in report.warnings[0] and "3 requests left" in report.warnings[0]
    assert "240s" in report.warnings[0]
    assert collector.fetch_comments(mentions[0], 10) == []  # no request (respx would fail)


@respx.mock
def test_healthy_ratelimit_headers_do_not_stop():
    token_route()
    listing_route("new").mock(return_value=ok_json("listing_new.json", **{"x-ratelimit-remaining": "97.0"}))
    rising = listing_route("rising").mock(
        return_value=ok_json("listing_rising.json", **{"x-ratelimit-remaining": "n/a"})
    )
    _mentions, report = oauth_collector().run()
    assert rising.call_count == 1 and report.warnings == []


@respx.mock
def test_new_is_paged_while_a_full_page_is_all_unseen_posts():
    token_route()
    fresh = [post(f"p{i}", NOW - timedelta(minutes=10 * (i + 1))) for i in range(6)]
    new = listing_route("new", "IndieDev").mock(
        side_effect=[listing(*fresh[:2], after="t3_p1"), listing(*fresh[2:4], after="t3_p3")]
    )
    listing_route("rising", "IndieDev").mock(return_value=listing())
    state = State()
    collector = oauth_collector(subreddits=["IndieDev"], limit=2, state=state)
    mentions, _report = collector.run()
    assert new.call_count == 2  # capped by new_max_pages=2 even though page 2 was full too
    assert "after" not in new.calls[0].request.url.params
    assert new.calls[1].request.url.params["after"] == "t3_p1"
    assert [m.source_id for m in mentions] == ["p0", "p1", "p2", "p3"]
    newest = state.meta.collector_state["reddit"]["newest_created_utc"]
    assert newest == fresh[0]["data"]["created_utc"]


@respx.mock
def test_new_is_not_paged_when_page_reaches_posts_seen_last_run():
    token_route()
    state = State()
    state.meta.collector_state["reddit"] = {"newest_created_utc": (NOW - timedelta(minutes=15)).timestamp()}
    page = [post("a", NOW - timedelta(minutes=5)), post("b", NOW - timedelta(minutes=20))]
    new = listing_route("new", "IndieDev").mock(return_value=listing(*page, after="t3_b"))
    listing_route("rising", "IndieDev").mock(return_value=listing())
    oauth_collector(subreddits=["IndieDev"], limit=2, state=state).run()
    assert new.call_count == 1


@respx.mock
def test_private_subreddit_in_multireddit_falls_back_and_is_excluded():
    token_route()
    private = httpx.Response(403, text=fx("private.json"), headers={"content-type": "application/json"})
    listing_route("new", "IndieDev+SecretSub+godot").mock(return_value=private)
    listing_route("new", "IndieDev").mock(return_value=listing(post("a1", NOW - timedelta(hours=1))))
    listing_route("new", "SecretSub").mock(return_value=private)
    listing_route("new", "godot").mock(
        return_value=listing(
            post("g1", NOW - timedelta(hours=2), subreddit="godot", permalink="/r/godot/comments/g1/x/")
        )
    )
    rising = listing_route("rising", "IndieDev+godot").mock(return_value=listing())
    state = State()
    mentions, report = oauth_collector(subreddits=["IndieDev", "SecretSub", "godot"], state=state).run()
    assert [m.source_id for m in mentions] == ["a1", "g1"]
    assert rising.call_count == 1  # the private subreddit is left out of later requests
    assert report.errors == [] and report.ok
    assert any("checking the subreddits one by one" in w for w in report.warnings)
    assert any(w.startswith("r/SecretSub is unavailable") and "private" in w for w in report.warnings)
    until = state.meta.collector_state["reddit"]["excluded"]["secretsub"]
    assert datetime.fromisoformat(until) == NOW + timedelta(hours=24)


@respx.mock
def test_excluded_subreddits_expire():
    token_route()
    state = State()
    state.meta.collector_state["reddit"] = {
        "excluded": {
            "godot": (NOW + timedelta(hours=1)).isoformat(),
            "indiedev": (NOW - timedelta(hours=1)).isoformat(),
            "x": "garbage",
        }
    }
    new = listing_route("new", "IndieDev").mock(return_value=listing())
    listing_route("rising", "IndieDev").mock(return_value=listing())
    oauth_collector(subreddits=["IndieDev", "r/godot", "indiedev"], state=state).run()
    assert new.call_count == 1
    assert state.meta.collector_state["reddit"]["excluded"] == {
        "godot": (NOW + timedelta(hours=1)).isoformat()
    }


@respx.mock
def test_rate_limit_during_per_subreddit_fallback_stops_the_loop():
    token_route()
    private = httpx.Response(403, json={"reason": "private", "message": "Forbidden", "error": 403})
    listing_route("new", "IndieDev+godot").mock(return_value=private)
    listing_route("new", "IndieDev").mock(
        return_value=httpx.Response(
            200,
            json={"kind": "Listing", "data": {"children": [post("a1", NOW - timedelta(hours=1))]}},
            headers={"x-ratelimit-remaining": "1"},
        )
    )
    godot = listing_route("new", "godot").mock(return_value=listing())
    mentions, report = oauth_collector(subreddits=["IndieDev", "godot"]).run()
    assert [m.source_id for m in mentions] == ["a1"]
    assert godot.call_count == 0  # /rising is not requested either (respx would fail)
    assert any("rate limit" in w for w in report.warnings)


@respx.mock
def test_missing_subreddit_redirect_is_excluded_with_a_warning():
    token_route()
    listing_route("new", "Nope").mock(
        return_value=httpx.Response(
            302, headers={"location": "https://www.reddit.com/subreddits/search?q=Nope"}
        )
    )
    _mentions, report = oauth_collector(subreddits=["Nope"]).run()
    assert report.errors == []
    assert any("r/Nope is unavailable" in w and "redirected" in w for w in report.warnings)
    assert any("every configured subreddit is unavailable" in w for w in report.warnings)  # /rising skipped


@respx.mock
def test_banned_subreddit_404_in_fallback():
    token_route()
    banned = httpx.Response(404, json={"reason": "banned", "message": "Not Found", "error": 404})
    listing_route("new", "IndieDev+Gone").mock(return_value=banned)
    listing_route("new", "IndieDev").mock(return_value=listing())
    listing_route("new", "Gone").mock(return_value=banned)
    listing_route("rising", "IndieDev").mock(return_value=listing())
    _mentions, report = oauth_collector(subreddits=["IndieDev", "Gone"]).run()
    assert any("r/Gone is unavailable" in w and "banned" in w for w in report.warnings)


@respx.mock
def test_other_api_errors_and_bad_payloads_are_recorded_not_raised():
    token_route()
    listing_route("new").mock(return_value=httpx.Response(403, json={"message": "Forbidden", "error": 403}))
    listing_route("rising").mock(return_value=httpx.Response(200, text="<html>not json</html>"))
    mentions, report = oauth_collector().run()
    assert mentions == [] and not report.ok
    assert "HTTP 403" in report.errors[0]
    assert "invalid JSON" in report.errors[1]


@respx.mock
def test_unexpected_listing_shape_and_malformed_posts():
    token_route()
    listing_route("new").mock(return_value=httpx.Response(200, json={"kind": "Listing", "data": {}}))
    weird = {"kind": "t3", "data": {"id": "bad", "created_utc": NOW.timestamp(), "score": "lots"}}
    no_id = {"kind": "t3", "data": {"created_utc": NOW.timestamp(), "title": "no id"}}
    odd_preview = post(
        "ok1",
        NOW - timedelta(hours=1),
        preview={"images": [{"source": {}}]},
        thumbnail="https://b.thumbs.redditmedia.com/ok1.jpg",
        author="[deleted]",
        upvote_ratio=None,
    )
    listing_route("rising").mock(return_value=listing(weird, no_id, odd_preview, {"kind": "t1", "data": {}}))
    mentions, report = oauth_collector().run()
    assert [m.source_id for m in mentions] == ["ok1"]
    ok1 = mentions[0]
    assert ok1.media_thumb == "https://b.thumbs.redditmedia.com/ok1.jpg"
    assert ok1.author is None and ok1.engagement.ratio is None
    assert "unexpected Reddit listing shape" in report.errors[0]
    assert report.warnings == ["reddit/rising: skipped 1 malformed post"]


@respx.mock
def test_transport_errors_never_crash_the_run():
    token_route()
    listing_route("new").mock(side_effect=httpx.ConnectError("boom"))
    listing_route("rising").mock(side_effect=httpx.ReadTimeout("slow"))
    mentions, report = oauth_collector().run()
    assert mentions == [] and len(report.errors) == 2 and not report.ok


@respx.mock
def test_budget_is_enforced_and_partial_results_kept():
    token = token_route()
    listing_route("new").mock(return_value=ok_json("listing_new.json"))
    rising = listing_route("rising").mock(return_value=ok_json("listing_rising.json"))
    mentions, report = oauth_collector(budget=Budget("reddit", 2)).run()
    assert token.call_count == 1 and rising.call_count == 0
    assert len(mentions) == 5 and report.requests == 2
    assert any("budget" in w for w in report.warnings)


# ---------------------------------------------------------------- comments


@respx.mock
def test_fetch_comments_parses_top_level_comments():
    token_route()
    route = respx.get(f"https://{OAUTH}/comments/1xa1b2c").mock(return_value=ok_json("comments.json"))
    collector = oauth_collector()
    comments = collector.fetch_comments(make_mention("reddit", "1xa1b2c"), 100)
    params = dict(route.calls.last.request.url.params)
    assert params == {"sort": "top", "limit": "100", "depth": "1", "raw_json": "1"}
    assert route.calls.last.request.headers["authorization"] == "bearer test-token-abc123"
    assert [c.author for c in comments] == [
        "AutoModerator",
        "mossyknight",
        "brickfan99",
        "coop_enjoyer",
        "bellhop_studio",
        "lego_lad",
    ]  # [deleted], [removed] and the "more" stub are skipped
    assert [c.is_bot for c in comments] == [True, False, False, False, False, False]
    first, wish = comments[0], comments[1]
    assert first.id == "nq8x2ab" and first.score == 1
    assert wish.text == "Wishlisted! Me and the boys are going to lose our minds in this."
    assert wish.score == 41 and wish.created_at == datetime.fromtimestamp(1791022800, UTC)
    assert comments[3].text == "When does the demo drop? Need this for my friend group & me"


@respx.mock
def test_fetch_comments_respects_the_limit_and_reuses_the_token():
    token = token_route()
    route = respx.get(f"https://{OAUTH}/comments/1xa1b2c").mock(return_value=ok_json("comments.json"))
    collector = oauth_collector()
    mention = make_mention("reddit", "1xa1b2c")
    assert len(collector.fetch_comments(mention, 3)) == 3
    assert route.calls.last.request.url.params["limit"] == "3"
    assert len(collector.fetch_comments(mention, 1000)) == 6
    assert route.calls.last.request.url.params["limit"] == "500"
    assert token.call_count == 1
    assert collector.fetch_comments(mention, 0) == []
    assert collector.fetch_comments(make_mention("bluesky", "1xa1b2c"), 10) == []


def test_moderator_sticky_counts_as_bot_and_bad_payloads_raise():
    payload = [
        {"kind": "Listing", "data": {"children": []}},
        {
            "kind": "Listing",
            "data": {
                "children": [
                    {
                        "kind": "t1",
                        "data": {
                            "id": "m",
                            "author": "mod_jane",
                            "body": "Rules",
                            "distinguished": "moderator",
                            "stickied": True,
                        },
                    },
                    {
                        "kind": "t1",
                        "data": {
                            "id": "d",
                            "author": "mod_jane",
                            "body": "Just a mod chatting",
                            "distinguished": "moderator",
                        },
                    },
                    {"kind": "t1", "data": {"id": "e", "author": "ghost", "body": "   "}},
                ]
            },
        },
    ]
    comments = parse_comments(payload, 10)
    assert [(c.id, c.is_bot) for c in comments] == [("m", True), ("d", False)]
    assert comments[0].created_at is None
    with pytest.raises(ValueError):
        parse_comments({"not": "a list"}, 10)


@respx.mock
def test_safe_fetch_comments_turns_errors_into_warnings():
    token_route()
    respx.get(f"https://{OAUTH}/comments/gone").mock(return_value=httpx.Response(404, json={"error": 404}))
    collector = oauth_collector()
    assert collector.safe_fetch_comments(make_mention("reddit", "gone"), 10) == []
    assert "comments for reddit:gone" in collector.report.warnings[0]
    assert collector.report.requests == 2


@respx.mock
def test_token_429_stops_reddit_for_the_run():
    token = token_route(httpx.Response(429, headers={"Retry-After": "600"}))
    comments = respx.get(f"https://{OAUTH}/comments/abc").mock(return_value=ok_json("comments.json"))
    collector = oauth_collector()
    mentions, report = collector.run()
    assert collector.safe_fetch_comments(make_mention("reddit", "abc"), 10) == []
    assert mentions == [] and token.call_count == 1 and comments.call_count == 0
    assert any("429" in w and "no more Reddit requests this run" in w for w in report.warnings)


# ---------------------------------------------------------------- anonymous RSS


@respx.mock
def test_rss_mode_makes_exactly_one_request_and_parses_entries():
    route = rss_route().mock(
        return_value=httpx.Response(
            200, text=fx("feed_new.rss"), headers={"content-type": "application/atom+xml; charset=UTF-8"}
        )
    )
    collector = make_collector()
    mentions, report = collector.run()

    assert len(respx.calls) == 1 and route.call_count == 1
    req = route.calls.last.request
    assert dict(req.url.params) == {"limit": "100"}
    assert req.headers["user-agent"] == "python:gembot:0.1.0 (by /u/gembot)"
    assert report.ok and report.errors == [] and report.requests == 1
    assert report.warnings == [
        "reddit: using anonymous RSS (no engagement numbers; Reddit retires RSS on 2026-11-13)"
        " — add REDDIT_CLIENT_ID/SECRET for full data"
    ]
    # t5_ entry and the old post are skipped
    assert [m.source_id for m in mentions] == ["1xa1b2c", "1xa0zz9", "1x9zq3k"]

    bellhop, raccoon, video = mentions
    assert bellhop == Mention(
        source="reddit",
        source_id="1xa1b2c",
        url="https://www.reddit.com/r/IndieDev/comments/1xa1b2c/we_just_announced_our_coop_horror_game_bellhop/",
        title="We just announced our co-op horror game BELLHOP PANIC - proximity chat, ragdolls, the lot",
        text=(
            "Hey all! After 14 months we finally announced Bellhop Panic, a 1-4 player co-op horror game"
            " with proximity chat & ragdolls.\n"
            "Wishlist on Steam: https://store.steampowered.com/app/3456780/Bellhop_Panic/\n"
            "Trailer: here (feedback welcome!)\n"
            "Devlog at https://bellhop.example.com/devlog."
        ),
        author="bellhop_studio",
        author_audience=263000,  # fallback_subscribers from sources.yaml
        created_at=datetime(2026, 10, 3, 10, 0, tzinfo=UTC),
        engagement=Engagement(),
        links=[
            "https://store.steampowered.com/app/3456780/Bellhop_Panic/",
            "https://www.youtube.com/watch?v=abc123XYZ_0",
            "https://bellhop.example.com/devlog",
        ],
        media_thumb="https://b.thumbs.redditmedia.com/bellhop_thumb.jpg",
        channel="r/IndieDev",
        extra={
            "name": "t3_1xa1b2c",
            "is_self": True,
            "domain": "self.IndieDev",
            "engagement_known": False,
            "listings": ["new"],
        },
    )
    assert raccoon.links == ["https://raccoondev.itch.io/raccoon-rumble"]  # the [link] target
    assert raccoon.text == "" and raccoon.extra["is_self"] is False
    assert raccoon.extra["domain"] == "raccoondev.itch.io" and raccoon.channel == "r/playmygame"
    assert raccoon.media_thumb == "https://external-preview.redd.it/raccoon.png?width=640&crop=smart&s=abc"
    assert video.links == [] and video.media_thumb is None and video.extra["domain"] == "v.redd.it"
    assert all(m.extra["engagement_known"] is False and m.engagement.total == 0 for m in mentions)


@respx.mock
def test_rss_429_retries_once_then_warns_without_crashing():
    route = rss_route().mock(return_value=httpx.Response(429, headers={"Retry-After": "7"}))
    slept: list[float] = []
    mentions, report = make_collector(sleep=slept.append).run()
    assert route.call_count == 2 and slept == [7.0]
    assert mentions == [] and report.errors == []
    assert report.warnings[-1].startswith(RSS_RATE_LIMITED)
    assert report.failed_units == 1 and report.requests == 2


@respx.mock
def test_rss_429_then_success():
    rss_route().mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(200, text=fx("feed_new.rss")),
        ]
    )
    mentions, report = make_collector().run()
    assert len(mentions) == 3 and report.ok_units == 1 and report.requests == 2


@respx.mock
def test_rss_ip_block_is_reported_as_block():
    rss_route().mock(return_value=blocked())
    mentions, report = make_collector().run()
    assert mentions == [] and not report.ok
    assert report.errors == [f"reddit RSS: {IP_BLOCKED}"]


@respx.mock
@pytest.mark.parametrize(
    ("response", "needle"),
    [
        (
            httpx.Response(404, text="<html>nope</html>", headers={"content-type": "text/html"}),
            "may be private",
        ),
        (httpx.Response(200, text="<!DOCTYPE html><html>search</html>"), "not an RSS feed"),
        (
            httpx.Response(200, text="not a feed at all", headers={"content-type": "application/atom+xml"}),
            "malformed RSS",
        ),
        (httpx.Response(500, text="oops"), "HTTP 500"),
        (  # not followed: the search page would be a 2nd RSS request this minute
            httpx.Response(302, headers={"location": "https://www.reddit.com/subreddits/search.rss?q=x"}),
            "redirected to search (a subreddit in sources.yaml may be private",
        ),
    ],
)
def test_rss_errors_are_recorded(response, needle):
    rss_route().mock(return_value=response)
    mentions, report = make_collector().run()
    assert mentions == [] and not report.ok
    assert needle in report.errors[0]


@respx.mock
def test_rss_odd_entries_are_skipped_not_fatal():
    feed = (
        '<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">'
        '<entry><id>t3_nodate</id><title>No date</title><link href="https://www.reddit.com/r/godot/comments/nodate/x/"/></entry>'
        "<entry><id>t3_baddate</id><title>Bad date</title><published>yesterday</published></entry>"
        "<entry><id>t3_bare</id><title>Bare &amp; simple</title><updated>2026-10-03T11:00:00Z</updated>"
        '<link href="https://www.reddit.com/r/godot/comments/bare/x/"/>'
        '<summary type="html">&lt;div class=&quot;md&quot;&gt;&lt;p&gt;see https://itch.io/x&lt;/p&gt;&lt;/div&gt; &amp;#32; submitted by</summary></entry>'
        "</feed>"
    )
    rss_route().mock(return_value=httpx.Response(200, text=feed))
    mentions, _report = make_collector().run()
    assert [m.source_id for m in mentions] == ["bare"]
    bare = mentions[0]
    assert bare.channel == "r/godot" and bare.author is None and bare.title == "Bare & simple"
    assert bare.text == "see https://itch.io/x" and bare.links == ["https://itch.io/x"]
    assert bare.author_audience == 252000


@respx.mock
def test_rss_entry_parse_failure_becomes_a_warning(monkeypatch):
    rss_route().mock(return_value=httpx.Response(200, text=fx("feed_new.rss")))
    collector = make_collector()
    original = collector._rss_to_mention

    def flaky(entry):
        if entry.get("id") == "t3_1xa0zz9":
            raise KeyError("boom")
        return original(entry)

    monkeypatch.setattr(collector, "_rss_to_mention", flaky)
    mentions, report = collector.run()
    assert [m.source_id for m in mentions] == ["1xa1b2c", "1x9zq3k"]
    assert "skipped 1 malformed entry" in report.warnings[-1]


def test_rss_is_skipped_after_retirement():
    collector = make_collector(now=datetime(2026, 11, 13, 0, 5, tzinfo=UTC))
    mentions, report = collector.run()  # respx is not active: any request would hit the network
    assert mentions == [] and report.skipped and report.ok
    assert report.skip_reason == (
        "Reddit RSS was retired on 2026-11-13; add Reddit API credentials (see README)"
    )
    assert report.requests == 0
    assert collector.fetch_comments(make_mention("reddit", "x"), 10) == []


def test_retirement_date_is_configurable_and_oauth_ignores_it():
    later = datetime(2026, 12, 1, tzinfo=UTC)
    assert make_collector(now=later, rss_retirement_date=date(2027, 1, 1)).enabled() == (True, None)
    assert oauth_collector(now=later).enabled() == (True, None)


@respx.mock
def test_fetch_comments_in_rss_mode_makes_no_request():
    collector = make_collector()
    assert collector.fetch_comments(make_mention("reddit", "1xa1b2c"), 50) == []
    assert len(respx.calls) == 0 and collector.budget.used == 0


@respx.mock
def test_public_json_403_falls_back_to_rss():
    json_route = respx.get(host="www.reddit.com", path=f"/r/{MULTI}/new.json").mock(return_value=blocked())
    rss = rss_route().mock(return_value=httpx.Response(200, text=fx("feed_new.rss")))
    mentions, report = make_collector(try_public_json=True).run()
    assert json_route.call_count == 1 and rss.call_count == 1  # .json is never retried
    assert dict(json_route.calls.last.request.url.params) == {"limit": "100", "raw_json": "1"}
    assert len(mentions) == 3 and report.ok and report.errors == []
    assert any("public .json failed" in w and "falling back to RSS" in w for w in report.warnings)


@respx.mock
def test_public_json_success_skips_rss():
    respx.get(host="www.reddit.com", path=f"/r/{MULTI}/new.json").mock(
        return_value=ok_json("listing_new.json")
    )
    mentions, report = make_collector(try_public_json=True).run()
    assert len(respx.calls) == 1 and report.ok_units == 1
    assert len(mentions) == 5 and mentions[0].engagement.likes == 152


@respx.mock
def test_public_json_only_after_rss_retirement():
    respx.get(host="www.reddit.com", path=f"/r/{MULTI}/new.json").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"})
    )
    collector = make_collector(try_public_json=True, now=datetime(2026, 12, 1, tzinfo=UTC))
    mentions, report = collector.run()
    assert len(respx.calls) == 1 and mentions == []
    assert "RSS is retired" in report.warnings[0]
    assert report.errors and "HTTP 403" in report.errors[0]


@respx.mock
def test_public_json_invalid_body_falls_back():
    respx.get(host="www.reddit.com", path=f"/r/{MULTI}/new.json").mock(
        return_value=httpx.Response(200, text="nope")
    )
    rss_route().mock(return_value=httpx.Response(200, text=fx("feed_new.rss")))
    mentions, report = make_collector(try_public_json=True).run()
    assert len(mentions) == 3 and any("invalid JSON" in w for w in report.warnings)


def test_anonymous_mode_with_every_subreddit_excluded_makes_no_request():
    state = State()
    state.meta.collector_state["reddit"] = {"excluded": {"indiedev": (NOW + timedelta(hours=2)).isoformat()}}
    mentions, report = make_collector(subreddits=["IndieDev"], state=state).run()
    assert mentions == [] and report.requests == 0
    assert any("every configured subreddit" in w for w in report.warnings)


@respx.mock
def test_rss_budget_is_enforced():
    rss_route().mock(return_value=httpx.Response(200, text=fx("feed_new.rss")))
    mentions, report = make_collector(budget=Budget("reddit", 0)).run()
    assert mentions == [] and len(respx.calls) == 0
    assert any("budget" in w for w in report.warnings)


# ---------------------------------------------------------------- helpers


def test_text_url_extraction():
    text = (
        "Steam: https://store.steampowered.com/app/1/X/. [trailer](https://youtu.be/abc) "
        "[wiki](https://en.wikipedia.org/wiki/Co-op_(gaming)) <https://a.example/b>, "
        "**https://b.example/c**! (see https://c.example/d)"
    )
    assert _clean_links(_text_urls(text)) == [
        "https://store.steampowered.com/app/1/X/",
        "https://youtu.be/abc",
        "https://en.wikipedia.org/wiki/Co-op_(gaming)",
        "https://a.example/b",
        "https://b.example/c",
        "https://c.example/d",
    ]


def test_clean_links_drops_reddit_urls_and_duplicates():
    assert _clean_links(
        [
            "https://v.redd.it/abc",
            "https://i.redd.it/x.png",
            "https://preview.redd.it/y.png?width=1",
            "https://www.reddit.com/r/x/comments/1/",
            "https://old.reddit.com/r/x/",
            "https://redd.it/abc",
            "/r/IndieDev/comments/1/",
            "ftp://example.com/file",
            "https://itch.io/a",
            "https://itch.io/a",
            "https://store.steampowered.com/app/1/Some\\_Game/",
        ]
    ) == ["https://itch.io/a", "https://store.steampowered.com/app/1/Some_Game/"]


def test_html_to_text_keeps_paragraphs():
    assert _html_to_text("<p>One &amp; <b>two</b></p><p>three<br/>four</p>") == "One & two\nthree\nfour"
    assert _html_to_text("") == ""
