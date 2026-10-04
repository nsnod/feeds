"""Setup flow (idempotent channel creation, welcome once) and the one-time gateway connect."""

from __future__ import annotations

import asyncio
import logging
import warnings

import pytest

with warnings.catch_warnings():  # discord.py imports the deprecated stdlib audioop module
    warnings.simplefilter("ignore", DeprecationWarning)
    import discord

from gembot.discord import setup as S
from gembot.discord.fake import FakeDiscord
from gembot.models import State
from tests.factories import NOW, make_config

GUILD_NAMES = ("gem-alarm", "gem-roundup", "gembot-status")


def run(fake: FakeDiscord, state: State, config=None, **kw) -> S.SetupResult:
    return S.run_setup(fake, config or make_config(), state, now=NOW, **kw)


def text_channels(fake: FakeDiscord) -> list[dict]:
    return [c for c in fake.channels.values() if c["type"] == 0]


# ---------------------------------------------------------------- first run / second run


def test_first_run_creates_category_channels_welcome_and_test_alarm():
    fake = FakeDiscord(guilds=["Friends"])
    state = State()
    result = run(fake, state)

    category = fake.channel_by_name("GemBot")
    assert category["type"] == 4
    assert result.created == ["category GemBot", "#gem-alarm", "#gem-roundup", "#gembot-status"]
    for role, name in zip(S.ROLES, GUILD_NAMES, strict=True):
        channel = fake.channel_by_name(name)
        assert channel["type"] == 0 and channel["parent_id"] == category["id"]
        assert channel["topic"] == S.TOPICS[role]
        assert result.channels[role] == channel["id"]

    meta = state.meta.discord
    assert meta.guild_id == result.guild_id == next(iter(fake.guilds))
    assert meta.category_id == result.category_id == category["id"]
    assert meta.channels == result.channels
    assert meta.bot_user_id == "1000"

    assert result.posted == ["welcome message in #gembot-status", "TEST alarm in #gem-alarm"]
    (welcome_channel, welcome), (alarm_channel, alarm) = fake.sent
    assert welcome_channel == result.channels["status"]
    assert welcome["embeds"][0]["title"] == "👋 Hi, I'm GemBot!"
    assert f"<#{result.channels['alarm']}>" in welcome["embeds"][0]["fields"][0]["value"]
    assert alarm_channel == result.channels["alarm"]
    assert alarm["embeds"][0]["title"] == "🧪 TEST — 🚨 GEM ALARM: GemBot Test Game"
    test_id = next(m["id"] for m in fake.messages.values() if m["channel_id"] == alarm_channel)
    assert list(fake.messages[test_id]["reactions"]) == ["👍", "👎"]

    assert meta.welcome_message_id is not None
    kinds = {m.kind for m in state.posted.messages.values()}
    assert kinds == {"welcome", "test"}
    assert all(not m.entries for m in state.posted.messages.values())


def test_second_run_creates_and_posts_nothing():
    fake = FakeDiscord(guilds=["Friends"])
    state = State()
    first = run(fake, state)
    channels_before, sent_before, calls_before = dict(fake.channels), len(fake.sent), len(fake.calls)

    second = run(fake, state)
    assert second.created == [] and second.posted == []
    assert second.channels == first.channels and second.category_id == first.category_id
    assert fake.channels == channels_before and len(fake.sent) == sent_before
    assert [c[0] for c in fake.calls[calls_before:]] == ["me", "list_guilds", "list_channels"]

    # state lost (e.g. a fresh bot-state branch): channels are found again by name
    third = run(fake, State())
    assert third.created == [] and third.channels == first.channels
    assert third.posted  # a new state has no welcome id, so the welcome goes out again


def test_force_welcome_posts_again_without_new_channels():
    fake = FakeDiscord(guilds=["Friends"])
    state = State()
    run(fake, state)
    first_welcome = state.meta.discord.welcome_message_id
    result = run(fake, state, force_welcome=True)
    assert result.created == [] and len(result.posted) == 2
    assert state.meta.discord.welcome_message_id != first_welcome
    assert len(fake.sent) == 4


def test_reuses_existing_channels_by_name_case_insensitively():
    fake = FakeDiscord()
    guild = fake.add_guild("Friends")
    fake.add_channel(guild, "general")
    other_category = fake.add_channel(guild, "Text Channels", type=4)
    existing_category = fake.add_channel(guild, "gembot", type=4)
    alarm = fake.add_channel(guild, "Gem-Alarm", parent_id=other_category["id"])
    fake.add_channel(guild, "gem-roundup", type=2)  # a voice channel with the same name is not reused
    state = State()

    result = run(fake, state)
    assert result.category_id == existing_category["id"]
    assert result.channels["alarm"] == alarm["id"]  # kept in its own category, not moved
    assert result.created == ["#gem-roundup", "#gembot-status"]
    assert len([c for c in text_channels(fake) if c["name"].lower() == "gem-alarm"]) == 1
    assert fake.channels[result.channels["roundup"]]["type"] == 0


def test_renamed_channels_are_found_by_their_stored_id():
    fake = FakeDiscord(guilds=["Friends"])
    state = State()
    first = run(fake, state)
    fake.channels[first.channels["alarm"]]["name"] = "🚨-alarms"
    fake.channels[first.category_id]["name"] = "Bots"
    again = run(fake, state)
    assert again.created == [] and again.channels == first.channels and again.category_id == first.category_id


# ---------------------------------------------------------------- choosing the server


