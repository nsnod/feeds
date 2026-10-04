"""Shared data models.

Everything that crosses a module boundary or is persisted in state lives here, so the
collectors, resolver, scoring, Discord and state layers all agree on one vocabulary.

Timestamps are always timezone-aware UTC ``datetime`` objects.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Platform names a Mention.source can take. "rss" is a generic feed; feeds.yaml can
# label a feed as instagram / tiktok / youtube so it counts as that platform.
SOURCES: tuple[str, ...] = (
    "steam",
    "reddit",
    "itch",
    "bluesky",
    "x",
    "instagram",
    "tiktok",
    "youtube",
    "rss",
)

PLATFORM_LABELS: dict[str, str] = {
    "steam": "Steam",
    "reddit": "Reddit",
    "itch": "itch.io",
    "bluesky": "Bluesky",
    "x": "X",
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "youtube": "YouTube",
    "rss": "RSS",
}

FEATURE_NAMES: tuple[str, ...] = ("velocity", "underdog", "cross", "fit", "hype", "meme", "fresh")


def platform_label(source: str) -> str:
    return PLATFORM_LABELS.get(source, source.title())


def ensure_utc(value: datetime) -> datetime:
    """Return ``value`` as an aware UTC datetime (naive values are assumed to be UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=False)


def _utc_validator(value: Any) -> Any:
    if isinstance(value, datetime):
        return ensure_utc(value)
    return value


# --------------------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------------------


class Engagement(_Model):
    likes: int = 0  # upvotes / likes / score
    comments: int = 0  # comment / reply count reported by the platform
    shares: int = 0  # reposts / retweets / quotes
    ratio: float | None = None  # reddit upvote_ratio, when known

    @property
    def total(self) -> int:
        return max(self.likes, 0) + max(self.comments, 0) + max(self.shares, 0)


class Comment(_Model):
    id: str = ""
    author: str = ""
    text: str = ""
    score: int = 0
    created_at: datetime | None = None
    is_bot: bool = False

    _utc = field_validator("created_at", mode="before")(_utc_validator)


class Snapshot(_Model):
    """One observation of a mention's public numbers, used for growth/velocity over runs."""

    at: datetime
    likes: int = 0
    comments: int = 0
    shares: int = 0
    rank: int | None = None
    followers: int | None = None

    _utc = field_validator("at", mode="before")(_utc_validator)


class Mention(_Model):
    """One post / listing / feed item that (probably) talks about one game."""

    source: str
    source_id: str
    url: str
    title: str = ""
    text: str = ""
    author: str | None = None
    author_audience: int | None = None  # followers / subreddit subscribers / None
    created_at: datetime
    engagement: Engagement = Field(default_factory=Engagement)
    links: list[str] = Field(default_factory=list)  # outbound URLs found in the post
    media_thumb: str | None = None
    raw_tags: list[str] = Field(default_factory=list)
    channel: str | None = None  # e.g. "r/IndieDev", "itch:new-and-popular", "steam:comingsoon"
    rank: int | None = None  # 1-based position in a ranked listing (itch / steam lists)
    list_size: int | None = None  # number of items in that ranked listing
    # Set by the pipeline, not by collectors:
    first_seen: datetime | None = None
    observed_at: datetime | None = None
    history: list[Snapshot] = Field(default_factory=list)
    comments: list[Comment] = Field(default_factory=list)  # sampled during enrichment (not persisted)
    comments_fetched_at: datetime | None = None
    signals: CommentSignals | None = None  # analysis of ``comments``; persisted instead of the bodies
    game_id: str | None = None
    # Source specific payload (e.g. {"steam": SteamInfo-dict}, {"feed_name": ...}).
    extra: dict[str, Any] = Field(default_factory=dict)

    _utc = field_validator("created_at", "first_seen", "observed_at", "comments_fetched_at", mode="before")(
        _utc_validator
    )

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_id}"

    def age_hours(self, now: datetime) -> float:
        return max((now - self.created_at).total_seconds() / 3600.0, 0.0)


