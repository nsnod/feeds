"""Configuration: ``config/*.yaml`` + environment variables, validated with pydantic.

* ``settings.yaml``  - thresholds, caps, budgets, weights (every number lives here)
* ``sources.yaml``   - subreddits, search terms, Steam searches, itch feeds
* ``feeds.yaml``     - user-added RSS feeds (Instagram/TikTok via RSS.app, YouTube, ...)
* ``blocklist.yaml`` - big publishers/studios and banned keywords

A key written twice is an error in settings / sources / blocklist (plain YAML would quietly
keep the last one). ``feeds.yaml`` is edited by hand on github.com, so it is read leniently:
every mistake becomes a plain-English line in ``Feeds.problems`` and the feeds that are fine
still load (the RSS source and ``python -m gembot check-config`` report the problems).

Secrets only ever come from the environment (GitHub Actions secrets).
"""

from __future__ import annotations

import difflib
import logging
import os
import re
from collections.abc import Iterable, Iterator, Mapping
from datetime import date
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic_core import PydanticCustomError

from gembot.models import FEATURE_NAMES, SOURCES

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


_THOUSANDS_RE = re.compile(r"\d{1,3}(?:[, _]\d{3})+")  # 8,357 / 8 357
_DOTTED_THOUSANDS_RE = re.compile(r"\d{1,3}(?:\.\d{3})+")  # 25.000 / 1.200.000 (European style)
_SHORT_COUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([kKmM])")  # 25K / 1.2M


