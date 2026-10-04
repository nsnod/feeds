"""Discord message builders (alarm, roundup, status, welcome) and Discord's size limits.

Every builder returns a plain ``dict`` ready to be sent as the JSON body of
``POST /channels/{id}/messages``. :func:`enforce_limits` guarantees the payload is
valid for Discord no matter how long the game titles, reasons or links are; every
builder runs it, and the REST client runs it again right before sending.

Lengths are measured in UTF-16 code units, which is never less than the number of
code points, so the payload stays valid whichever way Discord counts "characters".
"""

from __future__ import annotations

import copy
import math
import re
from datetime import datetime
from typing import Any

from gembot.models import GemCard, ensure_utc

# --- Discord limits (https://discord.com/developers/docs/resources/message#embed-object-embed-limits)
TITLE_LIMIT = 256
DESCRIPTION_LIMIT = 4096
FIELDS_LIMIT = 25
FIELD_NAME_LIMIT = 256
FIELD_VALUE_LIMIT = 1024
FOOTER_TEXT_LIMIT = 2048
AUTHOR_NAME_LIMIT = 256
EMBED_TOTAL_LIMIT = 6000  # title + description + field names/values + footer + author, across ALL embeds
EMBEDS_PER_MESSAGE = 10
CONTENT_LIMIT = 2000

# --- colours (embed side bar)
ALARM_COLOR = 0xFF4D4D
ROUNDUP_COLOR = 0x5865F2
STATUS_COLOR = 0x99AAB5
WELCOME_COLOR = 0x57F287

FILLED = "▰"
EMPTY = "▱"
ELLIPSIS = "…"
ZWSP = chr(0x200B)  # zero-width space: Discord rejects empty field names/values, this renders as nothing

DEFAULT_CHANNEL_NAMES = {"alarm": "gem-alarm", "roundup": "gem-roundup", "status": "gembot-status"}


# --------------------------------------------------------------------------------------
# text helpers
# --------------------------------------------------------------------------------------


def text_units(text: str) -> int:
    """Length in UTF-16 code units (an emoji like 🚨 counts as 2)."""
    return len(text.encode("utf-16-le")) // 2


def _prefix_within(text: str, limit: int) -> str:
    """Longest prefix of ``text`` whose UTF-16 length is <= ``limit`` (never splits a character)."""
    used = 0
    for index, char in enumerate(text):
        used += 2 if ord(char) > 0xFFFF else 1
        if used > limit:
            return text[:index]
    return text


def truncate(text: str, limit: int) -> str:
    """Shorten ``text`` to at most ``limit`` UTF-16 units, cutting at a word boundary and adding "…"."""
    if text_units(text) <= limit:
        return text
    if limit <= 0:
        return ""
    if limit == 1:
        return ELLIPSIS
    cut = _prefix_within(text, limit - 1)
    # Prefer a clean word boundary when one is reasonably close to the end.
    space = max(cut.rfind(" "), cut.rfind("\n"))
    if space >= max(len(cut) * 0.6, 1):
        cut = cut[:space]
    cut = cut.rstrip(" \n\t,;:-–—·•")
    return cut + ELLIPSIS


def shorten(text: str, limit: int) -> str:
    """Collapse whitespace, then :func:`truncate`."""
    return truncate(re.sub(r"\s+", " ", text).strip(), limit)


def _clamped(score: float) -> float:
    try:
        value = float(score)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(value):
        return 0.0
    return min(max(value, 0.0), 100.0)


def score_int(score: float) -> int:
    """The score as shown to people: clamped to 0..100 and rounded half up (NaN -> 0)."""
    return math.floor(_clamped(score) + 0.5)


def score_bar(score: float) -> str:
    """``score_bar(74) == "▰▰▰▰▰▰▰▱▱▱ 74"``: round(score/10) filled blocks (half up), then the score."""
    filled = math.floor(_clamped(score) / 10.0 + 0.5)
    return FILLED * filled + EMPTY * (10 - filled) + f" {score_int(score)}"


def format_utc(moment: datetime) -> str:
    """``Sat 3 Oct, 14:00 UTC``."""
    moment = ensure_utc(moment)
    return f"{moment:%a} {moment.day} {moment:%b}, {moment:%H:%M} UTC"


