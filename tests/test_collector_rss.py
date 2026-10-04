from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import httpx
import pytest
import respx

from gembot.collectors import rss as rss_module
from gembot.collectors.base import CollectContext
from gembot.collectors.rss import (
    MAX_ENTRY_AGE,
    BadFeedUrl,
    NotAFeed,
    RssCollector,
    check_feed_url,
    entry_to_mention,
    normalize_id,
    parse_feed,
)
from gembot.config import FeedConfig, Feeds
from gembot.http import Budget
from gembot.models import Engagement, Mention
from tests.factories import NOW, fixture_path, make_config, make_http

IG = FeedConfig(
    name="Lighthouse (Instagram)",
    url="https://rss.app/feeds/AbCdEfGhIjKlMnOp.xml",
    source="instagram",
    audience=25000,
)
TT = FeedConfig(name="Lighthouse (TikTok)", url="https://rss.app/feeds/TtKkLlMmNnOoPpQq.xml", source="tiktok")
YT = FeedConfig(
    name="Tiny Pixel Devlogs",
    url="https://www.youtube.com/feeds/videos.xml?channel_id=UCabcdefghijklmnopqrstuv",
    source="youtube",
    audience=12000,
)
BLOG = FeedConfig(name="Friendslop Weekly", url="https://friendslop.example.com/feed/")
ATOM = FeedConfig(name="Co-op Corner", url="https://coop.example.org/index.xml")

IG_THUMB = (
    "https://scontent-iad3-1.cdninstagram.com/v/t51.2885-15/461234567_1234567890_n.jpg?stp=dst-jpg_e35"
    "&_nc_ht=scontent-iad3-1.cdninstagram.com&_nc_cat=1&_nc_ohc=AbCdEf&oh=00_AbCdEf&oe=6AC91E8C&_nc_sid=8b3546"
)


def body(name: str) -> bytes:
    return fixture_path("rss", name).read_bytes()


def xml_response(content: bytes, **headers: str) -> httpx.Response:
    return httpx.Response(200, content=content, headers={"Content-Type": "application/rss+xml", **headers})


def make_collector(*feeds: FeedConfig, http=None, budget: Budget | None = None) -> RssCollector:
    config = make_config(feeds=Feeds(feeds=list(feeds)))
    ctx = CollectContext(config=config, http=http or make_http(), now=NOW)
    return RssCollector(ctx, budget=budget)


def run(*feeds: FeedConfig, http=None, budget: Budget | None = None):
    return make_collector(*feeds, http=http, budget=budget).run()


def collect_fixture(feed: FeedConfig, fixture: str) -> list[Mention]:
    respx.get(feed.url).mock(return_value=xml_response(body(fixture)))
    mentions, report = run(feed)
    assert report.errors == [] and report.warnings == []
    assert report.ok_units == 1 and report.requests == 1
    return mentions


# ------------------------------------------------------------------ fixtures -> Mentions


@respx.mock
def test_rssapp_instagram_feed():
    mentions = collect_fixture(IG, "rssapp_instagram.xml")
    assert [m.key for m in mentions] == [
        "instagram:DAbCdEfGhIj",
        "instagram:C9xYz123AbC",
        "instagram:DAaOldPost12",
    ]
    caption = "Our demo is live for Steam Next Fest! Link in bio. #indiegame #gamedev #pixelart"
    expected = Mention(
        source="instagram",
        source_id="DAbCdEfGhIj",
        url="https://www.instagram.com/p/DAbCdEfGhIj",
        title=caption,
        text=caption,
        author="@lighthouse.games",  # dc:creator is literally "Instagram"
        author_audience=25000,
        created_at=datetime(2026, 10, 3, 7, 4, 12, tzinfo=UTC),
        engagement=Engagement(),
        links=["https://www.instagram.com/p/DAbCdEfGhIj"],
        media_thumb=IG_THUMB,
        raw_tags=["indiegame", "gamedev", "pixelart"],
        channel="instagram:Lighthouse (Instagram)",
        extra={
            "feed_name": "Lighthouse (Instagram)",
            "feed_url": IG.url,
            "generator": "RSS.app",
            "is_short": False,
            "engagement_known": False,
        },
    )
    assert mentions[0].model_dump() == expected.model_dump()

    reel = mentions[1]
    assert reel.url == "https://www.instagram.com/reel/C9xYz123AbC"
    assert reel.title.endswith("co-op chaos game and the...")  # RSS.app truncates titles...
    assert reel.text.endswith("#friendslop #coop #IndieDev #proximitychat")  # ...the description is complete
    assert "🍕" in reel.text
    # scheme-less store link from the (unclickable) caption
    assert reel.links == [
        "https://www.instagram.com/reel/C9xYz123AbC",
        "https://store.steampowered.com/app/2345670",
    ]
    assert reel.raw_tags == ["friendslop", "coop", "indiedev", "proximitychat"]
    assert "&amp;" not in reel.media_thumb and "&oh=00_XyZ&oe=6AC9AA10" in reel.media_thumb
    assert all(m.engagement.total == 0 and m.extra["engagement_known"] is False for m in mentions)
    assert all(m.author == "@lighthouse.games" and m.author_audience == 25000 for m in mentions)
    assert mentions[2].raw_tags == [] and mentions[2].created_at == datetime(2026, 9, 20, 15, tzinfo=UTC)


