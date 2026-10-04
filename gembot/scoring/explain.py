"""Plain-English "why" lines for a scored game, built from its strongest features.

Each candidate line is ranked by how much its feature added to the score
(``weight * feature``); the top 2-4 are kept. Lines always quote real numbers, e.g.

* "312 upvotes and 45 comments in 3h on r/IndieDev (9× normal for that sub)"
* "Seen on Reddit, Bluesky and Steam in the last 24h"
* "17 different people said they wishlisted or want to play with friends"
* "🧱 Roblox-clone discourse: 11 commenters joking it's a Roblox game"  (always shown
  when the Roblox bonus applied)

Penalties are never reasons; they are logged and kept on the ``ScoreResult``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from gembot.models import FEATURE_NAMES, Game, ScoreResult, platform_label

MIN_STRENGTH = 0.01  # weight * feature below this is filler, used only to reach 2 lines
MIN_REASONS = 2
FRESH_LINE_MAX_DAYS = 7
UNDERDOG_LINE_MIN = 0.3  # underdog feature needed before we claim "big reaction for its size"
SMALL_CREATOR_MAX = 10_000  # followers; above this we don't call the account "small"
SMALL_SUB_MAX = 100_000  # subreddit members; above this the sub isn't "small"
_COOP_CATEGORY_RE = re.compile(r"co-?op|multi-?player|pvp", re.IGNORECASE)

LIKE_UNITS = {"reddit": "upvotes"}
COMMENT_UNITS = {"bluesky": "replies", "x": "replies"}
SHARE_UNITS = {"bluesky": "reposts", "x": "reposts"}


# ----------------------------------------------------------------------------- formatting


def natural_join(items: list[str]) -> str:
    """["A"] -> "A", ["A", "B"] -> "A and B", ["A", "B", "C"] -> "A, B and C"."""
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def fmt_count(n: float) -> str:
    """312 -> "312", 2100 -> "2,100", 48_250 -> "48.3k", 1_250_000 -> "1.2M"."""
    n = round(n)
    if abs(n) < 10_000:
        return f"{n:,}"
    if abs(n) < 1_000_000:
        return f"{n / 1000:.1f}".removesuffix(".0") + "k"
    return f"{n / 1_000_000:.1f}".removesuffix(".0") + "M"


def fmt_hours(hours: float) -> str:
    """0.4 -> "24m", 3.2 -> "3h", 50 -> "2 days"."""
    if hours < 1:
        return f"{max(round(hours * 60), 1)}m"
    if hours < 48:
        return f"{round(hours)}h"
    days = round(hours / 24)
    return f"{days} days"


def fmt_multiple(multiple: float) -> str:
    """9.04 -> "9", 2.46 -> "2.5", 16.7 -> "17"."""
    if multiple >= 10:
        return f"{multiple:.0f}"
    return f"{multiple:.1f}".removesuffix(".0")


def feed_label(channel: str | None) -> str:
    """ "itch:new-and-popular" -> "New & Popular"."""
    name = (channel or "").split(":", 1)[-1] or "new-and-popular"
    words = name.replace("-and-", " & ").replace("-", " ").split()
    return " ".join(w if w == "&" else w[:1].upper() + w[1:] for w in words)


# ----------------------------------------------------------------------------- candidates


@dataclass
class _Reason:
    text: str
    strength: float
    pinned: bool = False


def _engagement_phrase(source: str, likes: int, comments: int, shares: int) -> str:
    parts = []
    if likes > 0:
        parts.append(f"{fmt_count(likes)} {LIKE_UNITS.get(source, 'likes')}")
    if comments > 0:
        parts.append(f"{fmt_count(comments)} {COMMENT_UNITS.get(source, 'comments')}")
    if shares > 0 and source in SHARE_UNITS:
        parts.append(f"{fmt_count(shares)} {SHARE_UNITS[source]}")
    return natural_join(parts)


def _where(source: str, channel: str | None) -> str:
    if source == "reddit" and channel and channel.startswith("r/"):
        return channel
    return platform_label(source)


def _velocity_lines(result: ScoreResult, strength: float) -> list[_Reason]:
    ev = result.evidence
    lines: list[_Reason] = []
    source = ev.best_source or ""
    itch_line = None
    if ev.best_rank is not None:
        hours = ev.hours_on_list or 0.0
        tail = f"for {fmt_hours(hours)}" if hours >= 1 else "(just arrived)"
        itch_line = f"#{ev.best_rank} on itch.io {feed_label(ev.rank_channel)} {tail}"
    if source == "itch" and itch_line:
        return [_Reason(itch_line, strength)]
    if source == "steam" and ev.follower_growth:
        lines.append(
            _Reason(f"+{fmt_count(ev.follower_growth)} Steam followers in the last few days", strength)
        )
    elif source and source != "steam":
        engagement = _engagement_phrase(source, ev.best_likes, ev.best_comments, ev.best_shares)
        if engagement:
            where = _where(source, ev.best_channel)
            text = f"{engagement} in {fmt_hours(ev.best_age_hours or 0.0)} on {where}"
            multiple = ev.velocity_multiple or 0.0
            if multiple >= 1.5:
                normal = "that sub" if where.startswith("r/") else where
                text += f" ({fmt_multiple(multiple)}× normal for {normal})"
            lines.append(_Reason(text, strength))
    if itch_line:  # a secondary fact when something else drove velocity
        lines.append(_Reason(itch_line, min(strength, 0.03)))
    return lines


def _underdog_line(result: ScoreResult, strength: float) -> _Reason | None:
    ev = result.evidence
    if ev.audience is None or result.features.underdog < UNDERDOG_LINE_MIN or not ev.best_source:
        return None
    source = ev.best_source
    audience = fmt_count(ev.audience)
    if source == "reddit" and ev.best_channel and ev.best_channel.startswith("r/"):
        if ev.best_likes <= 0 or ev.audience > SMALL_SUB_MAX:
            return None
        text = f"Big reaction for a small sub: {fmt_count(ev.best_likes)} upvotes in {ev.best_channel} ({audience} members)"
        return _Reason(text, strength)
    if ev.best_likes > 0:
        reaction = f"{fmt_count(ev.best_likes)} {LIKE_UNITS.get(source, 'likes')}"
    else:
        total = ev.best_likes + ev.best_comments + ev.best_shares
        if total <= 0:
            return None
        reaction = f"{fmt_count(total)} reactions"
    if ev.audience <= SMALL_CREATOR_MAX:
        return _Reason(f"Small creator: {audience} followers, {reaction}", strength)
    return _Reason(f"Big reaction for the account's size: {reaction} with {audience} followers", strength)


def _cross_line(result: ScoreResult, strength: float) -> _Reason | None:
    ev = result.evidence
    if result.features.cross <= 0:
        return None
    if len(ev.sources_24h) >= 2:
        labels, when = ev.sources_24h, "in the last 24h"
    elif len(ev.sources_72h) >= 2:
        labels, when = ev.sources_72h, "in the last 3 days"
    else:
        return None
    return _Reason(f"Seen on {natural_join([platform_label(s) for s in labels])} {when}", strength)


def _hype_line(result: ScoreResult, strength: float) -> _Reason | None:
    n = result.evidence.signals.intent_commenters
    if n <= 0:
        return None
    if n == 1:
        return _Reason("1 person said they wishlisted it or want to play it with friends", strength)
    return _Reason(f"{n} different people said they wishlisted or want to play with friends", strength)


def _meme_line(result: ScoreResult, strength: float) -> _Reason | None:
    bonus = next((b for b in result.bonuses if b.code == "roblox_bonus"), None)
    jokers = result.evidence.signals.roblox_commenters
    if bonus is None and (result.features.meme <= 0 or jokers <= 0):
        return None
    people = "1 commenter" if jokers == 1 else f"{jokers} commenters"
    text = f"🧱 Roblox-clone discourse: {people} joking it's a Roblox game"
    return _Reason(text, strength + (bonus.points / 100.0 if bonus else 0.0), pinned=bonus is not None)


def _fit_line(result: ScoreResult, strength: float) -> _Reason | None:
    ev = result.evidence
    if ev.fit_hits:
        return _Reason(f"Friendslop fit: {', '.join(ev.fit_hits[:3])}", strength)
    if ev.llm_fit is not None and ev.llm_fit >= 0.5:
        return _Reason(f"AI check says it's a friends-co-op game ({ev.llm_fit:.1f} out of 1)", strength)
    return None


def _steam_line(
    game: Game, result: ScoreResult, fit_strength: float, fresh_strength: float, *, now: datetime
) -> _Reason | None:
    steam = game.steam
    if steam is None:
        return None
    matching = [c for c in steam.categories if _COOP_CATEGORY_RE.search(c)]
    # co-op categories first ("Online Co-op" before "Multi-player"), Steam's order otherwise
    categories = sorted(
        matching, key=lambda c: (not re.search(r"co-?op", c, re.IGNORECASE), "online" not in c.lower())
    )[:2]
    status = []
    if steam.release_date is not None and steam.release_date > now.date():
        d = steam.release_date
        status.append(f"out {d.day} {d:%b %Y}")
    elif steam.coming_soon:
        if (
            steam.release_date_text
            and steam.release_date is None
            and steam.release_date_text.lower() != "coming soon"
        ):
            status.append(f"coming {steam.release_date_text}")
        else:
            status.append("coming soon")
    if steam.early_access:
        status.append("Early Access")
    if not categories and not status:
        return None
    # mostly a supporting line: the keyword fit line and the freshness line carry the weight
    strength = 0.0
    if categories:
        strength += fit_strength * (0.25 if result.evidence.fit_hits else 1.0)
    if status:
        strength += 0.5 * fresh_strength
    return _Reason("Steam page: " + ", ".join(categories + status), strength)


def _fresh_line(result: ScoreResult, strength: float, *, now: datetime) -> _Reason | None:
    first_seen = result.evidence.first_seen
    if first_seen is None or result.features.fresh <= 0:
        return None
    age = now - first_seen
    if age > timedelta(days=FRESH_LINE_MAX_DAYS):
        return None
    when = fmt_hours(max(age.total_seconds() / 3600.0, 0.0))
    if "steam" in result.evidence.sources_72h:
        return _Reason(f"New Steam page, first seen {when} ago", strength)
    return _Reason(f"New find: first seen {when} ago", strength)


def _fallback(result: ScoreResult, game: Game) -> str:
    platforms = result.evidence.sources_72h or (
        [result.evidence.best_source] if result.evidence.best_source else []
    )
    if platforms:
        return f"New indie game spotted on {natural_join([platform_label(p) for p in platforms])}"
    return f"{game.title} turned up in GemBot's scan (Gem Score {result.score:.0f})"


# ----------------------------------------------------------------------------- public


def build_reasons(result: ScoreResult, game: Game, *, now: datetime, max_reasons: int = 4) -> list[str]:
    """2-4 plain-English reasons (never fewer than 1), strongest first."""
    weights = result.weights or {name: 1.0 / len(FEATURE_NAMES) for name in FEATURE_NAMES}
    values = result.features.as_dict()
    contrib = {name: weights.get(name, 0.0) * values[name] for name in FEATURE_NAMES}

    candidates: list[_Reason] = list(_velocity_lines(result, contrib["velocity"]))
    for maybe in (
        _meme_line(result, contrib["meme"]),
        _hype_line(result, contrib["hype"]),
        _cross_line(result, contrib["cross"]),
        _underdog_line(result, contrib["underdog"]),
        _fit_line(result, contrib["fit"]),
        _steam_line(game, result, contrib["fit"], contrib["fresh"], now=now),
        _fresh_line(result, contrib["fresh"], now=now),
    ):
        if maybe is not None:
            candidates.append(maybe)

    unique: list[_Reason] = []
    for c in candidates:
        if c.text not in {u.text for u in unique}:
            unique.append(c)
    limit = max(max_reasons, 1)
    ranked = sorted(unique, key=lambda r: -r.strength)
    pinned = [r for r in ranked if r.pinned]
    chosen = pinned + [r for r in ranked if not r.pinned and r.strength >= MIN_STRENGTH]
    if len(chosen) < MIN_REASONS:
        chosen += [r for r in ranked if r not in chosen][: MIN_REASONS - len(chosen)]
    chosen = sorted(chosen[:limit], key=lambda r: -r.strength)
    lines = [r.text for r in chosen]
    return lines or [_fallback(result, game)]
