"""Plain-English "why" lines for a scored game, built from its strongest features.

Each candidate line is ranked by how much its feature added to the score
(``weight * feature``); the top 2-4 are kept. Lines quote real numbers, e.g.

* "312 upvotes and 45 comments in 3h on r/IndieDev (9× normal for that sub)"
* "Seen on Reddit, Bluesky and Steam in the last 24h"
* "17 different people said they wishlisted or want to play with friends"
* "🧱 Roblox-clone discourse: 11 commenters joking it's a Roblox game"  (always shown
  when the Roblox bonus applied)

When the features give fewer than 2 lines (BUILD_SPEC 4.6: "2-4 reasons"), filler lines
pad the list: the strongest feature's generic line, Steam page facts, when GemBot first
spotted the game and where it was seen. Only a game with nothing to say gets one line.

Penalties are never reasons; they are logged and kept on the ``ScoreResult``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from gembot.models import FEATURE_NAMES, Game, ScoreResult, platform_label

MIN_STRENGTH = 0.01  # weight * feature below this is filler, used only to reach 2 lines
MIN_REASONS = 2
GENERIC_LINE_MIN = 0.5  # a feature this strong without a line of its own gets a generic filler line
FRESH_LINE_MAX_DAYS = 7
UNDERDOG_LINE_MIN = 0.3  # underdog feature needed before we claim "big reaction for its size"
SMALL_CREATOR_MAX = 10_000  # followers; above this we don't call the account "small"
SMALL_SUB_MAX = 100_000  # subreddit members; above this the sub isn't "small"
_COOP_CATEGORY_RE = re.compile(r"co-?op|multi-?player|pvp", re.IGNORECASE)

LIKE_UNITS = {"reddit": "upvotes"}
COMMENT_UNITS = {"bluesky": "replies", "x": "replies"}
SHARE_UNITS = {"bluesky": "reposts", "x": "reposts"}

#: Filler line for a strong feature whose own line had nothing to quote (freshness has its
#: own filler: "First spotted N ago").
GENERIC_LINES: dict[str, str] = {
    "velocity": "Getting attention faster than usual",
    "underdog": "Big reaction for the size of its audience",
    "cross": "Talked about on more than one platform",
    "fit": "Looks like a game to play with friends",
    "hype": "Commenters say they want to play it",
    "meme": "Commenters are joking it's a Roblox game",
}


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
    topic: str = ""  # the feature (or "steam" / "platform") the line is about


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
        if (ev.rank_channel or "").startswith("steam:"):
            itch_line = f"#{ev.best_rank} on Steam's popular upcoming list {tail}"
        else:
            itch_line = f"#{ev.best_rank} on itch.io {feed_label(ev.rank_channel)} {tail}"
    if source in ("itch", "steam") and itch_line and not (source == "steam" and ev.follower_growth):
        return [_Reason(itch_line, strength, topic="velocity")]
    if source == "steam" and ev.follower_growth:
        text = f"+{fmt_count(ev.follower_growth)} Steam followers in the last few days"
        lines.append(_Reason(text, strength, topic="velocity"))
    elif source and source != "steam":
        engagement = _engagement_phrase(source, ev.best_likes, ev.best_comments, ev.best_shares)
        if engagement:
            where = _where(source, ev.best_channel)
            text = f"{engagement} in {fmt_hours(ev.best_age_hours or 0.0)} on {where}"
            multiple = ev.velocity_multiple or 0.0
            if multiple >= 1.5:
                normal = "that sub" if where.startswith("r/") else where
                text += f" ({fmt_multiple(multiple)}× normal for {normal})"
            lines.append(_Reason(text, strength, topic="velocity"))
    if itch_line:  # a secondary fact when something else drove velocity
        lines.append(_Reason(itch_line, min(strength, 0.03), topic="velocity"))
    return lines


def _underdog_line(result: ScoreResult, strength: float) -> _Reason | None:
    """Quotes the post that produced the underdog value (``evidence.underdog_*``)."""
    ev = result.evidence
    source, audience = ev.underdog_source, ev.underdog_audience
    if audience is None or result.features.underdog < UNDERDOG_LINE_MIN or not source:
        return None
    members = fmt_count(audience)
    channel = ev.underdog_channel
    if source == "reddit" and channel and channel.startswith("r/"):
        if ev.underdog_likes <= 0 or audience > SMALL_SUB_MAX:
            return None
        text = (
            f"Big reaction for a small sub: {fmt_count(ev.underdog_likes)} upvotes in {channel} "
            f"({members} members)"
        )
        return _Reason(text, strength, topic="underdog")
    if ev.underdog_likes > 0:
        reaction = f"{fmt_count(ev.underdog_likes)} {LIKE_UNITS.get(source, 'likes')}"
    else:
        total = ev.underdog_comments + ev.underdog_shares
        if total <= 0:
            return None
        reaction = f"{fmt_count(total)} reactions"
    if audience <= SMALL_CREATOR_MAX:
        return _Reason(f"Small creator: {members} followers, {reaction}", strength, topic="underdog")
    text = f"Big reaction for the account's size: {reaction} with {members} followers"
    return _Reason(text, strength, topic="underdog")


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
    text = f"Seen on {natural_join([platform_label(s) for s in labels])} {when}"
    return _Reason(text, strength, topic="cross")


def _hype_line(result: ScoreResult, strength: float) -> _Reason | None:
    n = result.evidence.signals.intent_commenters
    if n <= 0:
        return None
    if n == 1:
        text = "1 person said they wishlisted it or want to play it with friends"
    else:
        text = f"{n} different people said they wishlisted or want to play with friends"
    return _Reason(text, strength, topic="hype")


def _meme_line(result: ScoreResult, strength: float) -> _Reason | None:
    bonus = next((b for b in result.bonuses if b.code == "roblox_bonus"), None)
    jokers = result.evidence.meme_jokers  # only the jokers that counted (posts with 10+ comments)
    if bonus is None and (result.features.meme <= 0 or jokers <= 0):
        return None
    people = "1 commenter" if jokers == 1 else f"{jokers} commenters"
    text = f"🧱 Roblox-clone discourse: {people} joking it's a Roblox game"
    boost = bonus.points / 100.0 if bonus else 0.0
    return _Reason(text, strength + boost, pinned=bonus is not None, topic="meme")


def _fit_line(result: ScoreResult, strength: float) -> _Reason | None:
    ev = result.evidence
    if ev.fit_hits:
        return _Reason(f"Friendslop fit: {', '.join(ev.fit_hits[:3])}", strength, topic="fit")
    if ev.llm_fit is not None and ev.llm_fit >= 0.5:
        text = f"AI check says it's a friends-co-op game ({ev.llm_fit:.1f} out of 1)"
        return _Reason(text, strength, topic="fit")
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
    return _Reason("Steam page: " + ", ".join(categories + status), strength, topic="steam")


def _fresh_line(result: ScoreResult, strength: float, *, now: datetime) -> _Reason | None:
    first_seen = result.evidence.first_seen
    if first_seen is None or result.features.fresh <= 0:
        return None
    age = now - first_seen
    if age > timedelta(days=FRESH_LINE_MAX_DAYS):
        return None
    when = fmt_hours(max(age.total_seconds() / 3600.0, 0.0))
    if "steam" in result.evidence.sources_72h:
        return _Reason(f"New Steam page, first seen {when} ago", strength, topic="fresh")
    return _Reason(f"New find: first seen {when} ago", strength, topic="fresh")


# ----------------------------------------------------------------------------- fillers


def _generic_line(result: ScoreResult, contrib: dict[str, float], covered: set[str]) -> _Reason | None:
    """The strongest feature that has no line of its own, in general words."""
    values = result.features.as_dict()
    strong = [n for n in GENERIC_LINES if n not in covered and values[n] >= GENERIC_LINE_MIN]
    if not strong:
        return None
    name = max(strong, key=lambda n: contrib[n])
    return _Reason(GENERIC_LINES[name], 0.0, topic=name)


def _steam_facts_line(game: Game) -> _Reason | None:
    """Genres and price from the Steam page, when the co-op / release line had nothing."""
    steam = game.steam
    if steam is None:
        return None
    facts = [g for g in steam.genres if g][:2]
    if steam.is_free:
        facts.append("free to play")
    elif steam.price:
        facts.append(steam.price)
    if not facts:
        return None
    return _Reason("Steam page: " + ", ".join(facts), 0.0, topic="steam")


def _first_spotted_line(result: ScoreResult, *, now: datetime) -> _Reason | None:
    first_seen = result.evidence.first_seen
    if first_seen is None:
        return None
    when = fmt_hours(max((now - first_seen).total_seconds() / 3600.0, 0.0))
    return _Reason(f"First spotted {when} ago", 0.0, topic="fresh")


def _platform_line(result: ScoreResult) -> _Reason | None:
    ev = result.evidence
    platforms = ev.sources_72h or ([ev.best_source] if ev.best_source else [])
    if not platforms:
        return None
    labels = [_where(p, ev.best_channel) if p == ev.best_source else platform_label(p) for p in platforms]
    return _Reason(f"Spotted on {natural_join(labels)}", 0.0, topic="platform")


def _fillers(
    result: ScoreResult, game: Game, contrib: dict[str, float], covered: set[str], *, now: datetime
) -> list[_Reason]:
    """Lines that only pad a game to ``MIN_REASONS``, most telling first."""
    lines = [_generic_line(result, contrib, covered)]
    if "steam" not in covered:
        lines.append(_steam_facts_line(game))
    if "fresh" not in covered:
        lines.append(_first_spotted_line(result, now=now))
    if "cross" not in covered:
        lines.append(_platform_line(result))
    return [line for line in lines if line is not None]


def _fallback(result: ScoreResult, game: Game) -> str:
    return f"{game.title} turned up in GemBot's scan (Gem Score {result.score:.0f})"


# ----------------------------------------------------------------------------- public


def build_reasons(result: ScoreResult, game: Game, *, now: datetime, max_reasons: int = 4) -> list[str]:
    """2-4 plain-English reasons, strongest first.

    Lines whose feature added at least ``MIN_STRENGTH`` come first (the 🧱 line is pinned when
    the Roblox bonus applied). Fewer than 2 are padded with weaker feature lines, then with
    filler facts (see ``_fillers``). Only a game with nothing at all to say gets one line.
    """
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
    if len(chosen) < MIN_REASONS:
        texts = {r.text for r in chosen}
        covered = {r.topic for r in unique}
        fillers = [f for f in _fillers(result, game, contrib, covered, now=now) if f.text not in texts]
        chosen += fillers[: MIN_REASONS - len(chosen)]
    chosen = sorted(chosen[:limit], key=lambda r: -r.strength)
    lines = [r.text for r in chosen]
    return lines or [_fallback(result, game)]