@respx.mock
def test_rssapp_creator_fallbacks_without_handle_in_channel():
    hashtag_feed = (
        body("rssapp_instagram.xml")
        .replace(
            "Lighthouse Games (@lighthouse.games) • Instagram photos and videos".encode(),
            b"#indiegame hashtag on Instagram",
        )
        .replace(
            b"https://www.instagram.com/lighthouse.games/",
            b"https://www.instagram.com/explore/tags/indiegame/",
        )
    )
    respx.get(IG.url).mock(return_value=xml_response(hashtag_feed))
    mentions, _ = run(IG)
    assert {m.author for m in mentions} == {"Lighthouse (Instagram)"}  # "Instagram" is never an author

    profile_only = body("rssapp_instagram.xml").replace(
        "Lighthouse Games (@lighthouse.games) • Instagram photos and videos".encode(), b"Lighthouse Games"
    )
    parsed = parse_feed(profile_only)
    assert entry_to_mention(parsed.entries[0], parsed.feed, IG, now=NOW).author == "@lighthouse.games"

    named = body("rssapp_instagram.xml").replace(b"<![CDATA[Instagram]]>", b"<![CDATA[Lighthouse Games]]>")
    parsed = parse_feed(named.replace(b"(@lighthouse.games) ", b""))
    parsed.feed["link"] = "https://lighthouse.example.com/"
    assert entry_to_mention(parsed.entries[0], parsed.feed, IG, now=NOW).author == "Lighthouse Games"


@respx.mock
def test_rssapp_tiktok_feed():
    mentions = collect_fixture(TT, "rssapp_tiktok.xml")
    assert [m.key for m in mentions] == ["tiktok:7556123456789012345", "tiktok:7555000000000000001"]
    first, second = mentions
    assert first.author == "@lighthousegames"  # dc:creator "@lighthousegames"
    assert second.author == "@lighthousegames"  # dc:creator "TikTok" -> /@user/video/ in the link
    assert first.url == "https://www.tiktok.com/@lighthousegames/video/7556123456789012345"
    assert first.media_thumb == (
        "https://p16-sign-va.tiktokcdn.com/tos-maliva-p-0068/abc123~tplv-tiktokx-origin.image"
        "?dr=14575&x-expires=1791234000&x-signature=AbC%3D"
    )
    assert first.raw_tags == ["gamedev", "indiedev", "fyp"]
    assert second.raw_tags == ["indiegame", "coop"]
    assert second.text.startswith("Day 40 of making a co-op horror game")
    assert first.channel == "tiktok:Lighthouse (TikTok)" and first.author_audience is None
    assert first.created_at == datetime(2026, 10, 2, 20, 41, 55, tzinfo=UTC)
    assert first.extra["generator"] == "RSS.app" and first.extra["engagement_known"] is False


