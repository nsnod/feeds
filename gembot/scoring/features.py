"""Feature extraction: turn one game's mentions (+ comment signals) into seven 0..1 numbers.

Every feature is documented for humans in ``docs/SCORING.md``; the constants that are not
in ``settings.yaml`` are module-level names below so they are easy to find and test.

* ``velocity``  engagement per hour vs that channel's normal (itch: rank on the popular
  list; Steam: community follower growth when it is tracked)
* ``underdog``  big reaction for a small creator / small community
* ``cross``     how many different platforms talk about it right now
* ``fit``       friendslop / co-op fit (Steam categories, keywords, optional LLM verdict)
* ``hype``      do commenters actually want it (wishlist / "me and the boys" / day one)
* ``meme``      "it's a Roblox game" jokes (attention, not negativity)
* ``fresh``     how new the game is to us (+ bonus for an upcoming Steam page)
"""

from __future__ import annotations

import math
import re
import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache

from gembot.config import Config, Settings, Sources
from gembot.models import (
    SOURCES,
    BaselineSample,
    CommentSignals,
    Evidence,
    Features,
    Game,
    Mention,
    SteamInfo,
)

# ----------------------------------------------------------------------------- constants

#: engagement/hour assumed for a platform that has no entry in ``default_baseline_eph``
DEFAULT_FALLBACK_EPH = 1.0
#: a channel's normal is never taken as lower than this (avoids dividing by ~0)
MIN_BASELINE_EPH = 0.1
#: a post younger than this is treated as this old (``eph = total / max(age, 0.5)``)
MIN_AGE_HOURS = 0.5
#: Platforms whose mentions are listings, not posts with likes/comments.
LISTING_SOURCES = frozenset({"steam", "itch"})

# itch.io: velocity = 0.6 * rank_score + 0.4 * persistence (see docs/SCORING.md)
ITCH_RANK_SHARE = 0.6
ITCH_PERSISTENCE_SHARE = 0.4
DEFAULT_ITCH_LIST_SIZE = 30  # used when a ranked itch mention does not say how long the list was
ITCH_STALE_HOURS = 6.0  # not observed on the list for this long -> it fell off, no rank velocity

# Steam community followers (only when ``steam.track_followers`` produces snapshots)
STEAM_FOLLOWER_WINDOW_DAYS = 7
STEAM_FOLLOWER_MIN_BASE = 100  # growth is measured relative to max(followers, 100)
STEAM_FOLLOWER_MIN_SPAN_HOURS = 12.0  # short gaps are stretched to 12h so noise can't explode
STEAM_FOLLOWER_FULL_RATE = 0.5  # +50% followers per day -> velocity 1.0 (linear below)

# Steam appdetails categories -> fit strength. Any other id listed in
# ``sources.steam.coop_category_ids`` counts as COOP_CATEGORY_FIT and any id in
# ``multiplayer_category_ids`` as MULTIPLAYER_CATEGORY_FIT.
STEAM_CATEGORY_FIT: dict[int, float] = {38: 0.8, 9: 0.7, 39: 0.6, 48: 0.6}
COOP_CATEGORY_FIT = 0.7
MULTIPLAYER_CATEGORY_FIT = 0.5

# Optional LLM verdict: fit = max(LLM_BLEND * heuristic + (1 - LLM_BLEND) * llm, LLM_ALONE * llm)
LLM_BLEND = 0.5
LLM_ALONE = 0.8

