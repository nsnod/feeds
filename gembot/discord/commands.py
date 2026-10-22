"""Pause / resume GemBot from Discord.

GemBot isn't online between scans, so commands are read at the start of each scan: the
messages posted in #gembot-status since the last scan, oldest first.

* ``@GemBot pause`` / ``@GemBot resume`` / ``@GemBot status`` always work: Discord lets a bot
  read messages that mention it.
* Plain ``pause`` / ``resume`` / ``status`` work too once "Message Content Intent" is turned
  on for the bot in the Discord Developer Portal (otherwise Discord hands the bot an empty
  text for them; GemBot notices that and says how to turn it on).

While paused, a scan only looks for ``resume`` here: it reads no sources and posts nothing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from gembot.discord.rest import DiscordAPI

log = logging.getLogger(__name__)

PAUSE_WORDS = frozenset({"pause", "stop", "sleep"})
RESUME_WORDS = frozenset({"resume", "unpause", "start", "wake"})
STATUS_WORDS = frozenset({"status"})
MAX_PLAIN_WORDS = 3  # "pause", "pause please", "pause for now"; longer chatter is not a command
FIRST_LOOK_BACK = timedelta(hours=2)  # with no cursor yet, commands this recent still count
DISCORD_EPOCH_MS = 1420070400000
_MENTION = re.compile(r"<@!?(\d+)>")


@dataclass
class Command:
    action: str  # "pause" | "resume" | "status"
    user_id: str
    message_id: str


@dataclass
class Inbox:
    commands: list[Command]
    newest_id: str | None  # the cursor for the next scan
    unreadable: int = 0  # messages from people whose text Discord withheld (intent off)


def snowflake_at(moment: datetime) -> str:
    """The smallest Discord id created at ``moment`` (to read "messages since then")."""
    ms = int(moment.timestamp() * 1000)
    return str(max(ms - DISCORD_EPOCH_MS, 0) << 22)


def parse_command(content: str, bot_user_id: str | None) -> str | None:
    """``pause`` / ``resume`` / ``status`` if the message is one, else None."""
    mentioned = bool(bot_user_id) and any(m == str(bot_user_id) for m in _MENTION.findall(content))
    text = _MENTION.sub(" ", content).lower()
    words = re.findall(r"[a-z]+", text)
    if not words:
        return None
    if not mentioned and len(words) > MAX_PLAIN_WORDS:
        return None
    first = words[0]
    if first in PAUSE_WORDS:
        return "pause"
    if first in RESUME_WORDS:
        return "resume"
    if first in STATUS_WORDS:
        return "status"
    return None


def read_inbox(
    api: DiscordAPI, channel_id: str, *, bot_user_id: str | None, after: str | None, now: datetime
) -> Inbox:
    """Commands posted in ``channel_id`` after the cursor ``after`` (oldest first)."""
    start = after or snowflake_at(now - FIRST_LOOK_BACK)
    messages = api.get_messages(channel_id, after=start, limit=50)
    messages = sorted(messages, key=lambda m: int(str(m.get("id") or 0)))
    commands: list[Command] = []
    unreadable = 0
    for message in messages:
        author = message.get("author") or {}
        user_id = str(author.get("id") or "")
        if not user_id or author.get("bot") or (bot_user_id and user_id == str(bot_user_id)):
            continue
        content = str(message.get("content") or "")
        if not content.strip():
            if not (message.get("attachments") or message.get("embeds") or message.get("sticker_items")):
                unreadable += 1  # a person wrote something, Discord didn't let us see it
            continue
        action = parse_command(content, bot_user_id)
        if action:
            commands.append(Command(action, user_id, str(message["id"])))
    newest = str(messages[-1]["id"]) if messages else start
    return Inbox(commands, newest, unreadable)