@respx.mock
def test_youtube_channel_feed():
    mentions = collect_fixture(YT, "youtube_channel.xml")
    # the 2026-07-01 video is older than MAX_ENTRY_AGE and skipped
    assert [m.key for m in mentions] == ["youtube:pReMiErE001", "youtube:dQw4w9WgXcQ", "youtube:aZXQk7zkdy0"]
    premiere, devlog, short = mentions

    # premiere: no media:community -> missing stats tolerated
    assert premiere.engagement == Engagement()
    assert "views" not in premiere.extra and premiere.extra["engagement_known"] is False
    assert premiere.links == [
        "https://www.youtube.com/watch?v=pReMiErE001",
        "https://store.steampowered.com/app/2345670/Gorilla_Pizza_Panic/",  # trailing "." stripped
        "https://lighthouse-games.itch.io/gorilla-pizza-panic",
    ]
    assert premiere.raw_tags == ["friendslop", "coop"]
    assert (
        premiere.text.splitlines()[1] == "Demo on itch: https://lighthouse-games.itch.io/gorilla-pizza-panic"
    )

    expected = Mention(
        source="youtube",
        source_id="dQw4w9WgXcQ",
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        title="I made a roguelike about a lighthouse keeper (Devlog #12)",
        text="Wishlist on Steam: https://store.steampowered.com/app/0000000/\n"
        "This week: fog shaders, a new boss, and why I rewrote the save system.",
        author="Tiny Pixel Devlogs",
        author_audience=12000,
        created_at=datetime(2026, 10, 2, 16, 0, 31, tzinfo=UTC),
        engagement=Engagement(likes=842),
        links=["https://www.youtube.com/watch?v=dQw4w9WgXcQ", "https://store.steampowered.com/app/0000000/"],
        media_thumb="https://i2.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
        raw_tags=[],  # "Devlog #12" is not a hashtag
        channel="youtube:Tiny Pixel Devlogs",
        extra={
            "feed_name": "Tiny Pixel Devlogs",
            "feed_url": YT.url,
            "generator": None,
            "is_short": False,
            "engagement_known": True,
            "views": 12873,
            "yt_channel_id": "UCabcdefghijklmnopqrstuv",
        },
    )
    assert devlog.model_dump() == expected.model_dump()

    assert short.extra["is_short"] is True and short.extra["views"] == 48211
    assert short.engagement == Engagement(likes=3120) and short.text == ""
    assert short.url == "https://www.youtube.com/shorts/aZXQk7zkdy0"
    assert short.raw_tags == ["gamedev", "indiegame"]


@respx.mock
def test_generic_blog_rss():
    mentions = collect_fixture(BLOG, "generic_blog.rss")
    assert len(mentions) == 2  # the August post is older than 30 days
    post, roundup = mentions
    expected = Mention(
        source="rss",
        source_id="friendslop.example.com/2026/10/gorilla-pizza-panic",
        url="https://friendslop.example.com/2026/10/gorilla-pizza-panic",
        title="Gorilla Pizza Panic is the loudest co-op game of the fall",
        text="Four players, one oven and a lot of shouting. Wishlist it on Steam & grab the press kit at "
        "https://gorillapizza.example.com/presskit.\nMore in our co-op tag. #friendslop",
        author="Sam Rivera",  # from "editor@... (Sam Rivera)"
        author_audience=None,
        created_at=datetime(2026, 10, 2, 9, tzinfo=UTC),
        links=[
            "https://friendslop.example.com/2026/10/gorilla-pizza-panic",
            "https://store.steampowered.com/app/2345670/Gorilla_Pizza_Panic/",
            "https://friendslop.example.com/tags/co-op/",  # relative href resolved
            "https://gorillapizza.example.com/presskit",  # trailing "." stripped
        ],
        media_thumb="https://friendslop.example.com/wp-content/uploads/2026/10/gpp-cover.jpg?w=1200&h=630",
        raw_tags=["friendslop", "co-op", "indie"],
        channel="rss:Friendslop Weekly",
        extra={
            "feed_name": "Friendslop Weekly",
            "feed_url": BLOG.url,
            "generator": "WordPress 6.6",
            "is_short": False,
            "engagement_known": False,
        },
    )
    assert post.model_dump() == expected.model_dump()

    # no guid, long link with a query string -> sha1 of the normalised link; no date -> now
    normalized = (
        "friendslop.example.com/2026/10/screenshot-saturday-roundup-proximity-chat-everywhere-and-more-"
        "tiny-co-op-games?utm_source=rss&utm_medium=rss"
    )
    assert roundup.source_id == hashlib.sha1(normalized.encode()).hexdigest()[:16]
    assert roundup.created_at == NOW
    assert roundup.author == "Friendslop Weekly"
    assert roundup.media_thumb == "https://friendslop.example.com/wp-content/uploads/2026/10/ss-roundup.png"
    assert roundup.links[1:] == ["https://lighthouse-games.itch.io/haunted-laundromat"]
    assert roundup.raw_tags == ["screenshotsaturday", "roundup"]


