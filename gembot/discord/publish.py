"""Turn scored games into Discord posts.

* :func:`build_card` gathers what a post needs from a :class:`Game` and its mentions.
* :class:`Publisher` sends alarms, roundups and status notes through any
  :class:`~gembot.discord.rest.DiscordAPI`, adds the 👍/👎 reactions and returns the
  :class:`PostedMessage` records the feedback loop reads later.

Roundups are **one header message plus one short message per game**: Discord reactions
attach to a whole message, so a message per game gives every game its own clean 👍/👎
label (with several embeds in one message a 👍 could not say which game it meant).
"""

from __future__ import annotations

import html
import logging
import re
import time
from collections.abc import Callable
from datetime import datetime
from urllib.parse import urlsplit

from gembot.config import Settings
from gembot.discord.embeds import (
    alarm_payload,
    roundup_entry_payload,
    roundup_header_payload,
    shorten,
    status_payload,
)
from gembot.discord.rest import DiscordAPI
from gembot.http import BudgetExceeded, HttpError
from gembot.models import (
    Decision,
    Features,
    Game,
    GemCard,
    Mention,
    PostedEntry,
    PostedMessage,
    platform_label,
)

log = logging.getLogger(__name__)

PITCH_CHARS = 140
_STEAM_APP = re.compile(r"^/app/(\d+)")


class PublishError(RuntimeError):
    """A post could not be made at all (e.g. the channel was never set up)."""


# --------------------------------------------------------------------------------------
# cards
# --------------------------------------------------------------------------------------