class FeedConfig(_Cfg):
    name: str
    url: str
    source: str = "rss"  # instagram | tiktok | youtube | rss | ...
    audience: int | None = Field(default=None, gt=0)
    enabled: bool = True

    @field_validator("source")
    @classmethod
    def _lower(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("audience", mode="before")
    @classmethod
    def _follower_count(cls, value: Any) -> Any:
        """Follower counts as profiles show them: ``8,357``, ``25K``, ``1.2M``. ``true`` is not one
        (pydantic would read it as 1)."""
        if isinstance(value, bool):
            raise PydanticCustomError("int_type", "Input should be a valid integer")
        if not isinstance(value, str):
            return value
        text = value.strip()
        if _THOUSANDS_RE.fullmatch(text):
            return int(re.sub(r"[, _]", "", text))
        match = _SHORT_COUNT_RE.fullmatch(text)
        if match:
            return round(float(match.group(1)) * (1_000 if match.group(2) in "kK" else 1_000_000))
        return value


class Feeds(_Cfg):
    feeds: list[FeedConfig] = Field(default_factory=list)
    # Mistakes found while reading feeds.yaml ("line 38: ..."). Only load_feeds fills this, a
    # "problems:" key in the file is itself reported as a problem.
    problems: list[str] = Field(default_factory=list)

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


_MERGE_TAG = "tag:yaml.org,2002:merge"
_VALUE_TAG = "tag:yaml.org,2002:value"
_STR_TAG = "tag:yaml.org,2002:str"
_NULL_TAG = "tag:yaml.org,2002:null"


class _YamlMap(dict):
    """A mapping read by :class:`_LineLoader`: knows its line and each key's line (1-based)."""

    def __init__(self, line: int) -> None:
        super().__init__()
        self.line = line
        self.key_lines: dict[Any, int] = {}


class _YamlList(list):
    """A sequence read by :class:`_LineLoader`: knows its line and each item's line (1-based)."""

    def __init__(self, line: int, item_lines: list[int]) -> None:
        super().__init__()
        self.line = line
        self.item_lines = item_lines


class _LineLoader(yaml.SafeLoader):
    """Builds :class:`_YamlMap` / :class:`_YamlList`, so a problem in feeds.yaml can name its line.

    Typed values are built leniently: a date (``name: 2026-02-30``), a number YAML cannot build
    (``!!int abc``) or a dotted follower count (``25.000``) stays the text it was, so the feed's
    own checks report it on its line instead of one bad value losing the whole file.
    """


def _construct_map(loader: _LineLoader, node: yaml.MappingNode) -> Iterator[_YamlMap | str]:
    if isinstance(node, yaml.ScalarNode):  # "!!map text": the text, checked like any other value
        yield loader.construct_scalar(node)
        return
    data = _YamlMap(node.start_mark.line + 1)
    yield data
    data.update(loader.construct_mapping(node))
    for key_node, _ in node.value:  # repeats are gone; merged keys come first, own keys win
        key = loader.construct_object(key_node)  # already built: this is a cache lookup
        data.key_lines[key] = key_node.start_mark.line + 1


def _construct_list(loader: _LineLoader, node: yaml.SequenceNode) -> Iterator[_YamlList | str]:
    if isinstance(node, yaml.ScalarNode):  # "!!seq text"
        yield loader.construct_scalar(node)
        return
    if not isinstance(node, yaml.SequenceNode):  # "!!seq {a: 1}": a YAML error with its line
        raise yaml.constructor.ConstructorError(
            None, None, f"expected a list, but found {node.id}", node.start_mark
        )
    data = _YamlList(node.start_mark.line + 1, [item.start_mark.line + 1 for item in node.value])
    yield data
    data.extend(loader.construct_sequence(node))


def _construct_text(loader: _LineLoader, node: yaml.ScalarNode) -> str:
    return loader.construct_scalar(node)


def _lenient(build: Any, *errors: type[Exception]) -> Any:
    def construct(loader: _LineLoader, node: yaml.ScalarNode) -> Any:
        try:
            return build(loader, node)
        except errors:
            return loader.construct_scalar(node)

    return construct


def _construct_float(loader: _LineLoader, node: yaml.ScalarNode) -> Any:
    if isinstance(node.value, str) and _DOTTED_THOUSANDS_RE.fullmatch(node.value):
        return node.value  # "25.000": YAML would say 25.0; the feed check reads it as 25000, with a note
    return yaml.constructor.SafeConstructor.construct_yaml_float(loader, node)


_LineLoader.add_constructor("tag:yaml.org,2002:map", _construct_map)
_LineLoader.add_constructor("tag:yaml.org,2002:seq", _construct_list)
_LineLoader.add_constructor("tag:yaml.org,2002:timestamp", _construct_text)
_LineLoader.add_constructor("tag:yaml.org,2002:float", _lenient(_construct_float, ValueError))
_LineLoader.add_constructor(
    "tag:yaml.org,2002:int", _lenient(yaml.constructor.SafeConstructor.construct_yaml_int, ValueError)
)
_LineLoader.add_constructor(
    "tag:yaml.org,2002:bool", _lenient(yaml.constructor.SafeConstructor.construct_yaml_bool, KeyError)
)


def _parse_yaml(
    text: str, loader_class: type[yaml.SafeLoader], *, join_feeds: bool = False
) -> tuple[Any, list[tuple[str, int, int]], list[tuple[int, str]]]:
    """``(data, repeated keys, problems from reading the 'feeds:' lists)``; raises ``yaml.YAMLError``.

    A key written twice keeps its first value (plain YAML silently keeps the last one, which
    once turned a stray ``feeds: [2]`` at the end of feeds.yaml into "no feeds at all"). With
    ``join_feeds`` (feeds.yaml) every top-level ``feeds:`` list is read, as one list, and a feed
    typed without its leading ``- `` is split off the feed above it.
    """
    loader = loader_class(text)
    try:
        root = loader.get_single_node()
        if root is None:
            return None, [], []
        problems = (_join_feeds_lists(loader, root) + _split_run_on_feeds(root)) if join_feeds else []
        repeated = _drop_repeated_keys(loader, root)
        return loader.construct_document(root), repeated, problems
    finally:
        loader.dispose()


def _drop_repeated_keys(loader: yaml.SafeLoader, root: yaml.Node) -> list[tuple[str, int, int]]:
    """Keep only the first of each key written twice in one mapping; returns
    ``(key, first line, repeated line)`` sorted by line (1-based).

    Runs on the parsed tree before anything is built, so a ``<<: *defaults`` merge has not been
    copied in yet: overriding a merged key is fine and never counts as a repeat. Each node is
    checked once, however many ``*aliases`` point at it.
    """
    repeated: list[tuple[str, int, int]] = []
    checked: set[int] = set()
    todo = [root]
    while todo:
        node = todo.pop()
        if id(node) in checked:
            continue
        checked.add(id(node))
        if isinstance(node, yaml.SequenceNode):
            todo.extend(node.value)
        elif isinstance(node, yaml.MappingNode):
            first_lines: dict[Any, int] = {}
            kept: list[tuple[yaml.Node, yaml.Node]] = []
            for key_node, value_node in node.value:
                if isinstance(key_node, yaml.ScalarNode) and key_node.tag not in (_MERGE_TAG, _VALUE_TAG):
                    key = loader.construct_object(key_node, deep=True)
                    line = key_node.start_mark.line + 1
                    if key in first_lines:
                        repeated.append((str(key), first_lines[key], line))
                        continue
                    first_lines[key] = line
                kept.append((key_node, value_node))
                todo += (key_node, value_node)
            node.value = kept
    return sorted(repeated, key=lambda item: (item[2], item[1]))


def _join_feeds_lists(loader: yaml.SafeLoader, root: yaml.Node) -> list[tuple[int, str]]:
    """feeds.yaml with more than one top-level ``feeds:`` (a second block further down, the
    example block uncommented, a leftover ``feeds: []``...): every list with a feed in it is
    read, in file order, as one list, so no feed is lost. Each extra ``feeds:`` line is a problem
    whose advice keeps every feed: "delete line N" alone only when the file still reads right
    without that line. Plain YAML would quietly keep only the last ``feeds:``."""
    if not isinstance(root, yaml.MappingNode):
        return []
    blocks = [(key, value) for key, value in root.value if _key_text(key) == "feeds"]
    if len(blocks) < 2:
        return []
    # the one to keep: the first with a feed in it, else the first non-empty list, else the first
    keep_key, keep_value = min(blocks, key=lambda block: -_feeds_rank(block[1]))
    keep_line = keep_key.start_mark.line + 1
    target = _list_column(keep_value)
    items: list[yaml.Node] = []
    problems: list[tuple[int, str]] = []
    lined_up: set[int] = set()  # block lists that join the kept list once their 'feeds:' line goes
    above: yaml.Node | None = None  # the value above, once the lines the advice deletes are gone
    for key, value in root.value:
        if _key_text(key) != "feeds":
            above = value
            continue
        line = key.start_mark.line + 1
        rank = _feeds_rank(value)
        if isinstance(value, yaml.SequenceNode) and (key is keep_key or rank == 2):
            items += value.value
        if key is keep_key:
            lined_up.add(id(value))
            above = value
            continue
        if rank == 2:
            # without its 'feeds:' line, a block list right under a lined-up one joins it, as long
            # as its '- ' is in the same column (else YAML reads the file as broken)
            column = _list_column(value)
            joins = above is not None and id(above) in lined_up
            shift = target - column if joins and target is not None and column is not None else None
            if shift == 0:
                advice = f"delete line {line} and keep every feed under line {keep_line}"
            elif shift is not None:
                spaces = f"{abs(shift)} space{'s' if abs(shift) > 1 else ''}"
                move = f"add {spaces} at the start of" if shift > 0 else f"remove {spaces} from the start of"
                advice = (
                    f"delete line {line} and {move} each line under it, so its '- ' lines up with the "
                    f"ones under line {keep_line}"
                )
            else:
                where = "on" if value.start_mark.line + 1 == line else "under"  # feeds: [{...}] or a list
                advice = (
                    f"move the feeds {where} line {line} into the list under line {keep_line}, then delete "
                    "that 'feeds:' line"
                )
            if shift is not None:
                lined_up.add(id(value))
            text = (
                f"a second 'feeds:' line (the first is on line {keep_line}); what is under it was read "
                f"as more feeds - {advice}"
            )
            above = value
        else:  # its advice removes it and all under it, so 'above' stays what it was
            text = _extra_feeds_line(loader, line, value, keep_line)
        problems.append((line, f"line {line}: {text}"))
    if isinstance(keep_value, yaml.SequenceNode):
        keep_value = yaml.SequenceNode(
            keep_value.tag, items, keep_value.start_mark, keep_value.end_mark, keep_value.flow_style
        )
    extra = {id(key) for key, _ in blocks if key is not keep_key}
    root.value = [
        (key, keep_value if key is keep_key else value) for key, value in root.value if id(key) not in extra
    ]
    return problems


def _extra_feeds_line(loader: yaml.SafeLoader, line: int, value: yaml.Node, keep_line: int) -> str:
    """The problem for an extra 'feeds:' line with no feed list under it. Whatever it holds that
    looks like a feed (a feed without its '- ', bare URLs) is named, so it can be moved, not lost."""
    on_its_line = value.start_mark.line + 1 == line and value.end_mark.line + 1 == line
    ignored = (
        f"ignored - delete line {line} and keep the one on line {keep_line}"
        if on_its_line
        else f"ignored - delete line {line} and the lines under it, and keep the one on line {keep_line}"
    )
    if (isinstance(value, yaml.ScalarNode) and value.tag == _NULL_TAG) or (
        isinstance(value, yaml.SequenceNode) and not value.value
    ):
        return f"an extra, empty 'feeds:' line; {ignored}"
    if isinstance(value, yaml.MappingNode):  # "feeds:" then "  name: ..." without the "- "
        fields = {_key_text(key): node for key, node in value.value if isinstance(node, yaml.ScalarNode)}
        shown = fields.get("name") or fields.get("url")
        if shown is None or not shown.value.strip():
            return f"an extra 'feeds:' line that is not a list (found a group of settings); {ignored}"
        first = _key_text(value.value[0][0]) or "name"
        return (
            f"an extra 'feeds:' line with a feed under it that has no '- ' in front "
            f"({_short(repr(shown.value))}); not read - move it into the list under line {keep_line} and "
            f"put '- ' before its '{first}:', then delete that 'feeds:' line"
        )
    nodes = value.value if isinstance(value, yaml.SequenceNode) else [value]
    texts = [
        node.value.strip()
        for node in nodes
        if isinstance(node, yaml.ScalarNode) and node.tag == _STR_TAG and node.value.strip()
    ]
    if texts:  # "feeds:" then "  - https://..." (a URL, but no name: and url: around it)
        urls = all("://" in text or text.lower().startswith("www.") for text in texts)
        what = ("a URL" if len(texts) == 1 else f"{len(texts)} URLs") if urls else "text"
        shown = ", ".join(_short(repr(text)) for text in texts[:2])
        if len(texts) > 2:
            shown += f" and {len(texts) - 2} more"
        return (
            f"an extra 'feeds:' line with {what} under it but no feed ({shown}); not read - move "
            f"{'it' if len(texts) == 1 else 'each'} into the list under line {keep_line} as a feed "
            "('- name: ...' with 'url: ...' on the line below), then delete that 'feeds:' line"
        )
    if isinstance(value, yaml.SequenceNode):
        return f"an extra 'feeds:' line with no feed in it; {ignored}"
    found = _describe(loader.construct_object(value, deep=True))
    return f"an extra 'feeds:' line that is not a list (found {found}); {ignored}"


def _feeds_rank(node: yaml.Node) -> int:
    """2: a list with a feed (``- name: ...``) in it; 1: a list without one (``[2]``); 0: else."""
    if not isinstance(node, yaml.SequenceNode) or not node.value:
        return 0
    return 2 if any(isinstance(item, yaml.MappingNode) for item in node.value) else 1


def _list_column(node: yaml.Node) -> int | None:
    """The column of the '- ' of a block list (None for anything else, e.g. ``[a, b]``)."""
    if isinstance(node, yaml.SequenceNode) and not node.flow_style and node.value:
        return node.start_mark.column
    return None


def _key_text(node: yaml.Node) -> str | None:
    """A mapping key's text (None for a key that is not plain text, e.g. ``? [a]``)."""
    return node.value if isinstance(node, yaml.ScalarNode) and node.tag == _STR_TAG else None


def _split_run_on_feeds(root: yaml.Node) -> list[tuple[int, str]]:
    """A feed typed without its leading ``- `` joins the feed above it, so 'name' (and usually
    'url' and 'source') come twice in one entry. Such an entry is split where its 'name' comes
    again: the new feed is read on its own instead of being lost, and a problem says where the
    '- ' is missing. Flow entries (``{name: a, name: b}``) are left to the repeated-key check."""
    if not isinstance(root, yaml.MappingNode):
        return []
    problems: list[tuple[int, str]] = []
    todo = [value for key, value in root.value if _key_text(key) == "feeds"]
    done: set[int] = set()
    while todo:
        node = todo.pop()
        if not isinstance(node, yaml.SequenceNode) or id(node) in done:
            continue
        done.add(id(node))
        entries: list[yaml.Node] = []
        for item in node.value:
            if not isinstance(item, yaml.MappingNode):
                entries.append(item)
                continue
            parts = [item] if item.flow_style else _split_entry(item)
            for part in parts[1:]:
                key = part.value[0][0]
                line = key.start_mark.line + 1
                problems.append(
                    (
                        line,
                        f"line {line}: a new feed starts on line {line} but is missing '- ' before "
                        f"'{key.value}:' - add it, in line with the '- ' of the feed above",
                    )
                )
            entries += parts
            # feeds indented under a stray "feeds:" inside a feed are read as feeds too
            todo += [value for part in parts for key, value in part.value if _key_text(key) == "feeds"]
        node.value = entries
    return problems


def _split_entry(node: yaml.MappingNode) -> list[yaml.MappingNode]:
    """``node`` cut before each 'name' that comes again. The keys right above that 'name' which
    the feed above already has (a ``url:`` typed first) go with the new feed."""
    pairs = node.value
    starts = [0]
    for index, (key, _) in enumerate(pairs):
        if _key_text(key) != "name":
            continue
        above = [_key_text(other) for other, _ in pairs[starts[-1] : index]]
        if "name" not in above:
            continue
        start = index
        while start - 1 > starts[-1] and above[start - 1 - starts[-1]] in above[: start - 1 - starts[-1]]:
            start -= 1
        starts.append(start)
    if len(starts) == 1:
        return [node]
    ends = [*starts[1:], len(pairs)]
    return [
        yaml.MappingNode(
            node.tag,
            pairs[start:end],
            pairs[start][0].start_mark,
            pairs[end - 1][1].end_mark,
            node.flow_style,
        )
        for start, end in zip(starts, ends, strict=True)
    ]


def _read_yaml(path: Path) -> dict[str, Any]:
    """Strict reading (settings, sources, blocklist): a silently wrong setting is worse than an error."""
    if not path.exists():
        return {}
    try:
        data, repeated, _ = _parse_yaml(path.read_text(encoding="utf-8"), yaml.SafeLoader)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name}: invalid YAML: {exc}") from exc
    if repeated:
        listed = "; ".join(
            f"'{key}' appears twice (lines {first} and {line})" for key, first, line in repeated
        )
        raise ConfigError(
            f"{path.name}: {listed} - YAML would quietly use only the last one; keep one and delete the other"
        )
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name}: expected a mapping at the top level")
    return data


