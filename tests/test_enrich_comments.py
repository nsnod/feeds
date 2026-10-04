"""Stage B comment / audience enrichment with fake collectors."""

from __future__ import annotations

from datetime import timedelta

import pytest

from gembot.collectors.base import CollectContext, Collector
from gembot.enrich.comments import SOURCE_TO_COLLECTOR, collector_for, enrich_audience, enrich_game_comments
from gembot.enrich.signals import analyze_comments
from gembot.http import HttpError
from gembot.models import Comment, Game, Mention
from tests.factories import NOW, make_comments, make_config, make_game, make_http, make_mention


class FakeCollector(Collector):
    name = "fake"

    def __init__(
        self, ctx, comments: dict[str, list[Comment]] | None = None, audience: dict[str, int] | None = None
    ):
        super().__init__(ctx)
        self.comments = comments or {}
        self.audience = audience or {}
        self.comment_calls: list[tuple[str, int]] = []
        self.audience_calls: list[str] = []
        self.fail = False

    def collect(self):
        return []

    def fetch_comments(self, mention, limit):
        self.comment_calls.append((mention.key, limit))
        if self.fail:
            raise HttpError("HTTP 500", 500)
        return list(self.comments.get(mention.key, []))

    def fetch_audience(self, mention):
        self.audience_calls.append(mention.key)
        if self.fail:
            raise HttpError("HTTP 403", 403)
        return self.audience.get(mention.key)


class NoAudienceCollector(Collector):
    name = "steam"

    def collect(self):
        return []


@pytest.fixture
def settings():
    return make_config().settings


@pytest.fixture
def ctx():
    return CollectContext(config=make_config(), http=make_http(), now=NOW)


def make_fake(ctx, name: str, **kw) -> FakeCollector:
    cls = type(f"Fake_{name}", (FakeCollector,), {"name": name})
    return cls(ctx, **kw)


def game_with(*mentions: Mention) -> tuple[Game, dict[str, Mention]]:
    store = {m.key: m for m in mentions}
    return make_game(mention_keys=[m.key for m in mentions] + ["reddit:pruned"]), store


def test_source_to_collector_mapping():
    assert (
        SOURCE_TO_COLLECTOR["instagram"]
        == SOURCE_TO_COLLECTOR["tiktok"]
        == SOURCE_TO_COLLECTOR["youtube"]
        == "rss"
    )
    assert {k: v for k, v in SOURCE_TO_COLLECTOR.items() if v != "rss"} == {
        "reddit": "reddit",
        "bluesky": "bluesky",
        "x": "x",
        "steam": "steam",
        "itch": "itch",
    }


def test_fetches_top_posts_and_returns_merged_signals(ctx, settings):
    big = make_mention("reddit", "big", comments=80, likes=500, author="dev")
    mid = make_mention("bluesky", "mid", comments=12, likes=40, author="dev.bsky.social")
    small = make_mention("reddit", "small", comments=3, author="dev")
    reddit = make_fake(
        ctx,
        "reddit",
        comments={
            big.key: make_comments(
                ["Wishlisted!", "roblox clone lol", "thanks all!"], authors=["a", "b", "dev"]
            ),
            small.key: make_comments(["need this"]),
        },
    )
    bluesky = make_fake(ctx, "bluesky", comments={mid.key: make_comments(["take my money", "asset flip"])})
    game, store = game_with(small, mid, big)
    signals = enrich_game_comments(
        game, store, {"reddit": reddit, "bluesky": bluesky}, now=NOW, settings=settings
    )
    assert reddit.comment_calls == [(big.key, settings.run.max_comments_per_post)]
    assert bluesky.comment_calls == [(mid.key, settings.run.max_comments_per_post)]
    assert small.signals is None and small.comments_fetched_at is None
    assert big.comments_fetched_at == NOW and len(big.comments) == 3
    assert big.signals.sampled == 2  # the post author's own reply is ignored
    assert big.signals.post_comment_count == 80
    assert (
        signals.intent_commenters == 2 and signals.roblox_commenters == 1 and signals.negative_commenters == 1
    )
    assert signals.post_comment_count == 92


def test_eligibility_rules(ctx, settings):
    yt_quiet = make_mention("youtube", "yt0", comments=0, likes=900)
    yt_loud = make_mention("youtube", "yt1", comments=4)
    reddit_zero = make_mention("reddit", "r0", comments=0)
    x_post = make_mention("x", "x1", comments=50)  # no X collector configured
    rss = make_fake(ctx, "rss")
    reddit = make_fake(ctx, "reddit")
    game, store = game_with(yt_quiet, yt_loud, reddit_zero, x_post)
    enrich_game_comments(game, store, {"rss": rss, "reddit": reddit}, now=NOW, settings=settings, max_posts=5)
    assert [k for k, _ in rss.comment_calls] == [yt_loud.key]
    assert [k for k, _ in reddit.comment_calls] == [reddit_zero.key]  # reddit/bluesky are asked even at 0
    assert yt_quiet.signals is None and x_post.signals is None
    assert collector_for(x_post, {"rss": rss}) is None