def relative_time(moment: datetime, now: datetime) -> str:
    """``just now`` / ``12 min ago`` / ``5h ago`` / ``3 days ago``."""
    seconds = (ensure_utc(now) - ensure_utc(moment)).total_seconds()
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min ago"
    hours = int(seconds // 3600)
    if hours < 48:
        return f"{hours}h ago"
    days = int(seconds // 86400)
    return f"{days} days ago"


_MD_SPECIAL = re.compile(r"([\\*_~`|>\[\]<])")


def escape_markdown(text: str) -> str:
    """Show text from the internet literally: no bold/strike/spoilers, no masked links, no
    ``<@…>``/``<#…>`` mentions. (Pings are already impossible: ``allowed_mentions`` is locked.)"""
    return _MD_SPECIAL.sub(r"\\\1", text)


def _md_label(label: str) -> str:
    return label.replace("[", "(").replace("]", ")").strip() or "link"


def _md_url(url: str) -> str:
    return url.strip().replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def markdown_link(label: str, url: str) -> str:
    return f"[{_md_label(label)}]({_md_url(url)})"


def links_line(links: list[tuple[str, str]], *, sep: str = " · ", limit: int = FIELD_VALUE_LIMIT) -> str:
    """Join ``(label, url)`` pairs as markdown links, keeping only the links that fit in ``limit``."""
    out = ""
    for label, url in links:
        if not url:
            continue
        piece = markdown_link(label, url)
        candidate = piece if not out else out + sep + piece
        if text_units(candidate) > limit:
            break
        out = candidate
    return out


def _title_flags(card: GemCard) -> str:
    flags = ""
    if card.test:
        flags += "🧪 TEST — "
    if card.escalated:
        flags += "📈 Escalated · "
    return flags


# --------------------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------------------


def alarm_payload(card: GemCard, *, ping_role_id: str | None = None, now: datetime | None = None) -> dict:
    """The 🚨 GEM ALARM message for one game.

    ``now`` (optional) adds a relative "first seen 3h ago" to the footer; without it the
    footer only carries the absolute UTC time.
    """
    title = f"{_title_flags(card)}🚨 GEM ALARM: {card.title}"
    description = f"**Gem Score** {score_bar(card.score)}"
    reasons = [escape_markdown(r.strip()) for r in card.reasons if r and r.strip()]
    if reasons:
        description += "\n\n" + "\n".join(f"• {r}" for r in reasons)
    if card.pitch and card.pitch.strip():
        description += "\n\n> " + escape_markdown(shorten(card.pitch, 300))

    embed: dict[str, Any] = {"title": title, "description": description, "color": ALARM_COLOR}
    if card.url:
        embed["url"] = card.url
    where = links_line(card.links)
    if where:
        embed["fields"] = [{"name": "Where people are talking", "value": where, "inline": False}]
    if card.thumb:
        embed["thumbnail"] = {"url": card.thumb}
    if card.first_seen is not None:
        first_seen = ensure_utc(card.first_seen)
        when = format_utc(first_seen)
        if now is not None:
            when = f"{relative_time(first_seen, now)} · {when}"
        embed["footer"] = {"text": f"First seen {when}"}
        embed["timestamp"] = first_seen.isoformat()

    content = (
        f"{_title_flags(card)}🚨 **GEM ALARM:** {escape_markdown(card.title)} · "
        f"Gem Score {score_int(card.score)}"
    )
    payload: dict[str, Any] = {"embeds": [embed]}
    if ping_role_id:
        payload["content"] = f"<@&{ping_role_id}> {content}"
        payload["allowed_mentions"] = {"parse": [], "roles": [str(ping_role_id)]}
    else:
        payload["content"] = content
    return enforce_limits(payload)


def roundup_header_payload(now: datetime, count: int, *, up: str = "👍", down: str = "👎") -> dict:
    games = "1 game" if count == 1 else f"{count} games"
    content = f"📋 **Gem Roundup — {format_utc(now)}**\n{games} worth a look. React {up}/{down} on each one."
    return enforce_limits({"content": content})


def roundup_entry_payload(card: GemCard) -> dict:
    """One compact embed per roundup game (each gets its own message so 👍/👎 label one game)."""
    flags = ""
    if card.test:
        flags += "🧪 TEST — "
    if card.would_have_alarmed:
        flags += "⏰ Would have alarmed · "
    if card.heating_up:
        flags += "📈 Heating up · "
    lines = [f"Gem Score {score_bar(card.score)}"]
    top = next((r.strip() for r in card.reasons if r and r.strip()), None)
    if top:
        lines.append(escape_markdown(top))
    inline_links = links_line(card.links, limit=1000)
    if inline_links:
        lines.append(inline_links)
    embed: dict[str, Any] = {
        "title": f"{flags}{card.title}",
        "description": "\n".join(lines),
        "color": ROUNDUP_COLOR,
    }
    if card.url:
        embed["url"] = card.url
    if card.thumb:
        embed["thumbnail"] = {"url": card.thumb}
    return enforce_limits({"embeds": [embed]})


def status_payload(lines: list[str]) -> dict:
    """A short note for #gembot-status (source broke / recovered, weekly learning note)."""
    body = "\n".join(line.rstrip() for line in lines if line and line.strip()) or "All good."
    embed = {"title": "🩺 GemBot status", "description": body, "color": STATUS_COLOR}
    return enforce_limits({"embeds": [embed]})


def text_payload(text: str) -> dict:
    return enforce_limits({"content": text})


def _channel_ref(role: str, channel_ids: dict[str, str] | None) -> str:
    if channel_ids and channel_ids.get(role):
        return f"<#{channel_ids[role]}>"
    return f"#{DEFAULT_CHANNEL_NAMES[role]}"


def welcome_payload(channel_ids: dict[str, str] | None = None, *, up: str = "👍", down: str = "👎") -> dict:
    """The one-time hello that explains GemBot, its three channels and the 👍/👎 reactions."""
    alarm = _channel_ref("alarm", channel_ids)
    roundup = _channel_ref("roundup", channel_ids)
    status = _channel_ref("status", channel_ids)
    description = (
        "I look for **small indie games before they blow up**, especially chaotic co-op games "
        "you play with friends (think Lethal Company, R.E.P.O. or PEAK). Every 30 minutes I check "
        "Steam, Reddit, itch.io, Bluesky and any feeds you added, and give each game a "
        "**Gem Score** from 0 to 100 with the reasons behind it."
    )
    fields = [
        {
            "name": "🚨 Gem alarms",
            "value": (
                f"{alarm}: only games that are taking off right now. Rare on purpose. "
                "Set this channel to **All Messages** notifications so your phone pings."
            ),
            "inline": False,
        },
        {
            "name": "📋 Roundups",
            "value": (
                f"{roundup}: every couple of hours, a short list of games worth a look. "
                "If nothing is good enough, I stay quiet."
            ),
            "inline": False,
        },
        {
            "name": "🩺 Status",
            "value": (
                f"{status}: my notes. I only speak up when a source breaks or comes back, "
                "plus a weekly note on what I learned from you."
            ),
            "inline": False,
        },
        {
            "name": f"{up} / {down} teach me",
            "value": (
                f"Every alarm and roundup game gets {up} and {down}. Tap {up} if it's your kind of game "
                f"and {down} if it isn't. I read your reactions each run and slowly shift what I pay "
                "attention to (comment hype, buzz on several sites, co-op fit and so on). "
                "Only people count, never bots."
            ),
            "inline": False,
        },
    ]
    embed = {
        "title": "👋 Hi, I'm GemBot!",
        "description": description,
        "color": WELCOME_COLOR,
        "fields": fields,
        "footer": {"text": "I also posted a TEST alarm so you can see what a real one looks like."},
    }
    return enforce_limits({"embeds": [embed]})


# --------------------------------------------------------------------------------------
# limits
# --------------------------------------------------------------------------------------


def _embed_parts(embed: dict) -> list[tuple[dict, str, bool]]:
    """``(container, key, shrinkable)`` for every counted text slot in one embed."""
    parts: list[tuple[dict, str, bool]] = []
    if isinstance(embed.get("title"), str):
        parts.append((embed, "title", False))
    if isinstance(embed.get("description"), str):
        parts.append((embed, "description", True))
    for field in embed.get("fields") or []:
        parts.append((field, "name", False))
        parts.append((field, "value", True))
    if isinstance(embed.get("footer"), dict) and isinstance(embed["footer"].get("text"), str):
        parts.append((embed["footer"], "text", False))
    if isinstance(embed.get("author"), dict) and isinstance(embed["author"].get("name"), str):
        parts.append((embed["author"], "name", False))
    return parts


def embeds_total(embeds: list[dict]) -> int:
    """Characters Discord counts toward the 6000 limit, across every embed of a message."""
    return sum(text_units(c[k]) for e in embeds for c, k, _ in _embed_parts(e))


def _limit_embed(embed: dict) -> dict:
    out = dict(embed)
    if "title" in out:
        out["title"] = truncate(str(out["title"]), TITLE_LIMIT)
    if "description" in out:
        out["description"] = truncate(str(out["description"]), DESCRIPTION_LIMIT)
    if "fields" in out:
        fields = []
        for raw in list(out.get("fields") or [])[:FIELDS_LIMIT]:
            field = dict(raw) if isinstance(raw, dict) else {}
            field["name"] = truncate(str(field.get("name") or ""), FIELD_NAME_LIMIT) or ZWSP
            field["value"] = truncate(str(field.get("value") or ""), FIELD_VALUE_LIMIT) or ZWSP
            fields.append(field)
        out["fields"] = fields
    if isinstance(out.get("footer"), dict) and "text" in out["footer"]:
        out["footer"] = {**out["footer"], "text": truncate(str(out["footer"]["text"]), FOOTER_TEXT_LIMIT)}
    if isinstance(out.get("author"), dict) and "name" in out["author"]:
        out["author"] = {**out["author"], "name": truncate(str(out["author"]["name"]), AUTHOR_NAME_LIMIT)}
    return out


def _shrink_to_total(embeds: list[dict]) -> None:
    """Shrink descriptions and field values proportionally until the 6000 total holds.

    When titles/footers/field names alone are too big, trailing fields and then trailing
    embeds are dropped (never happens with GemBot's own builders, but stays valid).
    """
    for _ in range(10_000):  # each pass shrinks or removes something; bounded for safety
        if embeds_total(embeds) <= EMBED_TOTAL_LIMIT:
            return
        parts = [p for e in embeds for p in _embed_parts(e)]
        fixed = sum(text_units(c[k]) for c, k, shrink in parts if not shrink)
        shrinkable = [(c, k) for c, k, shrink in parts if shrink]
        flexible = sum(text_units(c[k]) for c, k in shrinkable)
        room = EMBED_TOTAL_LIMIT - fixed - len(shrinkable)  # keep 1 unit per slot for a non-empty value
        if flexible > 0 and room > 0:
            factor = room / flexible
            for container, key in shrinkable:
                if text_units(container[key]) <= 1:  # covered by the 1 unit reserved per slot
                    continue
                shortened = truncate(container[key], int(text_units(container[key]) * factor))
                container[key] = shortened or (ELLIPSIS if key == "value" else "")
            continue
        with_fields = [e for e in embeds if e.get("fields")]
        if with_fields:
            with_fields[-1]["fields"].pop()
        elif len(embeds) > 1:
            embeds.pop()
        else:  # pragma: no cover - one embed without fields always fits after shrinking (<= 2561 fixed)
            embeds[0].pop("description", None)
            return


def enforce_limits(payload: dict) -> dict:
    """Return a deep copy of ``payload`` that respects every Discord message/embed limit."""
    out = copy.deepcopy(payload)
    if isinstance(out.get("content"), str):
        out["content"] = truncate(out["content"], CONTENT_LIMIT)
    if "embeds" in out:
        embeds = [e for e in list(out.get("embeds") or []) if isinstance(e, dict)][:EMBEDS_PER_MESSAGE]
        embeds = [_limit_embed(e) for e in embeds]
        _shrink_to_total(embeds)
        for embed in embeds:
            if embed.get("description") == "":
                del embed["description"]
            if "fields" in embed and not embed["fields"]:
                del embed["fields"]
        out["embeds"] = embeds
    return out
