"""The Gem Score: weighted features, exclusions, penalties and the Roblox bonus.

::

    base  = 100 * sum(w_i * f_i) / sum(w_i)
    score = clamp(base + bonuses - penalties, 0, 100)      (rounded to 0.1)

Big studios (``blocklist.yaml`` companies) and banned keywords exclude a game entirely
(score 0, ``excluded=True``). See ``docs/SCORING.md`` for the plain-English version.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from datetime import timedelta

from gembot.config import Blocklist
from gembot.models import (
    FEATURE_NAMES,
    Adjustment,
    CommentSignals,
    Features,
    Game,
    Mention,
    ScoreResult,
    SteamInfo,
)
from gembot.scoring.explain import build_reasons
from gembot.scoring.features import (
    LISTING_SOURCES,
    ScoringContext,
    clamp,
    compute_features,
    normalize_text,
    phrase_pattern,
    steam_info_for,
)

log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


# ----------------------------------------------------------------------------- blocklist


def company_tokens(name: str) -> list[str]:
    """Lowercase alphanumeric tokens with accents stripped: "Ubisoft Montréal" -> [ubisoft, montreal]."""
    decomposed = unicodedata.normalize("NFKD", name)
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _TOKEN_RE.findall(plain.lower())


def company_matches(name: str, company: str) -> bool:
    """Whole-token prefix match, ignoring case and punctuation.

    "Ubisoft Montreal" matches "Ubisoft", "Electronic Arts Inc." matches "Electronic Arts",
    but "Blizzardo Games" does not match "Blizzard Entertainment" and "2Kool" not "2K".
    """
    wanted = company_tokens(company)
    have = company_tokens(name)
    return bool(wanted) and have[: len(wanted)] == wanted


def _company_names(game: Game, steam: SteamInfo | None) -> list[tuple[str, str]]:
    names: list[tuple[str, str]] = []
    if game.developer:
        names.append(("developer", game.developer))
    if game.publisher:
        names.append(("publisher", game.publisher))
    if steam is not None:
        names += [("developer", d) for d in steam.developers if d]
        names += [("publisher", p) for p in steam.publishers if p]
    return names


def _keyword_texts(game: Game, mentions: Iterable[Mention]) -> list[str]:
    texts = [game.title, *game.aliases]
    for m in mentions:
        texts += [m.title, m.text]
    return [t for t in texts if t]


def blocklist_reason(game: Game, mentions: list[Mention], blocklist: Blocklist) -> str | None:
    """Why this game must never be posted (big studio / banned keyword), or None."""
    steam = steam_info_for(game, mentions)
    for role, name in _company_names(game, steam):
        for company in blocklist.companies:
            if company_matches(name, company):
                return f"big studio: {company} ({role} {name!r})"
    if blocklist.keywords:
        blob = "\n".join(normalize_text(t) for t in _keyword_texts(game, mentions))
        for keyword in blocklist.keywords:
            if keyword.strip() and phrase_pattern(keyword).search(blob):
                return f"banned keyword: {keyword!r}"
    return None


# ----------------------------------------------------------------------------- penalties


def _negativity_penalty(signals: CommentSignals | None, ctx: ScoringContext) -> Adjustment | None:
    ps = ctx.settings.penalties
    if signals is None or signals.distinct_commenters < ps.negativity_min_commenters:
        return None
    if signals.negative_frac <= ps.negativity_threshold:
        return None
    terms = ", ".join(signals.negative_terms[:3])
    detail = f"{signals.negative_commenters} of {signals.distinct_commenters} commenters negative"
    return Adjustment(
        code="negativity", points=ps.negativity_points, detail=f"{detail} ({terms})" if terms else detail
    )


def _old_release_penalty(steam: SteamInfo | None, ctx: ScoringContext) -> Adjustment | None:
    """Released more than ``old_release_days`` ago (exact Steam date only).

    Steam reports the Early Access launch date as the release date, so a *new* Early Access
    launch is inside the window and never penalised; an Early Access game that launched
    months ago is penalised like any other old release.
    """
    if steam is None or steam.release_date is None or steam.coming_soon:
        return None
    ps = ctx.settings.penalties
    age_days = (ctx.now.date() - steam.release_date).days
    if age_days <= ps.old_release_days:
        return None
    kind = "Early Access launch" if steam.early_access else "released"
    return Adjustment(
        code="old_release",
        points=ps.old_release_points,
        detail=f"{kind} {steam.release_date.isoformat()} ({age_days} days ago)",
    )


def _spammer_penalty(mentions: list[Mention], ctx: ScoringContext) -> Adjustment | None:
    """Same author posting this game more than ``spammer_posts`` times in the window.

    Store listings (Steam / itch) are not posts by a person and are ignored.
    """
    ps = ctx.settings.penalties
    cutoff = ctx.now - timedelta(days=ps.spammer_window_days)
    posts: dict[tuple[str, str], set[str]] = defaultdict(set)
    for m in mentions:
        if m.author and m.source not in LISTING_SOURCES and m.created_at >= cutoff:
            posts[(m.source, m.author.strip().lower())].add(m.source_id)
    worst = max(posts.items(), key=lambda kv: len(kv[1]), default=None)
    if worst is None or len(worst[1]) <= ps.spammer_posts:
        return None
    (source, author), ids = worst
    return Adjustment(
        code="spammer",
        points=ps.spammer_points,
        detail=f"{author} posted it {len(ids)} times on {source} in {ps.spammer_window_days} days",
    )


def _roblox_bonus(
    features: Features, comments: int, signals: CommentSignals | None, ctx: ScoringContext
) -> Adjustment | None:
    bs = ctx.settings.bonuses
    if features.meme < bs.roblox_meme_min:
        return None
    if features.velocity < bs.roblox_velocity_min and comments < bs.roblox_comments_min:
        return None
    jokers = signals.roblox_commenters if signals is not None else 0
    return Adjustment(
        code="roblox_bonus", points=bs.roblox_points, detail=f"{jokers} commenters joking it's a Roblox game"
    )


# ----------------------------------------------------------------------------- score


def clean_weights(weights: dict[str, float]) -> dict[str, float]:
    """Feature weights summing to 1 (unknown names dropped, missing/negative -> 0)."""
    raw = {name: max(float(weights.get(name, 0.0) or 0.0), 0.0) for name in FEATURE_NAMES}
    raw = {k: (v if v == v else 0.0) for k, v in raw.items()}  # NaN -> 0
    total = sum(raw.values())
    if total <= 0:
        return {name: 1.0 / len(FEATURE_NAMES) for name in FEATURE_NAMES}
    return {name: value / total for name, value in raw.items()}


def alarm_signals_for(features: Features, thresholds: dict[str, float]) -> list[str]:
    values = features.as_dict()
    return [name for name in FEATURE_NAMES if name in thresholds and values[name] >= thresholds[name]]


def _score(
    game: Game,
    mentions: list[Mention],
    ctx: ScoringContext,
    weights: dict[str, float],
    blocklist: Blocklist,
    signals: CommentSignals | None,
) -> ScoreResult:
    w = clean_weights(weights)
    features, evidence = compute_features(game, mentions, ctx, signals)
    values = features.as_dict()
    base = 100.0 * sum(w[name] * values[name] for name in FEATURE_NAMES)
    result = ScoreResult(
        game_id=game.game_id, base=round(base, 2), features=features, evidence=evidence, weights=w
    )

    reason = blocklist_reason(game, mentions, blocklist)
    if reason is not None:
        result.excluded = True
        result.exclude_reason = reason
        result.score = 0.0
        return result

    steam = steam_info_for(game, mentions)
    penalties = [
        p
        for p in (
            _negativity_penalty(signals, ctx),
            _old_release_penalty(steam, ctx),
            _spammer_penalty(mentions, ctx),
        )
        if p is not None
    ]
    bonus = _roblox_bonus(features, evidence.total_comments, signals, ctx)
    result.penalties = penalties
    result.bonuses = [bonus] if bonus is not None else []
    total = base + sum(b.points for b in result.bonuses) - sum(p.points for p in penalties)
    result.score = round(clamp(total, 0.0, 100.0), 1)
    result.alarm_signals = alarm_signals_for(features, ctx.settings.decisions.alarm_signal_thresholds)
    return result


def score_game(
    game: Game,
    mentions: list[Mention],
    ctx: ScoringContext,
    weights: dict[str, float],
    blocklist: Blocklist,
    signals: CommentSignals | None = None,
) -> ScoreResult:
    """Full Gem Score for one game, with the plain-English reasons filled in."""
    result = _score(game, mentions, ctx, weights, blocklist, signals)
    if result.excluded:
        log.info("excluded %s: %s", game.game_id, result.exclude_reason)
    else:
        result.reasons = build_reasons(result, game, now=ctx.now)
        if result.penalties:
            log.info(
                "%s penalties: %s",
                game.game_id,
                "; ".join(f"-{p.points:g} {p.code} ({p.detail})" for p in result.penalties),
            )
    return result


def prefilter(
    games: dict[str, Game],
    mentions: dict[str, Mention],
    candidate_ids: Iterable[str],
    ctx: ScoringContext,
    weights: dict[str, float],
    blocklist: Blocklist,
    limit: int,
) -> list[str]:
    """Stage A: cheap score (no comment signals) -> the ``limit`` most promising game ids.

    Excluded games (blocklist, or ``Game.excluded_reason`` already set) are dropped. Ties
    are broken by the newest ``first_seen``, then by id so the result is deterministic.
    """
    scored: list[tuple[float, float, str]] = []
    seen: set[str] = set()
    for game_id in candidate_ids:
        game = games.get(game_id)
        if game is None or game_id in seen or game.excluded_reason:
            continue
        seen.add(game_id)
        game_mentions = [mentions[k] for k in game.mention_keys if k in mentions]
        result = _score(game, game_mentions, ctx, weights, blocklist, None)
        if result.excluded:
            continue
        scored.append((result.score, game.first_seen.timestamp(), game_id))
    scored.sort(key=lambda t: (-t[0], -t[1], t[2]))
    return [game_id for _, _, game_id in scored[: max(limit, 0)]]