# --------------------------------------------------------------------------------------
# Games
# --------------------------------------------------------------------------------------


class SteamInfo(_Model):
    appid: int
    name: str = ""
    type: str | None = None  # "game", "dlc", "demo", ...
    developers: list[str] = Field(default_factory=list)
    publishers: list[str] = Field(default_factory=list)
    category_ids: list[int] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    genre_ids: list[int] = Field(default_factory=list)
    genres: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)  # user tags, when known
    release_date_text: str | None = None  # raw, e.g. "Coming soon", "Q1 2027", "14 Oct, 2026"
    release_date: date | None = None  # parsed when the text is an exact date
    coming_soon: bool = False
    early_access: bool = False
    is_free: bool = False
    price: str | None = None  # formatted, e.g. "$4.99"
    header_image: str | None = None
    short_description: str = ""
    followers: int | None = None
    fetched_at: datetime | None = None

    _utc = field_validator("fetched_at", mode="before")(_utc_validator)

    @property
    def store_url(self) -> str:
        return f"https://store.steampowered.com/app/{self.appid}/"


class LLMVerdict(_Model):
    game_title: str | None = None
    is_a_specific_game: bool = False
    friendslop_fit: float = 0.0  # 0..1
    one_line_pitch: str | None = None


class Game(_Model):
    """Every mention of one game, across all sources."""

    game_id: str  # stable forever: "steam:<appid>", "itch:<dev>/<slug>" or "t:<slug>-<hash>"
    title: str
    aliases: list[str] = Field(default_factory=list)
    developer: str | None = None
    publisher: str | None = None
    hard_ids: list[str] = Field(default_factory=list)  # e.g. ["steam:123", "itch:dev/slug"]
    steam_appid: int | None = None
    itch_url: str | None = None
    first_seen: datetime
    last_seen: datetime
    mention_keys: list[str] = Field(default_factory=list)
    steam: SteamInfo | None = None
    pitch: str | None = None
    thumb: str | None = None
    llm: LLMVerdict | None = None
    last_score: float | None = None
    last_scored_at: datetime | None = None
    last_features: Features | None = None
    last_reasons: list[str] = Field(default_factory=list)
    excluded_reason: str | None = None

    _utc = field_validator("first_seen", "last_seen", "last_scored_at", mode="before")(_utc_validator)

    def best_url(self, mentions: dict[str, Mention] | None = None) -> str | None:
        """Steam > itch > the most engaged post."""
        if self.steam_appid:
            return f"https://store.steampowered.com/app/{self.steam_appid}/"
        if self.itch_url:
            return self.itch_url
        if mentions:
            candidates = [mentions[k] for k in self.mention_keys if k in mentions]
            if candidates:
                best = max(candidates, key=lambda m: (m.engagement.total, m.created_at))
                return best.url
        return None


# --------------------------------------------------------------------------------------
# Signals, features and scores
# --------------------------------------------------------------------------------------


class CommentSignals(_Model):
    """Counts produced by enrich/signals.py from a sample of comments/replies."""

    sampled: int = 0  # comments analysed
    distinct_commenters: int = 0
    intent_comments: int = 0
    intent_commenters: int = 0
    negative_commenters: int = 0
    roblox_comments: int = 0
    roblox_commenters: int = 0
    post_comment_count: int = 0  # the platform's reported comment count for the post(s)
    intent_examples: list[str] = Field(default_factory=list)
    negative_terms: list[str] = Field(default_factory=list)
    roblox_examples: list[str] = Field(default_factory=list)

    @property
    def negative_frac(self) -> float:
        if self.distinct_commenters <= 0:
            return 0.0
        return self.negative_commenters / self.distinct_commenters