@respx.mock
def test_generic_atom_feed():
    mentions = collect_fixture(ATOM, "atom_generic.xml")
    post, note = mentions
    assert post.source_id == hashlib.sha1(b"tag:coop.example.org,2026:posts/five-tiny").hexdigest()[:16]
    assert post.title == "Five tiny co-op games to watch"  # type="html" title stripped
    assert post.text == (
        "Five small co-op games. First up: Gorilla Pizza Panic (Steam), then a haunted laundromat sim."
    )  # the longer <content> wins over <summary>
    assert post.links == [
        "https://coop.example.org/posts/five-tiny/",
        "https://coop.example.org/games/gorilla-pizza-panic/",
        "https://s.team/a/2345670",
    ]
    assert post.media_thumb == "https://coop.example.org/images/five-tiny.webp"
    assert post.created_at == datetime(2026, 10, 2, 9, 30, tzinfo=UTC)  # <updated> when no <published>
    assert post.author == "Mara" and post.raw_tags == ["co-op", "friendslop"]
    assert post.extra["generator"] == "Hugo"

    assert note.source_id == "urn:uuid:1225c695-cfb8-4ebb-aaaa-80da344efa6a"
    assert note.url == "https://coop.example.org/"  # no link -> the site link
    assert note.links == [] and note.media_thumb is None
    assert note.title == "Notes <3" and note.text == "Short note: I <3 tiny co-op games & their devs."
    assert note.author == "Co-op Corner"


@respx.mock
def test_all_feeds_in_one_run():
    for feed, fixture in [
        (IG, "rssapp_instagram.xml"),
        (TT, "rssapp_tiktok.xml"),
        (YT, "youtube_channel.xml"),
        (BLOG, "generic_blog.rss"),
        (ATOM, "atom_generic.xml"),
    ]:
        respx.get(feed.url).mock(return_value=xml_response(body(fixture)))
    mentions, report = run(IG, TT, YT, BLOG, ATOM)
    assert len(mentions) == 3 + 2 + 3 + 2 + 2
    assert {m.source for m in mentions} == {"instagram", "tiktok", "youtube", "rss"}
    assert report.ok_units == 5 and report.mentions == 12 and report.requests == 5
    assert report.summary() == "rss: ok, 12 mentions, 5 requests"


# ------------------------------------------------------------------ URL lint


@respx.mock
def test_url_lint_errors_are_recorded_per_feed_and_other_feeds_still_collected():
    bad = [
        FeedConfig(name="viewer", url="https://rss.app/feed/AbCdEfGhIjKlMnOp", source="instagram"),
        FeedConfig(name="json", url="https://rss.app/feeds/AbCdEfGhIjKlMnOp.json", source="instagram"),
        FeedConfig(name="json v1.1", url="https://rss.app/feeds/v1.1/QqRrSsTtUuVvWwXx.json", source="tiktok"),
        FeedConfig(name="handle", url="https://www.youtube.com/@tinypixeldevlogs", source="youtube"),
        FeedConfig(name="custom", url="https://youtube.com/c/TinyPixel", source="youtube"),
        FeedConfig(name="jsonfeed", url="https://blog.example.com/feed.json"),
        FeedConfig(name="ftp", url="ftp://files.example.com/feed.xml"),
    ]
    respx.get(YT.url).mock(return_value=xml_response(body("youtube_channel.xml")))
    mentions, report = run(*bad[:3], YT, *bad[3:])  # respx would reject any request to a bad URL
    assert len(mentions) == 3
    assert report.ok_units == 1 and report.failed_units == 7 and report.ok
    errors = report.errors
    assert errors[0].startswith(
        "feed 'viewer': BadFeedUrl: use the https://rss.app/feeds/AbCdEfGhIjKlMnOp.xml RSS URL"
    )
    assert "use the https://rss.app/feeds/AbCdEfGhIjKlMnOp.xml RSS URL" in errors[1]
    assert "use the https://rss.app/feeds/QqRrSsTtUuVvWwXx.xml RSS URL" in errors[2]
    assert errors[3].startswith("feed 'handle': BadFeedUrl: needs a feeds/videos.xml?channel_id=UC... URL")
    assert "needs a feeds/videos.xml?channel_id=UC... URL" in errors[4]
    assert "JSON feeds are not supported" in errors[5]
    assert "not an http(s) URL" in errors[6]


