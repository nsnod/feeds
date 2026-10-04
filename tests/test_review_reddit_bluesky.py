"""Adversarial review of the Reddit and Bluesky collectors (and gembot/http.py as they use it).

Every test here encodes the behaviour the spec / module contracts promise and currently FAILS.
No network: respx answers every request; anything unmocked fails loudly.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest
import respx

from gembot.collectors.base import CollectContext
from gembot.collectors.bluesky import BlueskyCollector, parse_post
from gembot.collectors.reddit import RedditCollector
from gembot.http import Budget
from gembot.models import State
from tests.factories import NOW, make_config, make_http, make_mention

# ---------------------------------------------------------------- shared helpers

REDDIT_ENV = {"REDDIT_CLIENT_ID": "cid", "REDDIT_CLIENT_SECRET": "csecret", "REDDIT_USERNAME": "gem_owner"}
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
OAUTH = "oauth.reddit.com"

BSKY_ENV = {"BLUESKY_HANDLE": "gembot-test.bsky.social", "BLUESKY_APP_PASSWORD": "abcd-efgh-ijkl-mnop"}
PUBLIC = "https://public.api.bsky.app"
PUBLIC_SEARCH = f"{PUBLIC}/xrpc/app.bsky.feed.searchPosts"
THREAD = f"{PUBLIC}/xrpc/app.bsky.feed.getPostThread"
DID = "did:plc:mossdevq6gjnaw2blty4crtx"


def reddit_collector(env=None, *, budget: int | None = None, state: State | None = None, **reddit):
    config = make_config(env)
    for key, value in reddit.items():
        setattr(config.sources.reddit, key, value)
    ctx = CollectContext(config=config, http=make_http(), now=NOW, state=state)
    return RedditCollector(ctx, budget=Budget("reddit", budget) if budget is not None else None)


def bsky_collector(env=None, *, state: State | None = None, budget: int = 40, **bluesky):
    config = make_config(env=env or {})
    config.sources.bluesky = config.sources.bluesky.model_copy(update={"terms": ["friendslop"], **bluesky})
    ctx = CollectContext(
        config=config, http=make_http(), now=NOW, state=state if state is not None else State()
    )
    return BlueskyCollector(ctx, budget=Budget("bluesky", budget))


def token_ok() -> httpx.Response:
    return httpx.Response(200, json={"access_token": "tok", "token_type": "bearer", "expires_in": 86400})


def t3(post_id: str, created: datetime, **extra) -> dict:
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


def bsky_post(rkey: str, text: str = "co-op friendslop game", created: str | None = None, **extra) -> dict:
    stamp = (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    post = {
        "uri": f"at://{DID}/app.bsky.feed.post/{rkey}",
        "cid": f"bafy{rkey}",
        "author": {"did": DID, "handle": "mosslantern.bsky.social", "labels": []},
        "record": {"$type": "app.bsky.feed.post", "text": text, "createdAt": created or stamp},
        "indexedAt": stamp,
        "likeCount": 3,
        "replyCount": 1,
        "repostCount": 0,
        "quoteCount": 0,
        "labels": [],
    }
    post.update(extra)
    return post


# ======================================================================== Reddit


@respx.mock
def test_reddit_new_page1_survives_when_page2_fails():
    """Partial results must survive (collectors/base.py contract): a failure while fetching
    page 2 of /new must not throw away the 2 posts page 1 already returned."""
    respx.post(TOKEN_URL).mock(return_value=token_ok())
    page1 = [t3("p0", NOW - timedelta(minutes=10)), t3("p1", NOW - timedelta(minutes=20))]
    respx.get(host=OAUTH, path="/r/IndieDev/new").mock(
        side_effect=[
            listing(*page1, after="t3_p1"),
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(503),
        ]
    )
    respx.get(host=OAUTH, path="/r/IndieDev/rising").mock(return_value=listing())

    mentions, report = reddit_collector(REDDIT_ENV, subreddits=["IndieDev"], limit=2).run()

    assert {m.source_id for m in mentions} == {"p0", "p1"}, report.errors


@respx.mock
def test_reddit_new_page1_survives_a_budget_stop_on_page2():
    """Budget stop must keep partial results ("Append results to self.found as you go")."""
    respx.post(TOKEN_URL).mock(return_value=token_ok())
    page1 = [t3("p0", NOW - timedelta(minutes=10)), t3("p1", NOW - timedelta(minutes=20))]
    respx.get(host=OAUTH, path="/r/IndieDev/new").mock(return_value=listing(*page1, after="t3_p1"))

    # token (1) + /new page 1 (1); page 2 hits the budget
    mentions, report = reddit_collector(REDDIT_ENV, subreddits=["IndieDev"], limit=2, budget=2).run()

    assert report.warnings and "budget" in report.warnings[-1]
    assert {m.source_id for m in mentions} == {"p0", "p1"}


@respx.mock
def test_reddit_per_subreddit_fallback_keeps_posts_when_budget_runs_out():
    """Multireddit 403 (private sub) -> one-by-one probing. A budget stop on the 2nd probe must
    keep the posts the 1st probe already returned."""
    respx.post(TOKEN_URL).mock(return_value=token_ok())
    private = httpx.Response(403, json={"reason": "private", "message": "Forbidden", "error": 403})
    respx.get(host=OAUTH, path="/r/IndieDev+SecretSub+godot/new").mock(return_value=private)
    respx.get(host=OAUTH, path="/r/IndieDev/new").mock(
        return_value=listing(t3("a1", NOW - timedelta(hours=1)))
    )

    # token (1) + multireddit (1) + r/IndieDev (1); r/SecretSub hits the budget
    mentions, report = reddit_collector(
        REDDIT_ENV, subreddits=["IndieDev", "SecretSub", "godot"], budget=3
    ).run()

    assert report.warnings and "budget" in report.warnings[-1]
    assert [m.source_id for m in mentions] == ["a1"]


@respx.mock
def test_reddit_oauth_429_stops_further_reddit_requests_this_run():
    """`_stop_reason` is documented as "no more Reddit requests this run (IP block, rate limit)".
    A hard 429 (x-ratelimit-remaining 0, reset in 300 s) must stop /rising and comment fetches."""
    respx.post(TOKEN_URL).mock(return_value=token_ok())
    limited = httpx.Response(
        429,
        json={"message": "Too Many Requests", "error": 429},
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-used": "100", "x-ratelimit-reset": "300"},
    )
    new = respx.get(host=OAUTH, path="/r/IndieDev/new").mock(return_value=limited)
    rising = respx.get(host=OAUTH, path="/r/IndieDev/rising").mock(return_value=limited)
    comments = respx.get(host=OAUTH, path="/comments/abc").mock(return_value=limited)

    collector = reddit_collector(REDDIT_ENV, subreddits=["IndieDev"])
    collector.run()
    collector.safe_fetch_comments(make_mention("reddit", "abc", comments=5), 10)

    assert new.call_count == 1
    assert rising.call_count == 0
    assert comments.call_count == 0


@respx.mock
def test_reddit_oauth_link_urls_are_not_html_unescaped_twice():
    """Listings are requested with raw_json=1, so `url`/`selftext` are already unescaped.
    html.unescape() on them turns `&section=` into `§ion=`, `&region=` into `®ion=`,
    `&timestamp=` into `×tamp=` ..."""
    respx.post(TOKEN_URL).mock(return_value=token_ok())
    link = "https://store.steampowered.com/news/app/3141590/view/42?l=english&section=updates"
    in_text = "https://mosslantern.itch.io/moss-lantern?ref=reddit&region=eu&timestamp=1791090000"
    post = t3(
        "lnk",
        NOW - timedelta(hours=1),
        is_self=False,
        url=link,
        domain="store.steampowered.com",
        selftext=f"Also on itch: {in_text}",
    )
    respx.get(host=OAUTH, path="/r/IndieDev/new").mock(return_value=listing(post))
    respx.get(host=OAUTH, path="/r/IndieDev/rising").mock(return_value=listing())

    mentions, _ = reddit_collector(REDDIT_ENV, subreddits=["IndieDev"]).run()

    assert mentions[0].links == [link, in_text]


@respx.mock
def test_reddit_rss_does_not_follow_redirects_and_makes_exactly_one_request():
    """Anonymous mode promises ONE combined RSS request per run (~1 req/min/IP). A nonexistent
    subreddit makes Reddit redirect to subreddit search; following it silently sends a 2nd,
    unbudgeted request and reports 'ok, 0 mentions' (the OAuth/.json paths use
    follow_redirects=False for exactly this reason)."""
    multi = "IndieDev+Nopez"
    respx.get(host="www.reddit.com", path=f"/r/{multi}/new/.rss").mock(
        return_value=httpx.Response(
            302, headers={"location": "https://www.reddit.com/subreddits/search.rss?q=Nopez"}
        )
    )
    respx.get(host="www.reddit.com", path="/subreddits/search.rss").mock(
        return_value=httpx.Response(
            200,
            text='<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>search</title></feed>',
            headers={"content-type": "application/atom+xml"},
        )
    )
    collector = reddit_collector(subreddits=["IndieDev", "Nopez"])

    _mentions, report = collector.run()

    sent = len(respx.calls)
    assert sent == 1, f"{sent} HTTP requests to reddit.com in RSS mode"
    assert collector.budget.used == sent
    assert not report.ok or report.warnings[1:], "a redirect to search must be reported, not silently OK"


@respx.mock
def test_reddit_disabled_in_sources_yaml_makes_no_comment_requests():
    """`enabled: false` skips collection, but enrichment still calls fetch_comments on the same
    collector for older Reddit mentions in state -> token + comments requests to Reddit."""
    token = respx.post(TOKEN_URL).mock(return_value=token_ok())
    comments = respx.get(host=OAUTH, path="/comments/abc").mock(return_value=httpx.Response(200, json=[]))
    collector = reddit_collector(REDDIT_ENV, enabled=False)

    _mentions, report = collector.run()
    collector.safe_fetch_comments(make_mention("reddit", "abc", comments=5), 10)

    assert report.skipped
    assert token.call_count == 0 and comments.call_count == 0


# ======================================================================== Bluesky


def test_bluesky_parse_post_out_of_range_created_at_does_not_raise():
    """record.createdAt is author-controlled. '9999-12-31T23:59:59-12:00' is valid RFC 3339
    (sortAt = indexedAt, so it shows up in 'latest' search) but converting it to UTC overflows."""
    post = bsky_post("3hostile", created="9999-12-31T23:59:59-12:00")
    mention = parse_post(post, now=NOW, max_age_hours=72)  # raises OverflowError today
    assert mention is None or mention.created_at <= NOW


@respx.mock
def test_bluesky_one_hostile_created_at_does_not_lose_the_rest_of_the_term():
    """One odd post must be skipped on its own (`_take` only catches ValueError, so the
    OverflowError aborts the whole term and every later post on the page is lost)."""
    page = {"posts": [bsky_post("3hostile", created="9999-12-31T23:59:59-12:00"), bsky_post("3good")]}
    respx.get(PUBLIC_SEARCH).mock(return_value=httpx.Response(200, json=page))

    mentions, report = bsky_collector().run()  # logged out, public search answers

    assert [m.source_id for m in mentions] == [f"{DID}/3good"] or len(mentions) == 2
    assert report.errors == []


@respx.mock
def test_bluesky_fetch_comments_survives_a_reply_with_out_of_range_created_at():
    replies = [
        {
            "$type": "app.bsky.feed.defs#threadViewPost",
            "post": bsky_post("3r1", created="0001-01-01T00:00:00+01:00"),
        },
        {"$type": "app.bsky.feed.defs#threadViewPost", "post": bsky_post("3r2", text="wishlisted!")},
    ]
    thread = {
        "thread": {
            "$type": "app.bsky.feed.defs#threadViewPost",
            "post": bsky_post("3root"),
            "replies": replies,
        }
    }
    respx.get(THREAD).mock(return_value=httpx.Response(200, json=thread))
    mention = make_mention("bluesky", f"{DID}/3root", comments=2)

    comments = bsky_collector().safe_fetch_comments(mention, 10)

    assert "wishlisted!" in [c.text for c in comments]


def test_bluesky_truncated_display_url_followed_by_punctuation_is_not_a_link():
    """Shortened display links ('…/moss-lan...') are skipped only when '...' is the very last
    character; '(https://x.itch.io/moss-lan...)' or '...,' slips through as a bogus link
    (canonical id itch:mosslantern/moss-lan)."""
    full = "https://mosslantern.itch.io/moss-lantern"
    text = "Demo is up (https://mosslantern.itch.io/moss-lan...) go play"
    start = len(b"Demo is up (")
    post = bsky_post("3trunc", text=text)
    post["record"]["facets"] = [
        {
            "index": {"byteStart": start, "byteEnd": start + len("https://mosslantern.itch.io/moss-lan...")},
            "features": [{"$type": "app.bsky.richtext.facet#link", "uri": full}],
        }
    ]
    mention = parse_post(post, now=NOW, max_age_hours=72)
    assert mention is not None
    assert mention.links == [full]


@respx.mock
def test_bluesky_reply_author_matches_mention_author_when_handle_is_invalid():
    """parse_post maps author handle 'handle.invalid' to the DID, replies keep 'handle.invalid':
    the author's own replies are then not recognised as the author's (analyze_comments compares
    post_author with Comment.author), and all invalid-handle users collapse into one commenter."""
    root = bsky_post("3root")
    root["author"]["handle"] = "handle.invalid"
    mention = parse_post(root, now=NOW, max_age_hours=72)
    assert mention is not None and mention.author == DID
    own_reply = bsky_post("3self", text="thanks! wishlist link in bio")
    own_reply["author"]["handle"] = "handle.invalid"
    thread = {
        "thread": {
            "$type": "app.bsky.feed.defs#threadViewPost",
            "post": root,
            "replies": [{"$type": "app.bsky.feed.defs#threadViewPost", "post": own_reply}],
        }
    }
    respx.get(THREAD).mock(return_value=httpx.Response(200, json=thread))

    comments = bsky_collector().fetch_comments(mention, 10)

    assert [c.author for c in comments] == [mention.author]


@respx.mock
def test_bluesky_disabled_in_sources_yaml_makes_no_enrichment_requests():
    thread = respx.get(THREAD).mock(return_value=httpx.Response(200, json={"thread": {}}))
    profile = respx.get(f"{PUBLIC}/xrpc/app.bsky.actor.getProfile").mock(
        return_value=httpx.Response(200, json={"did": DID, "followersCount": 5})
    )
    collector = bsky_collector(BSKY_ENV, enabled=False)
    mention = make_mention("bluesky", f"{DID}/3root", comments=2)

    _mentions, report = collector.run()
    collector.safe_fetch_comments(mention, 10)
    collector.safe_fetch_audience(mention)

    assert report.skipped
    assert thread.call_count == 0 and profile.call_count == 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q", "-o", "addopts="]))