class Features(_Model):
    velocity: float = 0.0
    underdog: float = 0.0
    cross: float = 0.0
    fit: float = 0.0
    hype: float = 0.0
    meme: float = 0.0
    fresh: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {name: float(getattr(self, name)) for name in FEATURE_NAMES}


class Evidence(_Model):
    """The real numbers behind the features, used to write the "why" lines."""

    best_mention_key: str | None = None
    best_source: str | None = None
    best_channel: str | None = None
    best_likes: int = 0
    best_comments: int = 0
    best_shares: int = 0
    best_age_hours: float | None = None
    eph: float | None = None  # engagement per hour of the best post
    baseline_eph: float | None = None
    velocity_multiple: float | None = None  # eph / baseline
    best_rank: int | None = None
    rank_channel: str | None = None
    hours_on_list: float | None = None
    follower_growth: int | None = None
    audience: int | None = None
    sources_72h: list[str] = Field(default_factory=list)
    sources_24h: list[str] = Field(default_factory=list)
    fit_hits: list[str] = Field(default_factory=list)
    signals: CommentSignals = Field(default_factory=CommentSignals)
    first_seen: datetime | None = None
    release_date_text: str | None = None
    coming_soon: bool = False
    llm_fit: float | None = None
    total_comments: int = 0

    _utc = field_validator("first_seen", mode="before")(_utc_validator)


class Adjustment(_Model):
    code: str  # "negativity", "old_release", "spammer", "roblox_bonus", ...
    points: float  # always positive; penalties are subtracted, bonuses added
    detail: str = ""


class ScoreResult(_Model):
    game_id: str
    score: float = 0.0
    base: float = 0.0  # 100 * weighted mean of features, before adjustments
    bonuses: list[Adjustment] = Field(default_factory=list)
    penalties: list[Adjustment] = Field(default_factory=list)
    features: Features = Field(default_factory=Features)
    evidence: Evidence = Field(default_factory=Evidence)
    weights: dict[str, float] = Field(default_factory=dict)
    excluded: bool = False
    exclude_reason: str | None = None
    alarm_signals: list[str] = Field(default_factory=list)  # which of velocity/hype/cross/meme passed
    reasons: list[str] = Field(default_factory=list)  # 2-4 plain-English lines


class DecisionKind(StrEnum):
    ALARM = "alarm"
    ROUNDUP = "roundup"
    SKIP = "skip"


class Decision(_Model):
    game_id: str
    kind: DecisionKind
    score: float = 0.0
    escalated: bool = False  # was in a roundup before, now alarms
    would_have_alarmed: bool = False  # qualified for an alarm but a cap was hit
    heating_up: bool = False  # back in a roundup after rising >= heating_up_delta
    note: str = ""  # why skipped / extra context for logs


# --------------------------------------------------------------------------------------
# Persisted state records
# --------------------------------------------------------------------------------------


class PostedEntry(_Model):
    """One game inside one Discord message."""

    game_id: str
    score: float
    features: Features = Field(default_factory=Features)
    applied_label: float = 0.0  # label already folded into the weights


class PostedMessage(_Model):
    message_id: str
    channel_id: str
    kind: str  # "alarm" | "roundup" | "roundup_header" | "status" | "welcome" | "test"
    posted_at: datetime
    entries: list[PostedEntry] = Field(default_factory=list)
    reactions_checked_at: datetime | None = None
    last_up: int = 0
    last_down: int = 0

    _utc = field_validator("posted_at", "reactions_checked_at", mode="before")(_utc_validator)


class GamePostState(_Model):
    alarmed_at: datetime | None = None
    alarm_score: float | None = None
    roundup_at: datetime | None = None
    roundup_score: float | None = None
    would_have_alarmed: bool = False
    pending_roundup: bool = False  # carried to the next roundup (alarm cap overflow)

    _utc = field_validator("alarmed_at", "roundup_at", mode="before")(_utc_validator)


class PostedState(_Model):
    messages: dict[str, PostedMessage] = Field(default_factory=dict)
    games: dict[str, GamePostState] = Field(default_factory=dict)