@respx.mock
def test_youtube_channel_page_is_rewritten_to_its_feed():
    feed = FeedConfig(
        name="channel page", url="https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv", source="youtube"
    )
    route = respx.get(YT.url).mock(return_value=xml_response(body("youtube_channel.xml")))
    mentions, report = run(feed)
    assert route.called and len(mentions) == 3 and report.errors == []
    assert (
        "fetched https://www.youtube.com/feeds/videos.xml?channel_id=UCabcdefghijklmnopqrstuv"
        in report.warnings[0]
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://rss.app/feeds/AbCdEfGhIjKlMnOp.xml",
        "https://www.youtube.com/feeds/videos.xml?channel_id=UCabcdefghijklmnopqrstuv",
        "https://www.youtube.com/feeds/videos.xml?playlist_id=PLabc",
        "https://friendslop.example.com/feed/",
        "  https://itch.io/games/newest.xml  ",
    ],
)
def test_check_feed_url_accepts_feeds(url):
    assert check_feed_url(url) == (url.strip(), None)


def test_check_feed_url_rejects_youtube_short_links_and_watch_pages():
    for url in (
        "https://youtu.be/dQw4w9WgXcQ",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://rss.app/",
    ):
        with pytest.raises(BadFeedUrl):
            check_feed_url(url)


# ------------------------------------------------------------------ HTTP behaviour


@respx.mock
def test_conditional_get_304_means_nothing_new_not_an_error():
    route = respx.get(IG.url).mock(
        side_effect=[
            xml_response(
                body("rssapp_instagram.xml"), ETag='"rssapp-v1"', **{"Last-Modified": "Sat, 03 Oct 2026"}
            ),
            httpx.Response(304),
        ]
    )
    http = make_http()  # the pipeline persists http.cache between runs
    first, _ = run(IG, http=http)
    assert len(first) == 3
    assert "if-none-match" not in route.calls[0].request.headers

    second, report = run(IG, http=http)
    assert second == [] and report.errors == [] and report.warnings == []
    assert report.ok_units == 1 and report.ok and report.requests == 1
    assert route.calls[1].request.headers["if-none-match"] == '"rssapp-v1"'


@respx.mock
def test_youtube_404_is_recorded_with_a_hint_and_other_feeds_continue():
    respx.get(YT.url).mock(return_value=httpx.Response(404))
    respx.get(TT.url).mock(return_value=xml_response(body("rssapp_tiktok.xml")))
    mentions, report = run(YT, TT)
    assert [m.source for m in mentions] == ["tiktok", "tiktok"]
    assert len(report.errors) == 1 and "HTTP 404" in report.errors[0]
    assert "YouTube feeds return 404 now and then" in report.errors[0]
    assert report.failed_units == 1 and report.ok_units == 1 and report.ok


@respx.mock
def test_500_is_retried_then_recorded():
    route = respx.get(BLOG.url).mock(return_value=httpx.Response(500))
    mentions, report = run(BLOG)
    assert mentions == [] and route.call_count == 3  # 1 + 2 retries, all charged to the budget
    assert report.requests == 3 and "HTTP 500" in report.errors[0]
    assert not report.ok and "FAILED" in report.summary()


@respx.mock
def test_404_on_a_normal_feed_has_no_youtube_hint():
    respx.get(BLOG.url).mock(return_value=httpx.Response(404))
    _, report = run(BLOG)
    assert "HTTP 404" in report.errors[0] and "YouTube" not in report.errors[0]


@respx.mock
def test_429_is_retried_then_rate_limited_is_recorded():
    route = respx.get(IG.url).mock(return_value=httpx.Response(429, headers={"Retry-After": "1"}))
    mentions, report = run(IG)
    assert mentions == [] and route.call_count == 3
    assert "rate limited (429)" in report.errors[0]