_DASHES = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-"})
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Clamp to ``[lo, hi]``; NaN becomes ``lo``."""
    if value != value:  # NaN
        return lo
    return max(lo, min(hi, value))


# ----------------------------------------------------------------------------- context


@dataclass
class ScoringContext:
    """Everything feature extraction needs besides the game itself."""

    settings: Settings
    sources: Sources
    now: datetime
    baselines: dict[str, dict[str, BaselineSample]] = field(default_factory=dict)

    @classmethod
    def from_config(
        cls,
        config: Config,
        *,
        now: datetime,
        baselines: dict[str, dict[str, BaselineSample]] | None = None,
    ) -> ScoringContext:
        return cls(settings=config.settings, sources=config.sources, now=now, baselines=baselines or {})


# ----------------------------------------------------------------------------- baselines


def baseline_eph(channel: str, source: str, ctx: ScoringContext) -> tuple[float, int]:
    """The channel's normal engagement per hour: ``(median eph, number of samples used)``.

    Uses the samples of the last ``baseline_window_days``. With fewer than
    ``baseline_min_samples`` it falls back to ``default_baseline_eph[source]`` (still
    reporting how many samples it saw). The result is never below ``MIN_BASELINE_EPH``.
    """
    fs = ctx.settings.features
    cutoff = ctx.now - timedelta(days=fs.baseline_window_days)
    samples = [s.eph for s in ctx.baselines.get(channel, {}).values() if s.at >= cutoff and s.eph >= 0]
    if len(samples) >= max(fs.baseline_min_samples, 1):
        return max(float(statistics.median(samples)), MIN_BASELINE_EPH), len(samples)
    fallback = fs.default_baseline_eph.get(source, DEFAULT_FALLBACK_EPH)
    return max(float(fallback), MIN_BASELINE_EPH), len(samples)


# ----------------------------------------------------------------------------- helpers


def is_recent(mention: Mention, now: datetime, hours: float) -> bool:
    """True when the mention was created *or* first seen within the last ``hours``."""
    window = timedelta(hours=hours)
    times = [mention.created_at] + ([mention.first_seen] if mention.first_seen else [])
    return any(now - t <= window for t in times)


#: How platforms are listed in evidence and reasons ("Seen on Reddit, Bluesky and Steam").
PLATFORM_ORDER: tuple[str, ...] = (*(s for s in SOURCES if s != "steam"), "steam")


def ordered_platforms(sources: Iterable[str]) -> list[str]:
    """Distinct platforms in a stable, human-friendly order (social first, Steam last)."""
    distinct = set(sources)
    known = [s for s in PLATFORM_ORDER if s in distinct]
    return known + sorted(distinct - set(PLATFORM_ORDER))


def steam_info_for(game: Game, mentions: Iterable[Mention] = ()) -> SteamInfo | None:
    """``game.steam``, else the first Steam payload a mention carries (``extra["steam"]``)."""
    if game.steam is not None:
        return game.steam
    for mention in mentions:
        payload = mention.extra.get("steam") if mention.extra else None
        if isinstance(payload, dict):
            try:
                return SteamInfo.model_validate(payload)
            except ValueError:
                continue
    return None


def normalize_text(text: str) -> str:
    return text.translate(_DASHES).translate(_QUOTES).lower()


@lru_cache(maxsize=1024)
def phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Case-insensitive whole-word(ish) pattern: the phrase may not touch letters/digits."""
    return re.compile(r"(?<![a-z0-9])" + re.escape(normalize_text(phrase).strip()) + r"(?![a-z0-9])")


def match_fit_keywords(texts: Iterable[str], keywords: dict[str, float]) -> list[tuple[str, float]]:
    """Which fit keywords appear in ``texts``, strongest first.

    Longer phrases are matched first and "use up" the text they cover, so
    "proximity voice chat" does not also count as "proximity voice", and "co-op horror"
    does not also count as "co-op" (a separate "co-op" elsewhere still does).
    """
    blob = "\n".join(normalize_text(t) for t in texts if t)
    hits: list[tuple[str, float]] = []
    for phrase, weight in sorted(keywords.items(), key=lambda kv: (-len(kv[0]), kv[0])):
        if not phrase.strip() or weight <= 0:
            continue
        blob, count = phrase_pattern(phrase).subn(lambda m: "\x00" * len(m.group(0)), blob)
        if count:
            hits.append((phrase, clamp(float(weight))))
    hits.sort(key=lambda hw: (-hw[1], hw[0]))
    return hits


def combine_strengths(weights: Iterable[float]) -> float:
    """``1 - prod(1 - w)``: several moderate hits add up, never above 1."""
    remaining = 1.0
    for w in weights:
        remaining *= 1.0 - clamp(w)
    return clamp(1.0 - remaining)


# ----------------------------------------------------------------------------- velocity


@dataclass
class _Candidate:
    """Velocity of one mention plus the numbers behind it."""

    mention: Mention
    kind: str  # "engagement" | "itch_rank" | "steam_followers" | "none"
    value: float = 0.0
    eph: float | None = None
    baseline: float | None = None
    multiple: float | None = None
    rank: int | None = None
    hours_on_list: float | None = None
    follower_growth: int | None = None


