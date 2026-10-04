"""Stage B enrichment: fetch top comments / replies and author audiences for shortlisted games.

Only the game's most-discussed posts are sampled (``max_posts``, default 2), comments are
re-fetched at most every ``run.comment_refresh_hours`` unless the post's comment count
grew by 25% or more, and every fetch goes through ``Collector.safe_fetch_comments`` /
``safe_fetch_audience`` so a failing platform never breaks the run. A failed fetch keeps
the previous signals and does not mark the post as fetched, so the next run retries it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta

from gembot.collectors.base import Collector
from gembot.config import Settings
from gembot.enrich.signals import analyze_comments, merge_signals
from gembot.models import CommentSignals, Game, Mention

SOURCE_TO_COLLECTOR: dict[str, str] = {
    "reddit": "reddit",
    "bluesky": "bluesky",
    "x": "x",
    "steam": "steam",
    "itch": "itch",
    "instagram": "rss",
    "tiktok": "rss",
    "youtube": "rss",
    "rss": "rss",
}
# Platforms whose reported comment count is unreliable/absent: worth asking even at 0.
ALWAYS_FETCH_SOURCES = frozenset({"bluesky", "reddit"})
REFRESH_GROWTH = 1.25  # re-fetch early when the comment count grew by 25% or more
MAX_AUDIENCE_LOOKUPS = 2


def collector_for(mention: Mention, collectors: Mapping[str, Collector]) -> Collector | None:
    return collectors.get(SOURCE_TO_COLLECTOR.get(mention.source, mention.source))


def _game_mentions(game: Game, mentions: dict[str, Mention]) -> list[Mention]:
    return [mentions[key] for key in game.mention_keys if key in mentions]


def _needs_refresh(mention: Mention, now: datetime, settings: Settings) -> bool:
    if mention.signals is None or mention.comments_fetched_at is None:
        return True
    if now - mention.comments_fetched_at >= timedelta(hours=settings.run.comment_refresh_hours):
        return True
    before = mention.signals.post_comment_count
    current = mention.engagement.comments
    return current >= max(before * REFRESH_GROWTH, before + 1)


def enrich_game_comments(
    game: Game,
    mentions: dict[str, Mention],
    collectors: Mapping[str, Collector],
    *,
    now: datetime,
    settings: Settings,
    max_posts: int = 2,
) -> CommentSignals:
    """Sample comments for the game's most-discussed posts; return merged signals for the game."""
    own = _game_mentions(game, mentions)
    eligible: list[tuple[Mention, Collector]] = []
    for mention in own:
        collector = collector_for(mention, collectors)
        if collector is not None and (
            mention.engagement.comments > 0 or mention.source in ALWAYS_FETCH_SOURCES
        ):
            eligible.append((mention, collector))
    eligible.sort(key=lambda pair: (-pair[0].engagement.comments, -pair[0].engagement.total, pair[0].key))
    for mention, collector in eligible[: max(max_posts, 0)]:
        if not _needs_refresh(mention, now, settings):
            continue
        warnings_before = len(collector.report.warnings)
        comments = collector.safe_fetch_comments(mention, settings.run.max_comments_per_post)
        if not comments and len(collector.report.warnings) > warnings_before:
            continue  # the fetch failed: keep the old signals, retry next run
        mention.comments = comments
        mention.comments_fetched_at = now
        mention.signals = analyze_comments(
            comments, post_author=mention.author, post_comment_count=mention.engagement.comments
        )
    return merge_signals([m.signals for m in own if m.signals is not None])


def _implements_audience(collector: Collector) -> bool:
    return type(collector).fetch_audience is not Collector.fetch_audience


def enrich_audience(game: Game, mentions: dict[str, Mention], collectors: Mapping[str, Collector]) -> int:
    """Fill ``author_audience`` for up to two of the game's most-engaged posts; returns how many."""
    missing = []
    for mention in _game_mentions(game, mentions):
        collector = collector_for(mention, collectors)
        if mention.author_audience is None and collector is not None and _implements_audience(collector):
            missing.append((mention, collector))
    missing.sort(key=lambda pair: (-pair[0].engagement.total, pair[0].key))
    filled = 0
    for mention, collector in missing[:MAX_AUDIENCE_LOOKUPS]:
        audience = collector.safe_fetch_audience(mention)
        if audience is not None:
            mention.author_audience = int(audience)
            filled += 1
    return filled