@respx.mock
def test_429_then_success():
    respx.get(IG.url).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1"}),
            xml_response(body("rssapp_instagram.xml")),
        ]
    )
    mentions, report = run(IG)
    assert len(mentions) == 3 and report.errors == [] and report.requests == 2


@respx.mock
def test_budget_is_enforced_and_partial_results_kept():
    other = FeedConfig(name="Other blog", url="https://other.example.com/rss")
    respx.get(IG.url).mock(return_value=xml_response(body("rssapp_instagram.xml")))
    respx.get(TT.url).mock(return_value=xml_response(body("rssapp_tiktok.xml")))
    third = respx.get(other.url).mock(return_value=xml_response(body("generic_blog.rss")))
    mentions, report = run(IG, TT, other, budget=Budget("rss", 2))
    assert len(mentions) == 5  # Instagram + TikTok kept
    assert not third.called
    assert report.requests == 2 and "budget" in report.warnings[0]
    assert report.errors == []


# ------------------------------------------------------------------ malformed bodies


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (body("broken.xml"), "not a valid RSS/Atom feed (SAXParseException"),
        (body("not_xml.html"), "got an HTML page, not an RSS/Atom feed"),
        (b'{"version": "https://jsonfeed.org/version/1.1", "items": []}', "got JSON, not RSS/Atom"),
        (b"", "empty response body"),
        (b"Service Unavailable", "not a valid RSS/Atom feed"),
    ],
)
@respx.mock
def test_malformed_bodies_are_recorded_not_raised(content, message):
    respx.get(BLOG.url).mock(return_value=httpx.Response(200, content=content))
    respx.get(ATOM.url).mock(return_value=xml_response(body("atom_generic.xml")))
    mentions, report = run(BLOG, ATOM)
    assert len(mentions) == 2 and {m.channel for m in mentions} == {"rss:Co-op Corner"}
    assert len(report.errors) == 1
    assert report.errors[0].startswith("feed 'Friendslop Weekly': NotAFeed: ")
    assert message in report.errors[0]


@respx.mock
def test_partly_broken_feed_keeps_readable_entries_with_a_warning():
    truncated = body("generic_blog.rss").split(b"<item>\n<title>Screenshot")[0]
    respx.get(BLOG.url).mock(return_value=xml_response(truncated))
    mentions, report = run(BLOG)
    assert [m.title for m in mentions] == ["Gorilla Pizza Panic is the loudest co-op game of the fall"]
    assert report.errors == [] and "malformed feed" in report.warnings[0]


@respx.mock
def test_valid_empty_feed_is_fine():
    respx.get(BLOG.url).mock(
        return_value=xml_response(
            b"<?xml version='1.0'?><rss version='2.0'><channel><title>x</title></channel></rss>"
        )
    )
    mentions, report = run(BLOG)
    assert mentions == [] and report.errors == [] and report.ok_units == 1


def test_parse_feed_never_reads_a_local_file_named_by_the_body(tmp_path):
    local = tmp_path / "secret.xml"
    local.write_bytes(body("generic_blog.rss"))
    with pytest.raises(NotAFeed):
        parse_feed(str(local).encode())  # bytes would make feedparser open() this path
    with pytest.raises(NotAFeed):
        parse_feed(b"https://friendslop.example.com/feed/")


@respx.mock
def test_unreadable_entry_is_skipped_with_a_warning(monkeypatch):
    real = rss_module.entry_to_mention

    def flaky(entry, channel, feed, *, now):
        if "C9xYz123AbC" in entry.get("link", ""):
            raise ValueError("odd entry")
        return real(entry, channel, feed, now=now)

    monkeypatch.setattr(rss_module, "entry_to_mention", flaky)
    respx.get(IG.url).mock(return_value=xml_response(body("rssapp_instagram.xml")))
    mentions, report = run(IG)
    assert [m.source_id for m in mentions] == ["DAbCdEfGhIj", "DAaOldPost12"]
    assert report.warnings == ["feed 'Lighthouse (Instagram)': skipped 1 unreadable entry"]
    assert report.errors == []


# ------------------------------------------------------------------ enable / disable


def test_no_feeds_means_the_collector_is_skipped():
    mentions, report = run()
    assert mentions == [] and report.skipped and report.ok
    assert report.skip_reason == "no feeds in config/feeds.yaml"
    assert report.summary() == "rss: skipped (no feeds in config/feeds.yaml)"