# --------------------------------------------------------------------------------------
# feeds.yaml (lenient)
# --------------------------------------------------------------------------------------

FEED_FIELDS = tuple(FeedConfig.model_fields)  # name, url, source, audience, enabled
# The platforms a feed stands for. A close typo of one (yotube) is read as it; nothing is ever
# turned into another platform (twitch must not become itch, stream must not become steam).
FEED_PLATFORMS = ("instagram", "tiktok", "youtube", "rss")
# Optional fields whose bad value is left out (the feed still loads); a bad name, url or enabled
# skips the feed ("enabled: paused" must not end up fetching a feed the user meant to pause).
_LENIENT_FIELDS = frozenset({"audience", "source"})
_FRIENDLY_ERRORS = {
    "int_parsing": "must be a whole number like 25000",
    "int_from_float": "must be a whole number like 25000",
    "int_type": "must be a whole number like 25000",
    "greater_than": "must be a follower count of 1 or more",
    "string_type": "must be text (put it in quotes)",
    "bool_parsing": "must be true or false",
    "bool_type": "must be true or false",
}
_QUOTE_HINT = 'put the text after the colon in double quotes, like name: "@handle: clips"'


def load_feeds(path: Path, known_sources: Iterable[str] = SOURCES) -> Feeds:
    """Read feeds.yaml without ever raising: each feed is checked on its own, a broken one is
    skipped (or a stray key in it ignored) with a problem that names its line, and the rest load.

    ``known_sources`` are the platforms a feed's ``source`` may name (any case). A close typo of
    instagram / tiktok / youtube / rss (``yotube``) is read as that platform, with a problem; any
    other source (``twitch``, a blog's own label) is kept as written.
    """
    if not path.exists():
        return Feeds()
    try:
        text = path.read_text(encoding="utf-8")
        data, repeated, problems = _parse_yaml(text, _LineLoader, join_feeds=True)
    except yaml.YAMLError as exc:
        return Feeds(problems=[_yaml_problem(exc, text)])
    except Exception as exc:  # not UTF-8, unreadable, nested too deep...: report it, never stop a scan
        return Feeds(
            problems=[
                f"could not read the file ({type(exc).__name__}: {_short(str(exc), 200)}); no feeds were loaded"
            ]
        )
    problems += [
        (line, f"line {line}: '{key}' appears again (first on line {first}); ignored - remove the extra line")
        for key, first, line in repeated
    ]
    try:
        feeds = _read_feeds(data, problems, frozenset(source.lower() for source in known_sources))
    except Exception as exc:  # last resort (a bug here must not stop a scan either)
        return Feeds(problems=[f"could not read the file ({type(exc).__name__}); no feeds were loaded"])
    problems.sort(key=lambda item: item[0])
    return Feeds(feeds=feeds, problems=[problem for _, problem in problems])