def _engagement_velocity(mention: Mention, ctx: ScoringContext) -> _Candidate:
    age = max(mention.age_hours(ctx.now), ctx.settings.features.velocity_min_age_hours or MIN_AGE_HOURS)
    eph = mention.engagement.total / age
    channel = mention.channel or mention.source
    base, _ = baseline_eph(channel, mention.source, ctx)
    multiple = eph / base
    divisor = ctx.settings.features.velocity_log2_divisor or 4.0
    value = clamp(math.log2(multiple) / divisor) if multiple > 1.0 else 0.0
    return _Candidate(mention, "engagement", value, eph=eph, baseline=base, multiple=multiple)


def popular_channels(ctx: ScoringContext) -> frozenset[str]:
    """Ranked "popular" listings whose rank works as a velocity signal.

    itch.io: the configured popular feed. Steam: every search with a ``popular*`` filter
    (Steam's own wishlist/follow-driven "popular upcoming" list), since follower counts
    are not available without a key (see docs/VERIFICATION.md).
    """
    channels = {f"itch:{ctx.sources.itch.popular_feed}"}
    for search in ctx.sources.steam.searches:
        if str(search.params.get("filter", "")).startswith("popular"):
            channels.add(f"steam:{search.name}")
    return frozenset(channels)


def _itch_velocity(mention: Mention, ctx: ScoringContext) -> _Candidate:
    """Rank-based velocity for a ranked listing (itch.io New & Popular, Steam popular upcoming)."""
    ranked = sorted((s for s in mention.history if s.rank is not None), key=lambda s: s.at)
    rank = mention.rank if mention.rank is not None else (ranked[-1].rank if ranked else None)
    if mention.channel not in popular_channels(ctx) or rank is None or rank < 1:
        return _Candidate(mention, "none")
    now = ctx.now
    if mention.observed_at and now - mention.observed_at > timedelta(hours=ITCH_STALE_HOURS):
        return _Candidate(mention, "none")  # it fell off the list
    list_size = max(mention.list_size or DEFAULT_ITCH_LIST_SIZE, rank, 1)
    rank_score = clamp(1.0 - (rank - 1) / list_size)
    start = min((s.at for s in ranked), default=mention.first_seen)
    hours_on_list = max((now - start).total_seconds() / 3600.0, 0.0) if start else 0.0
    full_hours = ctx.settings.features.itch_list_full_hours or 24.0
    persistence = clamp(hours_on_list / full_hours)
    value = clamp(ITCH_RANK_SHARE * rank_score + ITCH_PERSISTENCE_SHARE * persistence)
    return _Candidate(mention, "itch_rank", value, rank=rank, hours_on_list=hours_on_list)


def _steam_velocity(mention: Mention, ctx: ScoringContext) -> _Candidate:
    ranked = _itch_velocity(mention, ctx)
    if ranked.kind == "itch_rank":
        return ranked
    cutoff = ctx.now - timedelta(days=STEAM_FOLLOWER_WINDOW_DAYS)
    snaps = sorted(
        (s for s in mention.history if s.followers is not None and s.at >= cutoff), key=lambda s: s.at
    )
    if len(snaps) < 2 or snaps[-1].at <= snaps[0].at:
        return _Candidate(mention, "none")
    first, last = snaps[0], snaps[-1]
    growth = int(last.followers or 0) - int(first.followers or 0)
    span_days = max((last.at - first.at).total_seconds() / 3600.0, STEAM_FOLLOWER_MIN_SPAN_HOURS) / 24.0
    rate = growth / max(first.followers or 0, STEAM_FOLLOWER_MIN_BASE) / span_days
    value = clamp(rate / STEAM_FOLLOWER_FULL_RATE)
    return _Candidate(mention, "steam_followers", value, follower_growth=growth)


def _velocity_candidate(mention: Mention, ctx: ScoringContext) -> _Candidate:
    if mention.source == "itch":
        return _itch_velocity(mention, ctx)
    if mention.source == "steam":
        return _steam_velocity(mention, ctx)
    return _engagement_velocity(mention, ctx)


def _best_key(c: _Candidate) -> tuple[float, bool, int, datetime]:
    return (c.value, c.kind == "engagement", c.mention.engagement.total, c.mention.created_at)