def test_only_disabled_feeds_means_skipped():
    off = BLOG.model_copy(update={"enabled": False})
    mentions, report = run(off)
    assert mentions == [] and report.skipped and "disabled" in report.skip_reason


def test_default_config_has_no_feeds_and_is_skipped():
    collector = RssCollector(CollectContext(config=make_config(), http=make_http(), now=NOW))
    assert collector.enabled() == (False, "no feeds in config/feeds.yaml")


@respx.mock
def test_disabled_feeds_are_not_fetched():
    off = IG.model_copy(update={"enabled": False})
    respx.get(BLOG.url).mock(return_value=xml_response(body("generic_blog.rss")))
    mentions, report = run(off, BLOG)  # respx raises on the unmocked Instagram URL if it were fetched
    assert len(mentions) == 2 and report.ok_units == 1 and report.errors == []


# ------------------------------------------------------------------ dates


def _feed_with_dates(*dates: str | None) -> bytes:
    items = []
    for i, date in enumerate(dates):
        pub = f"<pubDate>{date}</pubDate>" if date else ""
        items.append(f"<item><title>Post {i}</title><link>https://b.example.com/{i}</link>{pub}</item>")
    return f"<?xml version='1.0'?><rss version='2.0'><channel><title>B</title>{''.join(items)}</channel></rss>".encode()


@respx.mock
def test_old_entries_are_skipped_and_future_dates_clamped():
    assert MAX_ENTRY_AGE.days == 30
    respx.get(BLOG.url).mock(
        return_value=xml_response(
            _feed_with_dates(
                "Thu, 03 Sep 2026 13:00:00 GMT",  # 29d 23h old -> kept
                "Thu, 03 Sep 2026 11:00:00 GMT",  # 30d 1h old -> skipped
                "Mon, 05 Oct 2026 12:00:00 GMT",  # in the future -> now
                None,  # no date -> now
                "not a date",
            )
        )
    )
    mentions, _ = run(BLOG)
    assert [m.title for m in mentions] == ["Post 0", "Post 2", "Post 3", "Post 4"]
    assert [m.created_at for m in mentions[1:]] == [NOW, NOW, NOW]


def test_entry_time_ignores_broken_struct_times():
    entry = {
        "title": "x",
        "published_parsed": (2026, 13, 45, 0, 0, 0),
        "updated_parsed": (2026, 10, 1, 8, 0, 0),
    }
    mention = entry_to_mention(entry, {}, BLOG, now=NOW)
    assert mention.created_at == datetime(2026, 10, 1, 8, tzinfo=UTC)


# ------------------------------------------------------------------ ids, authors, details


def test_normalize_id():
    assert normalize_id("  3f2b8c1d9e7a4b6c8d0e1f2a3b4c5d6e ") == "3f2b8c1d9e7a4b6c8d0e1f2a3b4c5d6e"
    assert normalize_id("https://www.Example.com/a/b/") == "example.com/a/b"
    assert normalize_id("http://example.com/a/b#frag") == "example.com/a/b"
    long_id = "x" * 65
    assert normalize_id(long_id) == hashlib.sha1(long_id.encode()).hexdigest()[:16]
    odd = "post 12, part 2"
    assert normalize_id(odd) == hashlib.sha1(odd.encode()).hexdigest()[:16]
    assert len(normalize_id("ümlaut")) == 16


def test_native_ids_from_links_and_entries_without_identity():
    channel = {"title": "Mixed links"}
    cases = {
        "https://youtu.be/dQw4w9WgXcQ": "dQw4w9WgXcQ",
        "https://www.youtube.com/watch?feature=share&v=dQw4w9WgXcQ": "dQw4w9WgXcQ",
        "https://www.instagram.com/lighthouse.games/reel/C9xYz123AbC/": "C9xYz123AbC",
        "https://www.tiktok.com/@someone/photo/7550000000000000002?lang=en": "7550000000000000002",
    }
    for link, expected in cases.items():
        mention = entry_to_mention({"link": link, "id": "guid-1"}, channel, BLOG, now=NOW)
        assert mention.source_id == expected

    assert entry_to_mention({"summary": "<p>no id, link or title</p>"}, channel, BLOG, now=NOW) is None
    titled = entry_to_mention({"title": "Only a title"}, channel, BLOG, now=NOW)
    assert titled.source_id == hashlib.sha1(b"Only a title").hexdigest()[:16]
    assert titled.url == BLOG.url  # no link anywhere -> the configured feed URL