def _read_feeds(
    data: Any, problems: list[tuple[int, str]], known_sources: frozenset[str]
) -> list[FeedConfig]:
    """The parsed file -> its valid feeds, in file order. Problems are appended."""
    if data is None:
        return []  # an empty file: no feeds, nothing wrong
    if not isinstance(data, dict):
        line = getattr(data, "line", 0)
        problems.append(
            (
                line,
                f"{_at(line)}the file must start with a 'feeds:' line followed by the list of feeds "
                f"(found {_describe(data)}); no feeds were loaded",
            )
        )
        return []
    key_lines = getattr(data, "key_lines", {})
    for key in data:
        if key != "feeds":
            line = key_lines.get(key, 0)
            problems.append(
                (
                    line,
                    f"{_at(line)}unknown top-level key '{key}' ignored - "
                    f"{_guess(key, ('feeds',))}only 'feeds:' belongs at the left edge, check the "
                    "spelling and the spaces in front",
                )
            )
    entries = data.get("feeds")
    if entries is None:
        return []  # "feeds:" with nothing under it
    if not isinstance(entries, list):
        line = key_lines.get("feeds", 0)
        problems.append(
            (
                line,
                f"{_at(line)}'feeds' must be a list of feeds, each starting with '- name:' "
                f"(found {_describe(entries)}); no feeds were loaded",
            )
        )
        return []
    feeds: list[FeedConfig] = []
    todo = _with_lines(entries)
    read_lists = {id(entries)}  # each list once, even if *aliases (or a loop) point at it again
    first_with_url: dict[str, tuple[FeedConfig, str, int]] = {}  # url key -> (feed, label, url line)
    position = 0
    while position < len(todo):
        entry, line = todo[position]
        position += 1
        feed, nested = _read_feed(entry, position, line, problems, known_sources, first_with_url)
        if feed is not None:
            feeds.append(feed)
        if nested is not None and id(nested) not in read_lists:
            read_lists.add(id(nested))
            todo[position:position] = _with_lines(nested)  # right after their feed, as in the file
    return feeds