# ----------------------------------------------------------------------------- features


def _underdog(mentions: list[Mention], ctx: ScoringContext) -> float:
    """``log10(1 + 1000 * engagement / max(audience, 100)) / 3`` for the most engaged mention
    whose audience (followers / subreddit members) is known; 0 when no audience is known."""
    known = [m for m in mentions if m.author_audience is not None]
    if not known:
        return 0.0
    best = max(known, key=lambda m: (m.engagement.total, m.created_at))
    audience = max(int(best.author_audience or 0), ctx.settings.features.underdog_min_audience, 1)
    return clamp(math.log10(1.0 + 1000.0 * best.engagement.total / audience) / 3.0)


def _cross(platforms: list[str], cross_map: dict[int, float]) -> float:
    n = len(platforms)
    if n <= 0 or not cross_map:
        return 0.0
    keys = sorted(cross_map)
    eligible = [k for k in keys if k <= n]
    if not eligible:
        return 0.0
    return clamp(float(cross_map[eligible[-1]]))


def _category_fit(steam: SteamInfo | None, sources: Sources) -> float:
    if steam is None:
        return 0.0
    coop = set(sources.steam.coop_category_ids)
    multi = set(sources.steam.multiplayer_category_ids)
    best = 0.0
    for cid in steam.category_ids:
        if cid in STEAM_CATEGORY_FIT and (cid in coop or not coop):
            best = max(best, STEAM_CATEGORY_FIT[cid])
        elif cid in coop:
            best = max(best, COOP_CATEGORY_FIT)
        elif cid in multi:
            best = max(best, MULTIPLAYER_CATEGORY_FIT)
    return best


def _fit_texts(game: Game, mentions: list[Mention], steam: SteamInfo | None) -> list[str]:
    texts = [game.title, *game.aliases, game.pitch or ""]
    for m in mentions:
        texts += [m.title, m.text, *m.raw_tags]
    if steam is not None:
        texts += [steam.short_description, *steam.tags]
    return texts


def _fit(
    game: Game, mentions: list[Mention], steam: SteamInfo | None, ctx: ScoringContext
) -> tuple[float, list[str], float | None]:
    hits = match_fit_keywords(_fit_texts(game, mentions, steam), ctx.sources.fit_keywords)
    heuristic = max(_category_fit(steam, ctx.sources), combine_strengths(w for _, w in hits))
    llm = clamp(game.llm.friendslop_fit) if game.llm is not None else None
    if llm is None:
        fit = heuristic
    else:
        fit = max(LLM_BLEND * heuristic + (1.0 - LLM_BLEND) * llm, LLM_ALONE * llm)
    return clamp(fit), [phrase for phrase, _ in hits], llm


def hype_from_signals(
    signals: CommentSignals | None, full_intent_commenters: int, confident_commenters: int = 1
) -> float:
    """``0.5 * min(1, 2 * intent_rate) * confidence + 0.5 * volume - negative_frac`` (0 without comments).

    ``confidence = min(1, distinct_commenters / confident_commenters)``: one "wishlisted!" out of
    two replies is not the same evidence as 10 out of 20, so the *rate* only counts fully once
    enough different people have commented.
    """
    if signals is None or signals.sampled <= 0:
        return 0.0
    intent = max(signals.intent_commenters, 0)
    distinct = max(signals.distinct_commenters, 1)
    rate = intent / distinct
    confidence = min(1.0, distinct / max(confident_commenters, 1))
    full = max(full_intent_commenters, 1)
    volume = clamp(math.log1p(intent) / math.log1p(full))
    return clamp(0.5 * min(1.0, rate * 2.0) * confidence + 0.5 * volume - signals.negative_frac)


def total_comments(signals: CommentSignals | None, mentions: list[Mention]) -> int:
    """Best guess of how many comments the discussion has (platform count or our sample)."""
    reported = max((m.engagement.comments for m in mentions), default=0)
    if signals is None:
        return max(reported, 0)
    return max(signals.post_comment_count, signals.sampled, reported, 0)