def test_max_posts_ordering_and_zero(ctx, settings):
    posts = [
        make_mention("reddit", f"p{i}", comments=c, likes=lk)
        for i, (c, lk) in enumerate([(5, 1), (5, 9), (9, 0)])
    ]
    reddit = make_fake(ctx, "reddit")
    game, store = game_with(*posts)
    enrich_game_comments(game, store, {"reddit": reddit}, now=NOW, settings=settings, max_posts=2)
    assert [k for k, _ in reddit.comment_calls] == ["reddit:p2", "reddit:p1"]
    reddit.comment_calls.clear()
    enrich_game_comments(game, store, {"reddit": reddit}, now=NOW, settings=settings, max_posts=0)
    assert reddit.comment_calls == []


def test_refresh_rules(ctx, settings):
    def fetched(hours_ago: float, count_then: int, count_now: int, with_signals: bool = True) -> Mention:
        m = make_mention(
            "reddit", f"m{hours_ago}-{count_then}-{count_now}-{with_signals}", comments=count_now
        )
        m.comments_fetched_at = NOW - timedelta(hours=hours_ago)
        if with_signals:
            m.signals = analyze_comments([], post_comment_count=count_then)
        return m

    fresh_same = fetched(2, 40, 45)  # +12.5%: skip
    fresh_grown = fetched(2, 40, 50)  # +25%: refetch
    stale = fetched(7, 40, 40)  # older than comment_refresh_hours (6): refetch
    no_signals = fetched(1, 0, 10, with_signals=False)  # never analysed: fetch
    from_zero = fetched(1, 0, 1)  # 0 -> 1 counts as growth
    reddit = make_fake(ctx, "reddit")
    for m in (fresh_same, fresh_grown, stale, no_signals, from_zero):
        game, store = game_with(m)
        enrich_game_comments(game, store, {"reddit": reddit}, now=NOW, settings=settings)
    called = {k for k, _ in reddit.comment_calls}
    assert called == {fresh_grown.key, stale.key, no_signals.key, from_zero.key}
    assert fresh_same.comments_fetched_at == NOW - timedelta(hours=2)
    assert stale.comments_fetched_at == NOW and stale.signals.post_comment_count == 40


def test_failed_fetch_keeps_old_signals_and_other_sources_still_work(ctx, settings):
    broken_post = make_mention("reddit", "broken", comments=30)
    old = analyze_comments(make_comments(["wishlisted"]), post_comment_count=10)
    broken_post.signals = old
    broken_post.comments_fetched_at = NOW - timedelta(hours=12)
    ok_post = make_mention("bluesky", "ok", comments=5)
    reddit = make_fake(ctx, "reddit")
    reddit.fail = True
    bluesky = make_fake(ctx, "bluesky", comments={ok_post.key: make_comments(["need this"])})
    game, store = game_with(broken_post, ok_post)
    signals = enrich_game_comments(
        game, store, {"reddit": reddit, "bluesky": bluesky}, now=NOW, settings=settings
    )
    assert reddit.report.warnings and "comments for reddit:broken" in reddit.report.warnings[0]
    assert broken_post.signals is old and broken_post.comments_fetched_at == NOW - timedelta(hours=12)
    assert ok_post.signals is not None and ok_post.comments_fetched_at == NOW
    assert signals.intent_commenters == 2  # old reddit signals + new bluesky signals


def test_empty_but_successful_fetch_is_recorded(ctx, settings):
    post = make_mention("bluesky", "quiet", comments=0)
    bluesky = make_fake(ctx, "bluesky")
    game, store = game_with(post)
    signals = enrich_game_comments(game, store, {"bluesky": bluesky}, now=NOW, settings=settings)
    assert post.comments_fetched_at == NOW and post.signals is not None and post.signals.sampled == 0
    assert signals.sampled == 0


def test_enrich_audience_fills_two_most_engaged(ctx):
    a = make_mention("bluesky", "a", likes=100, author="a.bsky.social")
    b = make_mention("bluesky", "b", likes=50, author="b.bsky.social")
    c = make_mention("bluesky", "c", likes=10, author="c.bsky.social")
    known = make_mention("bluesky", "known", likes=1000, audience=77)
    steam = make_mention("steam", "1", likes=5000)
    unknown = make_mention("bluesky", "none", likes=75)
    bluesky = make_fake(ctx, "bluesky", audience={a.key: 1200, b.key: 300, c.key: 5})
    game, store = game_with(a, b, c, known, steam, unknown)
    collectors = {"bluesky": bluesky, "steam": NoAudienceCollector(ctx)}
    assert enrich_audience(game, store, collectors) == 1  # a filled, "none" returned None
    assert bluesky.audience_calls == [a.key, unknown.key]
    assert a.author_audience == 1200 and unknown.author_audience is None and known.author_audience == 77
    assert enrich_audience(game, store, collectors) == 1  # next most engaged lacking audience: b
    assert b.author_audience == 300


def test_enrich_audience_errors_are_isolated(ctx):
    post = make_mention("bluesky", "a", likes=3)
    bluesky = make_fake(ctx, "bluesky")
    bluesky.fail = True
    game, store = game_with(post)
    assert enrich_audience(game, store, {"bluesky": bluesky}) == 0
    assert post.author_audience is None and bluesky.report.warnings
    assert enrich_audience(game, store, {}) == 0