def _with_lines(items: list[Any]) -> list[tuple[Any, int]]:
    lines = getattr(items, "item_lines", [])
    return [(item, lines[index] if index < len(lines) else 0) for index, item in enumerate(items)]


def _read_feed(
    entry: Any,
    position: int,
    line: int,
    problems: list[tuple[int, str]],
    known_sources: frozenset[str],
    first_with_url: dict[str, tuple[FeedConfig, str, int]],
) -> tuple[FeedConfig | None, list[Any] | None]:
    """One ``- name: ...`` entry -> ``(FeedConfig, or None when skipped; the feeds indented under
    a stray 'feeds:' line inside it, which are read next)``. Problems are appended.

    ``first_with_url`` holds the enabled feeds read so far by URL: a feed whose URL is already
    there is skipped (its posts would count twice, once per entry)."""
    if not isinstance(entry, dict):
        problems.append(
            (
                line,
                f"{_at(line)}feeds entry #{position} is {_describe(entry)}, not a feed; skipped - each feed "
                "starts with '- name:' and has its url and source on the lines below",
            )
        )
        return None, None
    name = entry.get("name")
    label = (
        f'feed #{position} "{_short(name)}"'
        if isinstance(name, str) and name.strip()
        else f"feed #{position}"
    )
    key_lines = getattr(entry, "key_lines", {})
    known: dict[str, Any] = {}
    nested: list[Any] | None = None
    for key, value in entry.items():
        if key in FEED_FIELDS:
            known[key] = value
            continue
        key_line = key_lines.get(key, line)
        if key == "feeds" and isinstance(value, list) and value:
            # GitHub's editor keeps the indentation of the line above, so the feeds typed after a
            # stray "feeds:" can end up under it: read them as feeds, and say how to fix the file
            nested = value
            text = (
                f"an extra 'feeds:' line ended up inside this feed, so the {len(value)} feed(s) indented "
                "under it were read as part of it (they still load) - delete that 'feeds:' line and move "
                "those feeds left so each '- name:' lines up with the others"
            )
        else:
            text = f"unknown key '{key}' ignored - {_key_hint(key)}"
        problems.append((key_line, f"{_at(key_line)}{label}: {text}"))
    audience = known.get("audience")
    if isinstance(audience, str) and _DOTTED_THOUSANDS_RE.fullmatch(audience.strip()):
        known["audience"] = number = int(audience.strip().replace(".", ""))
        audience_line = key_lines.get("audience", line)
        problems.append(
            (
                audience_line,
                f"{_at(audience_line)}{label}: audience {audience.strip()} was read as {number} - write it "
                f"as {number}, without dots",
            )
        )
    feed = _validate_feed(known, label, line, key_lines, problems)
    if feed is None:
        return None, nested
    fixed = _source_typo(feed.source) if feed.source not in known_sources else None
    if fixed is not None:
        source_line = key_lines.get("source", line)
        problems.append(
            (
                source_line,
                f"{_at(source_line)}{label}: source '{_short(feed.source, 30)}' is not a platform GemBot "
                f"knows; read as '{fixed}' - fix the spelling",
            )
        )
        feed = feed.model_copy(update={"source": fixed})
    if feed.enabled:
        url_line = key_lines.get("url", line)
        first, first_label, first_line = first_with_url.setdefault(
            _url_key(feed.url), (feed, label, url_line)
        )
        if first is not feed:
            same = first.name.strip().lower() == feed.name.strip().lower() and first.source == feed.source
            # GemBot can't tell which of the two holds the right URL (a copied block may sit above
            # or below the original), so the advice names both and blames neither.
            text = (
                f"the same feed as {first_label} (line {first_line}); read once - delete one of the two"
                if same
                else f"has the same url as {first_label} (line {first_line}), so only that one is read - "
                "one of the two needs its own URL (a copied feed whose URL wasn't changed?)"
            )
            problems.append((url_line, f"{_at(url_line)}{label}: {text}"))
            return None, nested
    return feed, nested