def test_multiple_guilds_without_guild_id_explain_how_to_fix():
    fake = FakeDiscord(guilds=["Friends", "Work"])
    with pytest.raises(S.SetupError) as info:
        run(fake, State())
    message = str(info.value)
    assert "2 servers" in message and "DISCORD_GUILD_ID" in message and "Developer Mode" in message
    assert "'Friends'" in message and "'Work'" in message
    assert fake.sent == []


def test_guild_id_secret_picks_that_server():
    fake = FakeDiscord(guilds=["Friends", "Work"])
    work = next(g for g, v in fake.guilds.items() if v["name"] == "Work")
    config = make_config(env={"DISCORD_GUILD_ID": work})
    result = run(fake, State(), config)
    assert result.guild_id == work
    assert all(c["guild_id"] == work for c in fake.channels.values())


def test_guild_id_secret_for_a_server_the_bot_is_not_in():
    fake = FakeDiscord(guilds=["Friends"])
    with pytest.raises(S.SetupError, match="not in that server"):
        run(fake, State(), make_config(env={"DISCORD_GUILD_ID": "123"}))


def test_bot_in_no_server():
    with pytest.raises(S.SetupError, match="invite link"):
        run(FakeDiscord(), State())


def test_moving_to_another_server_sets_everything_up_there():
    fake = FakeDiscord(guilds=["Old"])
    state = State()
    first = run(fake, state)
    new_guild = fake.add_guild("New")
    del fake.guilds[first.guild_id]
    result = run(fake, state)
    assert result.guild_id == new_guild
    assert len(result.created) == 4 and len(result.posted) == 2
    assert all(fake.channels[cid]["guild_id"] == new_guild for cid in result.channels.values())


def test_custom_channel_names_from_settings():
    config = make_config()
    config.settings.discord.alarm_channel = "alarms"
    config.settings.discord.category_name = "Indie Radar"
    fake = FakeDiscord(guilds=["Friends"])
    result = run(fake, State(), config)
    assert result.created[:2] == ["category Indie Radar", "#alarms"]
    assert fake.channels[result.channels["alarm"]]["name"] == "alarms"


def test_channel_names_are_matched_the_way_discord_normalises_them():
    assert S.normalize_channel_name("  #Gem   Alarm ") == "gem-alarm"
    config = make_config()
    config.settings.discord.alarm_channel = "Gem Alarm"  # Discord would store this as "gem-alarm"
    fake = FakeDiscord()
    guild = fake.add_guild("Friends")
    existing = fake.add_channel(guild, "gem-alarm")
    result = run(fake, State(), config)
    assert result.channels["alarm"] == existing["id"]
    assert "#Gem Alarm" not in result.created


# ---------------------------------------------------------------- gateway connect


class FakeClient:
    behaviour = "ready"
    instances: list[FakeClient] = []

    def __init__(self, *, intents):
        self.intents = intents
        self.closed = False
        self.user = "GemBot#0001"
        self.token = None
        FakeClient.instances.append(self)

    def event(self, coro):
        setattr(self, coro.__name__, coro)
        return coro

    async def start(self, token, *, reconnect=True):
        self.token = token
        self.reconnect = reconnect
        if self.behaviour == "ready":
            await self.on_ready()
        elif self.behaviour == "login_failure":
            raise discord.LoginFailure("Improper token has been passed.")
        elif self.behaviour == "intents":
            raise discord.PrivilegedIntentsRequired(None)
        elif self.behaviour == "hang":
            await asyncio.sleep(10)
        else:
            raise OSError("network is down")

    async def close(self):
        self.closed = True

    def is_closed(self):
        return self.closed

    async def __aenter__(self):  # discord.py: `async with client:` closes it on the way out
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        if not self.closed:
            await self.close()


@pytest.fixture
def fake_client(monkeypatch):
    FakeClient.instances = []
    monkeypatch.setattr(discord, "Client", FakeClient)
    return FakeClient


def test_gateway_ready_returns_true_and_closes(fake_client, monkeypatch):
    monkeypatch.setattr(fake_client, "behaviour", "ready")
    assert S.connect_gateway_once("tok-SECRET") is True
    client = fake_client.instances[0]
    assert client.closed and client.entered and client.token == "tok-SECRET" and client.reconnect is False
    assert client.intents.value == 0  # no intents at all, privileged or not


@pytest.mark.parametrize(
    ("behaviour", "logged", "level"),
    [
        ("login_failure", "rejected the bot token", logging.ERROR),
        ("intents", "privileged intents", logging.WARNING),
        ("hang", "did not say READY within 0s (optional check", logging.WARNING),
        (
            "boom",
            "optional one-time connect failed (posting over REST does not need it): OSError",
            logging.WARNING,
        ),
    ],
)
def test_gateway_failures_return_false(fake_client, monkeypatch, caplog, behaviour, logged, level):
    monkeypatch.setattr(fake_client, "behaviour", behaviour)
    with caplog.at_level(logging.WARNING):
        assert S.connect_gateway_once("tok-SECRET", timeout_s=0.05) is False
    assert logged in caplog.text
    assert [r.levelno for r in caplog.records] == [level]
    assert "tok-SECRET" not in caplog.text
    assert fake_client.instances[0].closed


def test_gateway_without_token(caplog):
    with caplog.at_level(logging.ERROR):
        assert S.connect_gateway_once("") is False
    assert "DISCORD_BOT_TOKEN" in caplog.text
