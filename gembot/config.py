"""Configuration: ``config/*.yaml`` + environment variables, validated with pydantic.

* ``settings.yaml``  - thresholds, caps, budgets, weights (every number lives here)
* ``sources.yaml``   - subreddits, search terms, Steam searches, itch feeds
* ``feeds.yaml``     - user-added RSS feeds (Instagram/TikTok via RSS.app, YouTube, ...)
* ``blocklist.yaml`` - big publishers/studios and banned keywords

Secrets only ever come from the environment (GitHub Actions secrets).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gembot.models import FEATURE_NAMES

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------------------
# settings.yaml
# --------------------------------------------------------------------------------------


class RunSettings(_Cfg):
    user_agent: str = "GemBot/0.1 (+https://github.com/nsnod/feeds; indie game discovery bot)"
    http_timeout_s: float = 15.0
    http_retries: int = 2  # extra attempts on 429/5xx (each one costs budget)
    max_backoff_s: float = 20.0  # never sleep longer than this for one 429
    shortlist_size: int = 40
    max_comments_per_post: int = 100
    comment_refresh_hours: float = 6.0  # re-fetch comments for a post at most this often
    # Wall-clock limits (the scan job is killed after 8 minutes): sources stop collecting after
    # collect_seconds, enrichment (comments, Steam details, links, LLM) after network_seconds,
    # so posting to Discord and saving state always get their turn.
    collect_seconds: float = 180.0
    network_seconds: float = 300.0


class Budgets(_Cfg):
    """Maximum HTTP requests per source per run."""

    steam: int = 60
    reddit: int = 60
    itch: int = 12
    bluesky: int = 40
    rss: int = 30
    x: int = 5
    resolver: int = 15  # following shortened links
    llm: int = 40
    discord: int = 150
    discord_feedback: int = 40  # reaction reads per run

    def for_source(self, name: str) -> int:
        return int(getattr(self, name, 20))


class FeatureSettings(_Cfg):
    velocity_log2_divisor: float = 4.0  # clamp(log2(v)/4): 16x normal -> 1.0
    baseline_window_days: int = 14
    baseline_min_samples: int = 8
    # engagement-per-hour fallback when a channel has too few samples
    default_baseline_eph: dict[str, float] = Field(
        default_factory=lambda: {
            "reddit": 3.0,
            "bluesky": 1.0,
            "x": 2.0,
            "youtube": 5.0,
            "instagram": 5.0,
            "tiktok": 10.0,
            "rss": 1.0,
        }
    )
    itch_list_full_hours: float = 24.0  # hours on new-and-popular that count as "full" persistence
    underdog_min_audience: int = 100
    cross_window_hours: int = 72
    cross_map: dict[int, float] = Field(default_factory=lambda: {1: 0.0, 2: 0.6, 3: 0.85, 4: 1.0})
    meme_divisor: float = 5.0
    meme_min_comments: int = 10
    hype_full_intent_commenters: int = 15  # this many distinct "I want it" commenters -> volume 1.0
    hype_confident_commenters: int = 10  # the intent *rate* counts fully from this many commenters
    velocity_min_age_hours: float = 1.0  # younger posts are treated as 1h old (early likes are noisy)
    fresh_full_hours: float = 48.0
    fresh_zero_days: float = 14.0
    fresh_coming_soon_bonus: float = 0.2


class PenaltySettings(_Cfg):
    negativity_threshold: float = 0.30
    negativity_min_commenters: int = 3  # 1 of 2 is a fluke; 3 of 4 is a pattern
    negativity_points: float = 15.0
    old_release_days: int = 30
    old_release_points: float = 20.0
    spammer_posts: int = 3  # more than this many posts...
    spammer_window_days: int = 7  # ...in this window
    spammer_points: float = 10.0


class BonusSettings(_Cfg):
    roblox_points: float = 8.0
    roblox_meme_min: float = 0.6
    roblox_velocity_min: float = 0.4
    roblox_comments_min: int = 30


class DecisionSettings(_Cfg):
    alarm_score: float = 72.0
    alarm_min_signals: int = 2
    alarm_signal_thresholds: dict[str, float] = Field(
        default_factory=lambda: {"velocity": 0.5, "hype": 0.5, "cross": 0.6, "meme": 0.6}
    )
    max_alarms_per_run: int = 3
    max_alarms_per_day: int = 12
    roundup_score: float = 45.0
    roundup_max_items: int = 8
    roundup_interval_minutes: int = 115
    roundup_lookback_hours: int = 48  # games scored within this window can enter a roundup
    heating_up_delta: float = 15.0


class FeedbackSettings(_Cfg):
    enabled: bool = True
    max_post_age_days: int = 7
    learning_rate: float = 0.05
    weight_min: float = 0.02
    weight_max: float = 0.5
    up_emoji: str = "👍"
    down_emoji: str = "👎"
    weekly_note_days: int = 7
    recheck_minutes: int = 25  # do not re-read a message's reactions more often than this


class DiscordSettings(_Cfg):
    api_base: str = "https://discord.com/api/v10"
    category_name: str = "GemBot"
    alarm_channel: str = "gem-alarm"
    roundup_channel: str = "gem-roundup"
    status_channel: str = "gembot-status"
    status_failure_threshold: int = 6
    max_links_in_alarm: int = 5


class KeepaliveSettings(_Cfg):
    """GitHub disables scheduled workflows in public repos after 60 days without activity."""

    remind_after_days: int = 45  # days since the last commit on the default branch
    remind_every_days: int = 7


class StateSettings(_Cfg):
    branch: str = "bot-state"
    games_days: int = 30
    seen_days: int = 14
    posted_days: int = 30  # Discord message records (for reactions) are kept this long
    alarm_memory_days: int = 365  # "this game already alarmed" is remembered this long
    http_cache_days: int = 7
    max_labels: int = 5000
    max_bytes: int = 5_000_000
    push_retries: int = 3


class LLMSettings(_Cfg):
    model: str = "claude-haiku-4-5-20251001"
    max_calls: int = 40
    api_base: str = "https://api.anthropic.com"
    timeout_s: float = 30.0


class Settings(_Cfg):
    run: RunSettings = Field(default_factory=RunSettings)
    budgets: Budgets = Field(default_factory=Budgets)
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "velocity": 0.25,
            "underdog": 0.15,
            "cross": 0.15,
            "fit": 0.15,
            "hype": 0.15,
            "meme": 0.05,
            "fresh": 0.10,
        }
    )
    features: FeatureSettings = Field(default_factory=FeatureSettings)
    penalties: PenaltySettings = Field(default_factory=PenaltySettings)
    bonuses: BonusSettings = Field(default_factory=BonusSettings)
    decisions: DecisionSettings = Field(default_factory=DecisionSettings)
    feedback: FeedbackSettings = Field(default_factory=FeedbackSettings)
    discord: DiscordSettings = Field(default_factory=DiscordSettings)
    state: StateSettings = Field(default_factory=StateSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    keepalive: KeepaliveSettings = Field(default_factory=KeepaliveSettings)

    @field_validator("weights")
    @classmethod
    def _check_weights(cls, value: dict[str, float]) -> dict[str, float]:
        unknown = set(value) - set(FEATURE_NAMES)
        if unknown:
            raise ValueError(f"unknown weight(s): {sorted(unknown)}; expected {list(FEATURE_NAMES)}")
        missing = set(FEATURE_NAMES) - set(value)
        if missing:
            raise ValueError(f"missing weight(s): {sorted(missing)}")
        if any(w < 0 for w in value.values()) or sum(value.values()) <= 0:
            raise ValueError("weights must be non-negative and not all zero")
        return {k: float(value[k]) for k in FEATURE_NAMES}


# --------------------------------------------------------------------------------------
# sources.yaml
# --------------------------------------------------------------------------------------


class SteamSearch(_Cfg):
    name: str
    params: dict[str, str | int] = Field(default_factory=dict)
    max_pages: int = 1


class SteamSource(_Cfg):
    enabled: bool = True
    cc: str = "us"
    lang: str = "english"
    tags: dict[str, int] = Field(default_factory=dict)  # friendly name -> Steam tag id
    searches: list[SteamSearch] = Field(default_factory=list)
    featured_categories: bool = True
    coop_category_ids: list[int] = Field(default_factory=list)
    multiplayer_category_ids: list[int] = Field(default_factory=list)
    early_access_genre_id: int = 70
    max_new_apps_per_run: int = 25
    track_followers: bool = False
    page_size: int = 50  # search/results "count" (rows per page)
    exclude_tags: list[int] = Field(default_factory=lambda: [128])  # search "untags" (128 = MMO)
    request_interval_s: float = 2.0  # pause between Steam requests (~200 req / 5 min per IP)
    appdetails_refresh_hours: float = 24.0  # reuse stored appdetails younger than this


class RedditSource(_Cfg):
    enabled: bool = True
    subreddits: list[str] = Field(default_factory=list)
    listings: list[str] = Field(default_factory=lambda: ["new", "rising"])
    limit: int = 50  # posts per OAuth listing request (Reddit max 100)
    comments_limit: int = 100
    max_post_age_hours: int = 72
    new_max_pages: int = 2  # OAuth /new: follow `after` while a full page is still all unseen posts
    # Anonymous fallback (no REDDIT_CLIENT_ID/SECRET): one combined RSS request per run.
    rss_limit: int = 100
    rss_retirement_date: date = date(2026, 11, 13)  # Reddit ends RSS on this day
    try_public_json: bool = False  # anonymous .json has been blocked (403) since 2026-05-29
    # Subscriber counts used as author_audience when the source does not report them (RSS).
    fallback_subscribers: dict[str, int] = Field(default_factory=dict)


class ItchFeed(_Cfg):
    name: str
    url: str
    ranked: bool = True  # item order reflects popularity rank
    max_pages: int = Field(default=1, ge=1, le=10)  # ?page=N pagination, 36 items per page
    # Read once (page 1) when `url` is challenged by Cloudflare or gone (404/410); None = skip.
    fallback_url: str | None = None
    fallback_keywords: list[str] = Field(default_factory=list)  # keep only fallback games matching one
    fallback_ranked: bool = False  # the fallback's item order is a meaningful rank


class ItchSource(_Cfg):
    enabled: bool = True
    feeds: list[ItchFeed] = Field(default_factory=list)
    popular_feed: str = "new-and-popular"  # feed whose rank/time-on-list drives itch velocity
    request_interval_s: float = Field(default=1.0, ge=0)  # pause between itch requests
    max_challenges: int = Field(default=2, ge=1)  # stop all itch feeds after this many blocks per run
    fallback_hold_hours: float = Field(default=6.0, ge=0)  # after a challenge, go straight to fallbacks


class BlueskySource(_Cfg):
    enabled: bool = True
    terms: list[str] = Field(default_factory=list)
    limit: int = 50
    sort: str = "latest"
    lang: str | None = "en"
    pds_host: str = "https://bsky.social"  # login server: createSession / refreshSession
    public_appview: str = "https://public.api.bsky.app"  # logged-out reads; never gets a token
    max_post_age_hours: int = 72
    appview: str = "https://api.bsky.app"  # fallback search route when the PDS proxy refuses (Bearer token)
    appview_proxy: str = "did:web:api.bsky.app#bsky_appview"  # atproto-proxy header for searches via the PDS
    search_pages: int = 1  # result pages per term (each page is one request)
    max_sessions_per_day: int = 8  # createSession calls per 24h (bsky.social allows ~10 per account per day)
    unauth_probe_hours: float = 24.0  # without a login: re-check public search at most this often


class XSource(_Cfg):
    enabled: bool = True  # still requires X_BEARER_TOKEN
    queries: list[str] = Field(default_factory=list)
    max_results: int = 25  # posts per query per run; X allows 10..100 and bills every post returned
    sort_order: Literal["recency", "relevancy"] = "recency"
    # expansions=author_id adds @usernames + follower counts, but every returned user is billed too
    expand_authors: bool = False
    # posts + expanded users returned per calendar month (UTC) before the collector pauses; None = no cap
    monthly_read_budget: int | None = 3000
    api_base: str = "https://api.x.com/2"


class ResolverSource(_Cfg):
    shortener_hosts: list[str] = Field(default_factory=list)
    fuzzy_threshold: int = 90
    lookback_days: int = 30


class Sources(_Cfg):
    steam: SteamSource = Field(default_factory=SteamSource)
    reddit: RedditSource = Field(default_factory=RedditSource)
    itch: ItchSource = Field(default_factory=ItchSource)
    bluesky: BlueskySource = Field(default_factory=BlueskySource)
    x: XSource = Field(default_factory=XSource)
    resolver: ResolverSource = Field(default_factory=ResolverSource)
    fit_keywords: dict[str, float] = Field(default_factory=dict)  # phrase -> weight (0..1)


# --------------------------------------------------------------------------------------
# feeds.yaml / blocklist.yaml
# --------------------------------------------------------------------------------------


class FeedConfig(_Cfg):
    name: str
    url: str
    source: str = "rss"  # instagram | tiktok | youtube | rss | ...
    audience: int | None = None
    enabled: bool = True

    @field_validator("source")
    @classmethod
    def _lower(cls, value: str) -> str:
        return value.strip().lower()


class Feeds(_Cfg):
    feeds: list[FeedConfig] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _none_is_empty(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("feeds") is None:
            data = {**data, "feeds": []}
        return data


class Blocklist(_Cfg):
    companies: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _none_is_empty(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = {k: (v if v is not None else []) for k, v in data.items()}
        return data


# --------------------------------------------------------------------------------------
# environment
# --------------------------------------------------------------------------------------


_SNOWFLAKE = re.compile(r"\d{15,21}")


class Secrets(BaseModel):
    """Credentials and per-install IDs, read from environment variables only."""

    model_config = ConfigDict(extra="ignore")

    discord_bot_token: str | None = None
    discord_guild_id: str | None = None
    alarm_ping_role_id: str | None = None
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    reddit_username: str | None = None
    reddit_password: str | None = None
    bluesky_handle: str | None = None
    bluesky_app_password: str | None = None
    x_bearer_token: str | None = None
    anthropic_api_key: str | None = None
    github_token: str | None = None
    github_repository: str | None = None

    @field_validator("alarm_ping_role_id", mode="after")
    @classmethod
    def _role_id(cls, value: str | None) -> str | None:
        """Discord IDs are 17-20 digit numbers. A pasted ``<@&123…>`` mention is unwrapped;
        anything else is ignored (with a warning) instead of ending up in every alarm.
        (``DISCORD_GUILD_ID`` needs no check: it is only compared with the bot's servers.)"""
        if value is None:
            return None
        bare = value.removeprefix("<@&").removesuffix(">")
        if _SNOWFLAKE.fullmatch(bare):
            return bare
        logging.getLogger("gembot.config").warning("ALARM_PING_ROLE_ID is not a Discord role ID; ignoring it")
        return None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Secrets:
        env = os.environ if env is None else env
        values: dict[str, str | None] = {}
        for field in cls.model_fields:
            raw = env.get(field.upper())
            # Tokens/keys/IDs never contain whitespace: drop it all, so a wrapped or padded paste
            # still works (and can never put a newline into an HTTP header).
            cleaned = "".join((raw or "").split())
            values[field] = cleaned or None
        return cls(**values)

    @property
    def has_reddit_oauth(self) -> bool:
        return bool(self.reddit_client_id and self.reddit_client_secret)

    @property
    def has_bluesky_login(self) -> bool:
        return bool(self.bluesky_handle and self.bluesky_app_password)

    def __repr__(self) -> str:  # never leak secrets into logs
        present = [name for name, value in self.__dict__.items() if value]
        return f"Secrets(set={present})"

    __str__ = __repr__


class Config(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    settings: Settings = Field(default_factory=Settings)
    sources: Sources = Field(default_factory=Sources)
    feeds: Feeds = Field(default_factory=Feeds)
    blocklist: Blocklist = Field(default_factory=Blocklist)
    secrets: Secrets = Field(default_factory=Secrets)
    config_dir: Path | None = None


class ConfigError(ValueError):
    pass


def _default_config_dir() -> Path:
    """``./config`` when run from a checkout, else the copy next to the package source."""
    local = Path.cwd() / "config"
    return local if (local / "settings.yaml").exists() else DEFAULT_CONFIG_DIR


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name}: invalid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name}: expected a mapping at the top level")
    return data


def load_config(config_dir: Path | str | None = None, env: Mapping[str, str] | None = None) -> Config:
    """Load and validate every config file. Missing files fall back to defaults."""
    env = os.environ if env is None else env
    directory = Path(config_dir or env.get("GEMBOT_CONFIG_DIR") or _default_config_dir())
    parts: dict[str, Any] = {}
    models = (("settings", Settings), ("sources", Sources), ("feeds", Feeds), ("blocklist", Blocklist))
    for key, model in models:
        path = directory / f"{key}.yaml"
        try:
            parts[key] = model.model_validate(_read_yaml(path))
        except ConfigError:
            raise
        except Exception as exc:  # pydantic.ValidationError, ...
            raise ConfigError(f"{path.name}: {exc}") from exc
    return Config(**parts, secrets=Secrets.from_env(env), config_dir=directory)