def _url_key(url: str) -> str:
    """A loose identity for de-duplicating links (scheme, ``www.``, trailing ``/`` ignored)."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    if host == "store.steampowered.com" and (match := _STEAM_APP.match(parts.path)):
        return f"steam:{match.group(1)}"
    key = host + parts.path.rstrip("/")
    return f"{key}?{parts.query}" if parts.query else key


def mention_label(mention: Mention) -> str:
    """``Reddit r/IndieDev``, ``Bluesky``, ``itch.io``, the feed name for generic RSS, ..."""
    if mention.source == "reddit" and mention.channel and mention.channel.startswith("r/"):
        return f"Reddit {mention.channel}"
    if mention.source == "rss" and mention.extra.get("feed_name"):
        return shorten(str(mention.extra["feed_name"]), 40)
    return platform_label(mention.source)


def _clean_pitch(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = re.sub(r"<[^>]+>", " ", html.unescape(text))
    cleaned = shorten(cleaned, PITCH_CHARS)
    return cleaned or None


def build_card(
    game: Game,
    mentions: dict[str, Mention],
    *,
    score: float,
    reasons: list[str],
    decision: Decision | None = None,
    max_links: int = 5,
) -> GemCard:
    """Everything the embeds need for one game: best URL, links best-first, pitch, thumbnail, flags."""
    own = sorted(
        (mentions[k] for k in game.mention_keys if k in mentions),
        key=lambda m: (-m.engagement.total, m.created_at, m.key),
    )
    candidates: list[tuple[str, str]] = []
    if game.steam_appid:
        candidates.append(("Steam", f"https://store.steampowered.com/app/{game.steam_appid}/"))
    if game.itch_url:
        candidates.append(("itch.io", game.itch_url))
    candidates.extend((mention_label(m), m.url) for m in own if m.url)

    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label, url in candidates:
        key = _url_key(url)
        if key in seen:
            continue
        seen.add(key)
        links.append((label, url))
        if len(links) >= max_links:
            break

    steam = game.steam
    pitch = (
        _clean_pitch(game.pitch)
        or _clean_pitch(game.llm.one_line_pitch if game.llm else None)
        or _clean_pitch(steam.short_description if steam else None)
    )
    thumb = game.thumb or (steam.header_image if steam else None)
    if not thumb:
        thumb = next((m.media_thumb for m in own if m.media_thumb), None)

    return GemCard(
        game_id=game.game_id,
        title=game.title or (steam.name if steam else "") or game.game_id,
        url=game.best_url(mentions),
        score=score,
        reasons=list(reasons),
        pitch=pitch,
        links=links,
        thumb=thumb,
        first_seen=game.first_seen,
        escalated=bool(decision and decision.escalated),
        would_have_alarmed=bool(decision and decision.would_have_alarmed),
        heating_up=bool(decision and decision.heating_up),
    )


# --------------------------------------------------------------------------------------
# publisher
# --------------------------------------------------------------------------------------


class Publisher:
    def __init__(
        self,
        api: DiscordAPI,
        channels: dict[str, str],
        settings: Settings,
        *,
        ping_role_id: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
        message_delay_s: float = 0.5,
    ):
        self.api = api
        self.channels = dict(channels)
        self.settings = settings
        self.ping_role_id = ping_role_id
        self.sleep = sleep
        self.message_delay_s = message_delay_s

    @property
    def emojis(self) -> tuple[str, str]:
        return self.settings.feedback.up_emoji, self.settings.feedback.down_emoji

    def _channel(self, role: str) -> str:
        channel_id = self.channels.get(role)
        if not channel_id:
            raise PublishError(
                f"The Discord '{role}' channel is not set up yet. Run the 'Setup GemBot' workflow first."
            )
        return channel_id

    def _react(self, channel_id: str, message_id: str) -> list[str]:
        """Add 👍 then 👎. A failure is logged and never loses the message; returns failed emojis."""
        failed = []
        for emoji in self.emojis:
            try:
                self.api.add_reaction(channel_id, message_id, emoji)
            except Exception as exc:  # any failure: the message itself was sent, keep it
                log.warning("discord: could not add %s to message %s: %s", emoji, message_id, exc)
                failed.append(emoji)
        return failed

    def post_alarm(self, card: GemCard, features: Features, now: datetime) -> PostedMessage:
        """Send one 🚨 alarm (+ 👍/👎). A TEST card is recorded as kind "test" with no entries."""
        channel_id = self._channel("alarm")
        payload = alarm_payload(card, ping_role_id=None if card.test else self.ping_role_id, now=now)
        message = self.api.send_message(channel_id, payload)
        message_id = str(message["id"])
        self._react(channel_id, message_id)
        entries = (
            [] if card.test else [PostedEntry(game_id=card.game_id, score=card.score, features=features)]
        )
        return PostedMessage(
            message_id=message_id,
            channel_id=channel_id,
            kind="test" if card.test else "alarm",
            posted_at=now,
            entries=entries,
        )

    def post_roundup(self, cards: list[tuple[GemCard, Features]], now: datetime) -> list[PostedMessage]:
        """Header + one message per game. Nothing at all is posted when ``cards`` is empty.

        If the header fails the error propagates (nothing was posted). After that, a failed
        entry is logged and skipped so the messages already sent are still returned.
        """
        if not cards:
            return []
        channel_id = self._channel("roundup")
        up, down = self.emojis
        header = self.api.send_message(channel_id, roundup_header_payload(now, len(cards), up=up, down=down))
        posted = [
            PostedMessage(
                message_id=str(header["id"]), channel_id=channel_id, kind="roundup_header", posted_at=now
            )
        ]
        for card, features in cards:
            if self.message_delay_s > 0:
                self.sleep(self.message_delay_s)
            try:
                message = self.api.send_message(channel_id, roundup_entry_payload(card))
            except BudgetExceeded as exc:
                log.warning("discord: roundup stopped early: %s", exc)
                break
            except HttpError as exc:
                log.warning("discord: could not post roundup entry %s: %s", card.game_id, exc)
                if getattr(exc, "blocked", False):  # Discord/Cloudflare blocked us: stop for this run
                    break
                continue
            message_id = str(message["id"])
            self._react(channel_id, message_id)
            posted.append(
                PostedMessage(
                    message_id=message_id,
                    channel_id=channel_id,
                    kind="roundup",
                    posted_at=now,
                    entries=[PostedEntry(game_id=card.game_id, score=card.score, features=features)],
                )
            )
        return posted

    def post_status(self, lines: list[str], now: datetime) -> PostedMessage | None:
        """A note in #gembot-status. Returns None when there is nothing to say."""
        lines = [line for line in lines if line and line.strip()]
        if not lines:
            return None
        channel_id = self._channel("status")
        message = self.api.send_message(channel_id, status_payload(lines))
        return PostedMessage(
            message_id=str(message["id"]), channel_id=channel_id, kind="status", posted_at=now
        )
