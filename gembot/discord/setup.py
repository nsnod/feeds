"""One-time Discord setup: pick the server, create the channels, say hello.

:func:`run_setup` is idempotent: it finds or creates the "GemBot" category and the
``#gem-alarm`` / ``#gem-roundup`` / ``#gembot-status`` channels (reusing channels it made
before, or channels that already have those names), stores their ids in
``state.meta.discord`` and posts the welcome message plus a TEST alarm only once.

:func:`connect_gateway_once` opens one short gateway session with discord.py (no
privileged intents) and closes it as soon as Discord says READY. Brand-new bots have
historically needed one gateway connection before REST message sends work; it is
harmless either way, so the Setup workflow always does it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from gembot.config import Config
from gembot.discord.embeds import welcome_payload
from gembot.discord.publish import Publisher
from gembot.discord.rest import CHANNEL_ANNOUNCEMENT, CHANNEL_CATEGORY, CHANNEL_TEXT, DiscordAPI
from gembot.models import Features, GemCard, PostedMessage, State

log = logging.getLogger(__name__)

ROLES = ("alarm", "roundup", "status")
TEXT_TYPES = (CHANNEL_TEXT, CHANNEL_ANNOUNCEMENT)

TOPICS = {
    "alarm": "🚨 Breakout indie co-op games, posted the moment GemBot spots them. React 👍/👎 to teach it.",
    "roundup": "📋 Every couple of hours: small indie games worth a look. React 👍/👎 on each one.",
    "status": "🩺 GemBot's notes: when a source breaks or recovers, and what it learned from your 👍/👎.",
}

TEST_GAME_ID = "test:gembot"

GUILD_ID_HOWTO = (
    "To copy a server ID: in Discord open User Settings -> Advanced and turn on Developer Mode, "
    "then right-click (or long-press) the server icon and choose 'Copy Server ID'. "
    "Add it as a repository secret named DISCORD_GUILD_ID (GitHub repo -> Settings -> "
    "Secrets and variables -> Actions -> New repository secret) and run the Setup workflow again."
)


class SetupError(RuntimeError):
    """Setup cannot continue; the message tells a non-programmer what to do."""


@dataclass
class SetupResult:
    guild_id: str
    category_id: str
    channels: dict[str, str]
    created: list[str] = field(default_factory=list)  # e.g. ["category GemBot", "#gem-alarm"]
    posted: list[str] = field(default_factory=list)  # e.g. ["welcome message in #gembot-status"]


def _guild_names(guilds: list[dict]) -> str:
    return ", ".join(f"'{g.get('name', '?')}' ({g.get('id')})" for g in guilds)


def pick_guild(guilds: list[dict], wanted: str | None) -> str:
    """The server to use: DISCORD_GUILD_ID if set, else the only server the bot is in."""
    if wanted:
        if any(str(g.get("id")) == str(wanted) for g in guilds):
            return str(wanted)
        listing = _guild_names(guilds) or "none"
        raise SetupError(
            f"DISCORD_GUILD_ID is set to {wanted}, but the bot is not in that server "
            f"(it is in: {listing}). Invite the bot to that server with the link from the README, "
            "or fix the secret."
        )
    if len(guilds) == 1:
        return str(guilds[0]["id"])
    if not guilds:
        raise SetupError(
            "The bot is not in any Discord server yet. Open the invite link from the README, "
            "add the bot to your server, then run the Setup workflow again."
        )
    raise SetupError(
        f"The bot is in {len(guilds)} servers ({_guild_names(guilds)}), so I don't know which one to "
        f"use. Add the DISCORD_GUILD_ID secret with the ID of the server GemBot should post in. "
        + GUILD_ID_HOWTO
    )


def _same_name(a: str | None, b: str) -> bool:
    return (a or "").strip().lstrip("#").casefold() == b.strip().lstrip("#").casefold()


def _find(
    channels: list[dict], *, name: str, types: tuple[int, ...], known_id: str | None = None
) -> dict | None:
    """A channel we created before (by id, even if renamed), else one with the same name."""
    if known_id:
        for channel in channels:
            if str(channel.get("id")) == str(known_id) and channel.get("type") in types:
                return channel
    matches = [c for c in channels if c.get("type") in types and _same_name(c.get("name"), name)]
    matches.sort(key=lambda c: (c.get("position", 0), str(c.get("id"))))
    return matches[0] if matches else None


def make_test_card(now: datetime) -> GemCard:
    """The fake game shown in the TEST alarm right after setup."""
    return GemCard(
        game_id=TEST_GAME_ID,
        title="GemBot Test Game",
        score=80,
        reasons=[
            "This is a test so you can see what an alarm looks like (not a real game)",
            'Real alarms show the numbers behind the score, like "312 upvotes in 3h on r/IndieDev '
            '(9× normal for that sub)"',
            "Tap 👍 or 👎 below: on real alarms, that's how you teach me what you like",
        ],
        pitch="Not a real game. Real alarms link to the Steam or itch.io page and to the posts "
        "people are talking in.",
        first_seen=now,
        test=True,
    )


def run_setup(
    api: DiscordAPI, config: Config, state: State, *, now: datetime, force_welcome: bool = False
) -> SetupResult:
    """Find the server, find-or-create the category and channels, post the welcome once."""
    settings = config.settings.discord
    meta = state.meta.discord

    me = api.me()
    meta.bot_user_id = str(me["id"])
    log.info("setup: logged in as %s (%s)", me.get("username"), meta.bot_user_id)

    guild_id = pick_guild(api.list_guilds(), config.secrets.discord_guild_id)
    if meta.guild_id and meta.guild_id != guild_id:
        log.info("setup: moving from server %s to %s", meta.guild_id, guild_id)
        meta.category_id = None
        meta.channels = {}
        meta.welcome_message_id = None
    meta.guild_id = guild_id

    channels = api.list_channels(guild_id)
    created: list[str] = []

    category = _find(
        channels, name=settings.category_name, types=(CHANNEL_CATEGORY,), known_id=meta.category_id
    )
    if category is None:
        category = api.create_channel(guild_id, settings.category_name, CHANNEL_CATEGORY)
        channels.append(category)
        created.append(f"category {settings.category_name}")
    meta.category_id = str(category["id"])

    names = {
        "alarm": settings.alarm_channel,
        "roundup": settings.roundup_channel,
        "status": settings.status_channel,
    }
    ids: dict[str, str] = {}
    for role in ROLES:
        channel = _find(channels, name=names[role], types=TEXT_TYPES, known_id=meta.channels.get(role))
        if channel is None:
            channel = api.create_channel(
                guild_id, names[role], CHANNEL_TEXT, parent_id=meta.category_id, topic=TOPICS[role]
            )
            channels.append(channel)
            created.append(f"#{names[role]}")
        ids[role] = str(channel["id"])
    meta.channels = ids

    posted: list[str] = []
    if meta.welcome_message_id is None or force_welcome:
        feedback = config.settings.feedback
        welcome = api.send_message(
            ids["status"], welcome_payload(ids, up=feedback.up_emoji, down=feedback.down_emoji)
        )
        meta.welcome_message_id = str(welcome["id"])
        state.posted.messages[meta.welcome_message_id] = PostedMessage(
            message_id=meta.welcome_message_id, channel_id=ids["status"], kind="welcome", posted_at=now
        )
        posted.append(f"welcome message in #{names['status']}")

        publisher = Publisher(api, ids, config.settings, sleep=lambda _s: None)
        test_message = publisher.post_alarm(make_test_card(now), Features(), now)
        state.posted.messages[test_message.message_id] = test_message
        posted.append(f"TEST alarm in #{names['alarm']}")

    for line in created:
        log.info("setup: created %s", line)
    return SetupResult(
        guild_id=guild_id, category_id=meta.category_id, channels=dict(ids), created=created, posted=posted
    )


# --------------------------------------------------------------------------------------
# gateway
# --------------------------------------------------------------------------------------


async def _connect(token: str, timeout_s: float) -> bool:
    import discord

    client: Any = discord.Client(intents=discord.Intents.none())
    ready = False

    @client.event
    async def on_ready() -> None:
        nonlocal ready
        ready = True
        log.info("gateway: connected as %s, closing", getattr(client, "user", None))
        await client.close()

    try:
        await asyncio.wait_for(client.start(token, reconnect=False), timeout=timeout_s)
    except TimeoutError:
        log.error("gateway: Discord did not say READY within %.0fs", timeout_s)
    finally:
        if not client.is_closed():
            await client.close()
    return ready


def connect_gateway_once(token: str, timeout_s: float = 30.0) -> bool:
    """Connect to the Discord gateway once (intents: none), wait for READY, disconnect.

    Returns True when Discord said READY, False otherwise (the reason is logged; the token
    never is).
    """
    if not token:
        log.error("gateway: DISCORD_BOT_TOKEN is not set")
        return False
    import discord

    try:
        return asyncio.run(_connect(token, timeout_s))
    except discord.LoginFailure:
        log.error(
            "gateway: Discord rejected the bot token. Copy a fresh token (Developer Portal -> "
            "your app -> Bot -> Reset Token) into the DISCORD_BOT_TOKEN secret."
        )
    except discord.PrivilegedIntentsRequired:  # cannot happen with Intents.none(), kept for clarity
        log.error("gateway: Discord asked for privileged intents; GemBot does not need any.")
    except Exception as exc:  # network trouble, gateway closed, ...
        log.error("gateway: could not connect: %s: %s", type(exc).__name__, exc)
    return False