# Short names people use for the feed platforms (read as the platform, with a note).
_SOURCE_ABBREVIATIONS = {"ig": "instagram", "insta": "instagram", "yt": "youtube", "tt": "tiktok"}


def _source_typo(source: str) -> str | None:
    """The feed platform a misspelled ``source`` means (``yotube``, ``instagarm``, ``tik tok``, ``yt``),
    else None. Only a close typo counts: one slip in tiktok / youtube, two in instagram, none in rss."""
    squeezed = re.sub(r"[\s_-]+", "", source)
    if squeezed in _SOURCE_ABBREVIATIONS:
        return _SOURCE_ABBREVIATIONS[squeezed]
    for platform in FEED_PLATFORMS:
        allowed = 2 if len(platform) >= 8 else 1 if len(platform) >= 5 else 0
        if _typo_distance(squeezed, platform, allowed) <= allowed:
            return platform
    return None


def _typo_distance(text: str, word: str, limit: int) -> int:
    """Typing slips from ``text`` to ``word`` (a letter added, missing, changed, or two letters
    swapped), counted up to ``limit + 1``."""
    if abs(len(text) - len(word)) > limit:
        return limit + 1
    before: list[int] = []
    previous = list(range(len(word) + 1))
    for i in range(1, len(text) + 1):
        current = [i] + [0] * len(word)
        for j in range(1, len(word) + 1):
            current[j] = min(
                previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (text[i - 1] != word[j - 1])
            )
            if i > 1 and j > 1 and text[i - 1] == word[j - 2] and text[i - 2] == word[j - 1]:
                current[j] = min(current[j], before[j - 2] + 1)
        before, previous = previous, current
    return min(previous[-1], limit + 1)


