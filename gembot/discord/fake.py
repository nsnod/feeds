"""An in-memory Discord for tests, ``--dry-run`` and the offline replay.

``FakeDiscord`` implements :class:`gembot.discord.rest.DiscordAPI` with the same payload
handling as the real client (limits enforced, ``allowed_mentions`` locked down), records
every message in :attr:`FakeDiscord.sent` and hands out deterministic snowflake-like ids.
"""

from __future__ import annotations

from typing import Any

from gembot.discord.rest import CHANNEL_CATEGORY, DiscordError, prepare_message

FIRST_ID = 900000000000000001


class FakeDiscord:
    def __init__(
        self,
        *,
        guilds: list[str] | None = None,
        bot_user: dict[str, Any] | None = None,
        start_id: int = FIRST_ID,
    ):
        self.bot_user: dict[str, Any] = bot_user or {"id": "1000", "username": "GemBot", "bot": True}
        self.guilds: dict[str, dict[str, Any]] = {}
        self.channels: dict[str, dict[str, Any]] = {}
        self.messages: dict[str, dict[str, Any]] = {}
        self.sent: list[tuple[str, dict]] = []
        self.calls: list[tuple[str, ...]] = []
        self.failures: dict[str, tuple[Exception, int | None]] = {}  # method -> (error, calls left)
        self._next_id = start_id
        for name in guilds or []:
            self.add_guild(name)

    # ------------------------------------------------------------------ helpers for tests
    def new_id(self) -> str:
        value = str(self._next_id)
        self._next_id += 1
        return value

    def add_guild(self, name: str = "Test Server", guild_id: str | None = None) -> str:
        guild_id = guild_id or self.new_id()
        self.guilds[guild_id] = {"id": guild_id, "name": name}
        return guild_id

    def add_channel(
        self,
        guild_id: str,
        name: str,
        type: int = 0,
        parent_id: str | None = None,
        topic: str | None = None,
    ) -> dict:
        channel_id = self.new_id()
        channel: dict[str, Any] = {
            "id": channel_id,
            "guild_id": guild_id,
            "name": name,
            "type": type,
            "parent_id": parent_id,
            "position": len(self.channels),
        }
        if topic is not None:
            channel["topic"] = topic
        self.channels[channel_id] = channel
        return dict(channel)

    def fail(self, method: str, error: Exception, times: int | None = None) -> None:
        """Make ``method`` raise ``error`` (``times`` calls in a row, or forever when None)."""
        self.failures[method] = (error, times)

    def react(
        self,
        channel_id: str,
        message_id: str,
        emoji: str,
        user_id: str,
        bot: bool = False,
        burst: bool = False,
    ) -> None:
        """Add a reaction as ``user_id`` (tests / replay). ``burst`` = a super reaction.

        Like Discord, user objects carry ``"bot": True`` only for bots (the key is absent otherwise).
        """
        message = self._message(channel_id, message_id)
        user: dict[str, Any] = {"id": str(user_id), "username": f"user{user_id}"}
        if bot:
            user["bot"] = True
        users = message["burst" if burst else "reactions"].setdefault(emoji, [])
        if all(u["id"] != user["id"] for u in users):
            users.append(user)

    def unreact(
        self, channel_id: str, message_id: str, emoji: str, user_id: str, burst: bool = False
    ) -> None:
        store = self._message(channel_id, message_id)["burst" if burst else "reactions"]
        users = [u for u in store.get(emoji, []) if u["id"] != str(user_id)]
        if users:
            store[emoji] = users
        else:
            store.pop(emoji, None)

    def messages_in(self, channel_id: str) -> list[dict]:
        return [m for m in self.messages.values() if m["channel_id"] == channel_id]

    def channel_by_name(self, name: str) -> dict | None:
        return next((c for c in self.channels.values() if c["name"] == name), None)

    # ------------------------------------------------------------------ internals
    def _call(self, method: str, *args: str) -> None:
        self.calls.append((method, *args))
        if method not in self.failures:
            return
        error, left = self.failures[method]
        if left is not None:
            if left <= 1:
                del self.failures[method]
            else:
                self.failures[method] = (error, left - 1)
        raise error

    def _guild(self, guild_id: str) -> dict:
        if guild_id not in self.guilds:
            raise DiscordError("Unknown Guild", 404, code=10004, discord_message="Unknown Guild")
        return self.guilds[guild_id]

    def _channel(self, channel_id: str) -> dict:
        if channel_id not in self.channels:
            raise DiscordError("Unknown Channel", 404, code=10003, discord_message="Unknown Channel")
        return self.channels[channel_id]

    def _message(self, channel_id: str, message_id: str) -> dict:
        message = self.messages.get(message_id)
        if message is None or message["channel_id"] != channel_id:
            raise DiscordError("Unknown Message", 404, code=10008, discord_message="Unknown Message")
        return message

    # ------------------------------------------------------------------ DiscordAPI
    def me(self) -> dict:
        self._call("me")
        return dict(self.bot_user)

    def list_guilds(self) -> list[dict]:
        self._call("list_guilds")
        return [dict(g) for g in self.guilds.values()]

    def list_channels(self, guild_id: str) -> list[dict]:
        self._call("list_channels", guild_id)
        self._guild(guild_id)
        return [dict(c) for c in self.channels.values() if c["guild_id"] == guild_id]

    def create_channel(
        self, guild_id: str, name: str, type: int, parent_id: str | None = None, topic: str | None = None
    ) -> dict:
        self._call("create_channel", guild_id, name)
        self._guild(guild_id)
        if parent_id is not None and self._channel(parent_id)["type"] != CHANNEL_CATEGORY:
            raise DiscordError("Invalid Form Body", 400, code=50035, discord_message="Invalid Form Body")
        return self.add_channel(guild_id, name, type, parent_id, topic)

    def send_message(self, channel_id: str, payload: dict) -> dict:
        self._call("send_message", channel_id)
        self._channel(channel_id)
        body = prepare_message(payload)
        message_id = self.new_id()
        self.messages[message_id] = {
            "id": message_id,
            "channel_id": channel_id,
            "payload": body,
            "reactions": {},  # emoji -> users (normal reactions)
            "burst": {},  # emoji -> users (super reactions)
        }
        self.sent.append((channel_id, body))
        return {"id": message_id, "channel_id": channel_id, **{k: v for k, v in body.items() if k != "id"}}

    def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        self._call("add_reaction", channel_id, message_id, emoji)
        self.react(
            channel_id, message_id, emoji, str(self.bot_user["id"]), bot=bool(self.bot_user.get("bot"))
        )

    def get_message(self, channel_id: str, message_id: str) -> dict:
        self._call("get_message", channel_id, message_id)
        message = self._message(channel_id, message_id)
        bot_id = str(self.bot_user["id"])
        reactions = []
        for emoji in dict.fromkeys([*message["reactions"], *message["burst"]]):
            normal = message["reactions"].get(emoji, [])
            burst = message["burst"].get(emoji, [])
            reactions.append(
                {
                    "emoji": {"id": None, "name": emoji},
                    "count": len(normal) + len(burst),  # like Discord: super reactions + the bot's own
                    "count_details": {"burst": len(burst), "normal": len(normal)},
                    "me": any(u["id"] == bot_id for u in normal),
                    "me_burst": any(u["id"] == bot_id for u in burst),
                }
            )
        data: dict[str, Any] = {"id": message_id, "channel_id": channel_id}
        if reactions:  # Discord omits the key when a message has no reactions
            data["reactions"] = reactions
        return data

    def get_reaction_users(
        self,
        channel_id: str,
        message_id: str,
        emoji: str,
        limit: int = 100,
        after: str | None = None,
        burst: bool = False,
    ) -> list[dict]:
        """Users ordered by id, like Discord; ``after`` pages, ``burst`` lists super reactions."""
        self._call("get_reaction_users", channel_id, message_id, emoji)
        store = self._message(channel_id, message_id)["burst" if burst else "reactions"]
        users = sorted(store.get(emoji, []), key=lambda u: _id_key(u["id"]))
        if after is not None:
            users = [u for u in users if _id_key(u["id"]) > _id_key(after)]
        return [dict(u) for u in users][: max(1, min(limit, 100))]


def _id_key(user_id: str) -> tuple[int, int, str]:
    """Order ids like Discord does (snowflakes compare as numbers)."""
    return (0, int(user_id), "") if user_id.isdigit() else (1, 0, user_id)