class BaselineSample(_Model):
    at: datetime
    eph: float

    _utc = field_validator("at", mode="before")(_utc_validator)


class WeightChange(_Model):
    at: datetime
    weights: dict[str, float]
    reason: str = ""

    _utc = field_validator("at", mode="before")(_utc_validator)


class WeightsState(_Model):
    current: dict[str, float] = Field(default_factory=dict)  # empty -> use settings defaults
    history: list[WeightChange] = Field(default_factory=list)
    last_weekly_note_at: datetime | None = None
    weights_at_last_note: dict[str, float] = Field(default_factory=dict)

    _utc = field_validator("last_weekly_note_at", mode="before")(_utc_validator)


class LabelExample(_Model):
    at: datetime
    message_id: str
    game_id: str
    label: float  # -1..1
    up: int = 0
    down: int = 0
    features: Features = Field(default_factory=Features)
    score: float = 0.0

    _utc = field_validator("at", mode="before")(_utc_validator)


class SourceHealth(_Model):
    consecutive_failures: int = 0
    last_ok_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error: str | None = None
    alerted_broken: bool = False  # status channel was told it is broken

    _utc = field_validator("last_ok_at", "last_error_at", mode="before")(_utc_validator)


class DiscordMeta(_Model):
    guild_id: str | None = None
    category_id: str | None = None
    channels: dict[str, str] = Field(default_factory=dict)  # role ("alarm"/"roundup"/"status") -> id
    bot_user_id: str | None = None
    welcome_message_id: str | None = None


class HttpCacheEntry(_Model):
    etag: str | None = None
    last_modified: str | None = None
    at: datetime

    _utc = field_validator("at", mode="before")(_utc_validator)


class Meta(_Model):
    schema_version: int = 1
    last_run_at: datetime | None = None
    last_roundup_at: datetime | None = None
    run_count: int = 0
    daily_alarms: dict[str, int] = Field(default_factory=dict)  # "YYYY-MM-DD" -> count
    source_health: dict[str, SourceHealth] = Field(default_factory=dict)
    discord: DiscordMeta = Field(default_factory=DiscordMeta)
    http_cache: dict[str, HttpCacheEntry] = Field(default_factory=dict)
    game_aliases: dict[str, str] = Field(default_factory=dict)  # merged game_id -> surviving game_id
    reddit_baseline_ready: bool = False

    _utc = field_validator("last_run_at", "last_roundup_at", mode="before")(_utc_validator)

    def alarms_on(self, day: date) -> int:
        return self.daily_alarms.get(day.isoformat(), 0)


class State(_Model):
    """Everything persisted between runs (see state/store.py for the file layout)."""

    games: dict[str, Game] = Field(default_factory=dict)
    mentions: dict[str, Mention] = Field(default_factory=dict)
    seen: dict[str, datetime] = Field(default_factory=dict)  # mention key -> first seen
    posted: PostedState = Field(default_factory=PostedState)
    baselines: dict[str, dict[str, BaselineSample]] = Field(default_factory=dict)  # channel -> key -> sample
    weights: WeightsState = Field(default_factory=WeightsState)
    labels: list[LabelExample] = Field(default_factory=list)
    meta: Meta = Field(default_factory=Meta)


class GemCard(_Model):
    """Everything the Discord layer needs to render one game (alarm or roundup entry)."""

    game_id: str
    title: str
    url: str | None = None  # best URL: Steam > itch > post
    score: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    pitch: str | None = None
    links: list[tuple[str, str]] = Field(default_factory=list)  # (platform label, url), best first
    thumb: str | None = None
    first_seen: datetime | None = None
    escalated: bool = False
    would_have_alarmed: bool = False
    heating_up: bool = False
    test: bool = False

    _utc = field_validator("first_seen", mode="before")(_utc_validator)


Mention.model_rebuild()
Game.model_rebuild()