def _url_key(url: str) -> str:
    """``url`` as compared to spot a feed listed twice: no scheme, the host in lower case and
    without ``www.``, no trailing ``/`` (the path and query keep their case: ids are in them)."""
    text = url.strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        return text
    if not parts.netloc:
        return text
    host = parts.netloc.lower().removeprefix("www.")
    return host + parts.path.rstrip("/") + (f"?{parts.query}" if parts.query else "")


def _validate_feed(
    known: dict[str, Any],
    label: str,
    line: int,
    key_lines: Mapping[Any, int],
    problems: list[tuple[int, str]],
) -> FeedConfig | None:
    """``known`` fields -> FeedConfig. A bad optional value (``audience: lots``) is left out and
    the feed still loads; any other bad or missing field skips the feed."""
    try:
        return FeedConfig.model_validate(known)
    except ValidationError as exc:
        errors = exc.errors()
    fields = {str(error["loc"][0]) for error in errors if error["loc"]}
    first_field = str(errors[0]["loc"][0]) if errors and errors[0]["loc"] else ""
    error_line = key_lines.get(first_field, line)
    reasons = "; ".join(_field_reason(error) for error in errors)
    if fields and fields <= _LENIENT_FIELDS:
        try:
            feed = FeedConfig.model_validate(
                {key: value for key, value in known.items() if key not in fields}
            )
        except ValidationError:
            pass  # cannot happen (they have defaults), but skipping the feed is the safe answer
        else:
            problems.append(
                (error_line, f"{_at(error_line)}{label}: {reasons}; ignored, the feed still loads")
            )
            return feed
    problems.append((error_line, f"{_at(error_line)}{label} skipped: {reasons}"))
    return None


def _key_hint(key: Any) -> str:
    if key == "feeds":
        return (
            "an extra 'feeds:' line ended up inside a feed; delete it (feeds.yaml needs exactly one "
            "'feeds:' line, at the very top)"
        )
    return (
        f"{_guess(key, FEED_FIELDS)}check its spelling and indentation (a feed has {', '.join(FEED_FIELDS)})"
    )


def _guess(key: Any, known: tuple[str, ...]) -> str:
    close = difflib.get_close_matches(str(key), known, n=1, cutoff=0.6)
    return f"did you mean '{close[0]}'? " if close else ""


def _field_reason(error: Mapping[str, Any]) -> str:
    field = ".".join(str(part) for part in error.get("loc") or ()) or "?"
    if error["type"] == "missing":
        return f"'{field}' is missing"
    value = error.get("input")
    if value is None:
        return f"'{field}' is empty"
    reason = _FRIENDLY_ERRORS.get(error["type"], str(error.get("msg", "is not valid")))
    # never repr() a list or a group: *aliases can make it billions of items long
    plain = isinstance(value, str | int | float) and not isinstance(value, bool)
    got = _short(repr(value)) if plain else _describe(value)
    return f"'{field}' {reason} (got {got})"


_UNCLOSED_QUOTE_HINT = "a quote mark there is never closed: end the text with the same quote mark"