def test_authors_for_generic_and_youtube_feeds():
    generic_platform_name = {"title": "x", "link": "https://b.example.com/1", "author": "Instagram"}
    assert entry_to_mention(generic_platform_name, {}, BLOG, now=NOW).author == "Friendslop Weekly"
    yt_entry = {
        "title": "x",
        "yt_videoid": "dQw4w9WgXcQ",
        "link": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    }
    assert entry_to_mention(yt_entry, {"title": "Channel Title"}, YT, now=NOW).author == "Channel Title"
    assert entry_to_mention(yt_entry, {}, YT, now=NOW).author == "Tiny Pixel Devlogs"
    tiktok_profile = {"title": "TikTok feed", "link": "https://www.tiktok.com/@lighthousegames"}
    entry = {"title": "x", "link": "https://www.tiktok.com/t/ZT8abc/", "author": "TikTok"}
    assert entry_to_mention(entry, tiktok_profile, TT, now=NOW).author == "@lighthousegames"


def test_stats_that_are_not_numbers_are_ignored():
    entry = {
        "title": "x",
        "yt_videoid": "dQw4w9WgXcQ",
        "media_starrating": {"count": "n/a"},
        "media_statistics": {"views": "1,204"},
    }
    mention = entry_to_mention(entry, {}, YT, now=NOW)
    assert mention.engagement.likes == 0 and mention.extra["views"] == 1204
    assert mention.extra["engagement_known"] is True


def test_thumbnail_fallbacks_and_link_cleanup():
    entry = {
        "title": "Plain <b>title</b>",
        "title_detail": {"type": "text/plain"},
        "link": "https://b.example.com/post",
        "media_thumbnail": [{"url": "data:image/png;base64,AAAA"}],
        "media_content": [{"url": "https://b.example.com/clip.mp4", "type": "video/mp4"}],
        "enclosures": [{"href": "https://b.example.com/ep.mp3", "type": "audio/mpeg"}],
        "summary": (
            "<p>See (https://en.wikipedia.org/wiki/Lethal_Company_(game)), C# tips, "
            "<a href='mailto:dev@example.com'>mail</a> and https://b.example.com/x?a=1&amp;b=2!</p>"
            "<img alt='' src='//cdn.example.com/a.png?w=1&amp;h=2'>"
        ),
        "content": [
            {"type": "text/html", "value": ""},
            {"type": "text/plain", "value": "plain #CoOp text https://b.example.com/plain"},
        ],
        "tags": [{"term": "#Horror"}, {"term": None}, {"term": "  "}],
    }
    mention = entry_to_mention(entry, {}, BLOG, now=NOW)
    assert mention.title == "Plain <b>title</b>"  # text/plain titles are not stripped
    assert mention.media_thumb == "https://cdn.example.com/a.png?w=1&h=2"
    assert mention.links == [
        "https://b.example.com/post",
        "https://en.wikipedia.org/wiki/Lethal_Company_(game)",
        "https://b.example.com/x?a=1&b=2",
        "https://b.example.com/plain",
    ]
    assert mention.raw_tags == ["coop", "horror"]


@respx.mock
def test_empty_items_are_ignored_and_harmless_encoding_quirks_do_not_warn():
    # declared UTF-8 but contains a Latin-1 byte: feedparser falls back (CharacterEncodingOverride)
    latin1 = (
        b"<?xml version='1.0' encoding='utf-8'?><rss version='2.0'><channel><title>B</title>"
        b"<item></item>"
        b"<item><title>Caf\xe9 co-op night</title><link>https://b.example.com/cafe</link></item>"
        b"</channel></rss>"
    )
    parsed = parse_feed(latin1)
    assert parsed.bozo and type(parsed.bozo_exception).__name__ == "CharacterEncodingOverride"
    respx.get(BLOG.url).mock(return_value=xml_response(latin1))
    mentions, report = run(BLOG)
    assert [m.title for m in mentions] == ["Café co-op night"]
    assert report.warnings == [] and report.errors == []