def meme_from_signals(signals: CommentSignals | None, comments: int, ctx: ScoringContext) -> float:
    """``clamp(roblox_commenters / 5)``, only when the discussion has at least 10 comments.

    Distinct commenters (not comments) are counted so one person spamming "roblox" can't fake it.
    """
    fs = ctx.settings.features
    if signals is None or comments < fs.meme_min_comments:
        return 0.0
    return clamp(signals.roblox_commenters / (fs.meme_divisor or 5.0))


def _fresh(game: Game, steam: SteamInfo | None, ctx: ScoringContext) -> float:
    fs = ctx.settings.features
    hours = max((ctx.now - game.first_seen).total_seconds() / 3600.0, 0.0)
    zero_hours = max(fs.fresh_zero_days * 24.0, fs.fresh_full_hours)
    if hours <= fs.fresh_full_hours:
        fresh = 1.0
    elif hours >= zero_hours:
        fresh = 0.0
    else:
        fresh = 1.0 - (hours - fs.fresh_full_hours) / (zero_hours - fs.fresh_full_hours)
    if steam is not None and (
        steam.coming_soon or (steam.release_date is not None and steam.release_date > ctx.now.date())
    ):
        fresh += fs.fresh_coming_soon_bonus
    return clamp(fresh)


def compute_features(
    game: Game,
    mentions: list[Mention],
    ctx: ScoringContext,
    signals: CommentSignals | None = None,
) -> tuple[Features, Evidence]:
    """All seven features for ``game`` plus the evidence (real numbers) behind them."""
    now = ctx.now
    fs = ctx.settings.features
    steam = steam_info_for(game, mentions)
    recent = [m for m in mentions if is_recent(m, now, fs.cross_window_hours)]
    recent_keys = {m.key for m in recent}

    # velocity: the hottest recent mention wins
    candidates = [_velocity_candidate(m, ctx) for m in mentions]
    for c in candidates:
        if c.mention.key not in recent_keys:
            c.value = 0.0
    velocity = max((c.value for c in candidates), default=0.0)
    best = max(candidates, key=_best_key, default=None)

    evidence = Evidence(signals=signals or CommentSignals(), first_seen=game.first_seen)
    if best is not None:
        m = best.mention
        evidence.best_mention_key = m.key
        evidence.best_source = m.source
        evidence.best_channel = m.channel or m.source
        evidence.best_likes = max(m.engagement.likes, 0)
        evidence.best_comments = max(m.engagement.comments, 0)
        evidence.best_shares = max(m.engagement.shares, 0)
        evidence.best_age_hours = round(m.age_hours(now), 2)
        evidence.audience = m.author_audience
        if best.kind == "engagement":
            evidence.eph = round(best.eph or 0.0, 3)
            evidence.baseline_eph = round(best.baseline or 0.0, 3)
            evidence.velocity_multiple = round(best.multiple or 0.0, 2)
    itch = [c for c in candidates if c.kind == "itch_rank"]
    if itch:
        top = max(itch, key=lambda c: (c.value, -(c.rank or 0)))
        evidence.best_rank = top.rank
        evidence.rank_channel = top.mention.channel
        evidence.hours_on_list = round(top.hours_on_list or 0.0, 1)
    followers = [c for c in candidates if c.kind == "steam_followers"]
    if followers:
        evidence.follower_growth = max(followers, key=lambda c: c.value).follower_growth

    # cross: distinct platforms talking about it in the window (and in the last 24h)
    evidence.sources_72h = ordered_platforms(m.source for m in recent)
    evidence.sources_24h = ordered_platforms(m.source for m in mentions if is_recent(m, now, 24))
    cross = _cross(evidence.sources_72h, fs.cross_map)

    fit, fit_hits, llm_fit = _fit(game, mentions, steam, ctx)
    evidence.fit_hits = fit_hits
    evidence.llm_fit = llm_fit

    comments = total_comments(signals, mentions)
    evidence.total_comments = comments
    if steam is not None:
        evidence.release_date_text = steam.release_date_text
        evidence.coming_soon = steam.coming_soon

    features = Features(
        velocity=clamp(velocity),
        underdog=_underdog(mentions, ctx),
        cross=cross,
        fit=fit,
        hype=hype_from_signals(signals, fs.hype_full_intent_commenters, fs.hype_confident_commenters),
        meme=meme_from_signals(signals, comments, ctx),
        fresh=_fresh(game, steam, ctx),
    )
    return features, evidence