def _yaml_problem(exc: yaml.YAMLError, text: str) -> str:
    if isinstance(exc, yaml.reader.ReaderError) and isinstance(exc.character, int):
        line = text.count("\n", 0, exc.position) + 1
        return (
            f"line {line}: not valid YAML (an invisible control character, #x{exc.character:04x}); no feeds "
            "were loaded - retype that line (the character often comes along when copying a URL)"
        )
    context = getattr(exc, "context", None) or ""
    problem = getattr(exc, "problem", None) or ""
    mark = getattr(exc, "problem_mark", None)
    runaway = _runaway_quote(text, mark) if "'\\t'" not in problem else None
    if runaway is not None:  # the quote's line, not the later (correct) line YAML tripped over
        start, end = runaway.start_mark, runaway.end_mark
        return (
            f"line {start.line + 1}, column {start.column + 1}: not valid YAML (the {runaway.style} opened "
            f"here runs on to line {end.line + 1}); no feeds were loaded - {_UNCLOSED_QUOTE_HINT}"
        )
    if context.startswith(
        ("while scanning a quoted scalar", "while parsing a flow", "while scanning a simple key")
    ):
        mark = getattr(exc, "context_mark", None) or mark  # where the quote, '[', '{' or key was opened
    what = ": ".join(filter(None, (context, problem))) or str(exc)
    where = f"line {mark.line + 1}, column {mark.column + 1}: " if mark is not None else ""
    return (
        f"{where}not valid YAML ({what}); no feeds were loaded - {_yaml_hint(context, problem, mark, text)}"
    )


def _runaway_quote(text: str, mark: Any) -> Any:
    """The quoted text that ran over its line break and swallowed the lines up to ``mark`` (its
    ``ScalarToken``), else None. A quote left open mid-file only ends at the next quote mark, so
    YAML trips over a later line that is fine; the open quote is the real mistake."""
    if mark is None:
        return None
    found = None
    try:
        for token in yaml.scan(text, Loader=yaml.SafeLoader):
            if token.start_mark.index >= mark.index:
                break
            if isinstance(token, yaml.ScalarToken) and token.style in ('"', "'"):
                found = token if token.end_mark.line > token.start_mark.line else None
    except yaml.YAMLError:
        pass  # scanning stops where reading stopped
    if found is not None and mark.line - found.end_mark.line <= 1:
        return found
    return None


def _yaml_hint(context: str, problem: str, mark: Any, text: str) -> str:
    """What to change, picked from what YAML tripped over."""
    if "'\\t'" in problem:
        return "use spaces, not tabs, at the start of that line"
    if "single document" in context:
        return "delete the '---' line (feeds.yaml is one list under 'feeds:')"
    if "quoted scalar" in context:
        return _UNCLOSED_QUOTE_HINT
    if "flow" in context:
        return "a '[' or '{' there is never closed; if it is part of a name, " + _QUOTE_HINT
    lines = text.splitlines()
    current = lines[mark.line] if mark is not None and mark.line < len(lines) else ""
    before = current[: mark.column] if mark is not None else ""
    glued = re.match(r"\s*(?:-\s+)?([^\s:#'\"]+):(?=[^\s:/])", current)
    if "simple key" in context and glued:  # url:"https://..." is one long word to YAML
        return f"put a space after '{glued.group(1)}:'"
    if re.match(r"\s*-[^\s-]", current):  # -name: "..." is a key named "-name"
        return "put a space after the '-' at the start of that line"
    if (
        "cannot start any token" in problem  # @handle, `name`, %, ...
        or any(word in context or word in problem for word in ("alias", "anchor", "tag", "block scalar"))
        or ("mapping values are not allowed" in problem and ":" in before)  # "Devlog: My Game"
    ):
        return _QUOTE_HINT
    return "check the spaces at the start of that line and the one above"


def _at(line: int) -> str:
    return f"line {line}: " if line else ""


def _describe(value: Any) -> str:
    if value is None:
        return "empty"
    if isinstance(value, bool):
        return f"the word {str(value).lower()}"
    if isinstance(value, int | float):
        return f"the number {value}"
    if isinstance(value, str):
        return f"the text {_short(repr(value))}"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, dict):
        return "a group of settings"
    return f"a {type(value).__name__}"


def _short(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def load_config(config_dir: Path | str | None = None, env: Mapping[str, str] | None = None) -> Config:
    """Load and validate every config file. Missing files fall back to defaults.

    Raises :class:`ConfigError` for settings / sources / blocklist; feeds.yaml never raises
    (its mistakes end up in ``config.feeds.problems``)."""
    env = os.environ if env is None else env
    directory = Path(config_dir or env.get("GEMBOT_CONFIG_DIR") or _default_config_dir())
    parts: dict[str, Any] = {}
    models = (("settings", Settings), ("sources", Sources), ("blocklist", Blocklist))
    for key, model in models:
        path = directory / f"{key}.yaml"
        try:
            parts[key] = model.model_validate(_read_yaml(path))
        except ConfigError:
            raise
        except Exception as exc:  # pydantic.ValidationError, ...
            raise ConfigError(f"{path.name}: {exc}") from exc
    known_sources = {*SOURCES, *parts["settings"].features.default_baseline_eph}
    parts["feeds"] = load_feeds(directory / "feeds.yaml", known_sources)
    return Config(**parts, secrets=Secrets.from_env(env), config_dir=directory)
