"""Pause / resume / status from #gembot-status."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import respx

from gembot.discord.commands import parse_command, read_inbox, snowflake_at
from gembot.discord.fake import FakeDiscord
from gembot.discord.rest import DiscordClient
from gembot.http import Budget, HttpError
from tests.factories import NOW, make_http
from tests.test_pipeline import World, hot_collectors

BOT = "1000"  # FakeDiscord's bot user id
ME = f"<@{BOT}>"
HALF_HOUR = timedelta(minutes=30)


@pytest.mark.parametrize(
    ("content", "action"),
    [
        (f"{ME} pause", "pause"),
        (f"<@!{BOT}> PAUSE!", "pause"),
        (f"hey {ME} pause for the weekend please, we're travelling", None),  # first word decides
        (f"{ME} pause for the weekend please, we're travelling", "pause"),
        ("pause", "pause"),
        ("Pause please", "pause"),
        ("stop", "pause"),
        ("resume", "resume"),
        (f"{ME} unpause", "resume"),
        ("start", "resume"),
        ("status?", "status"),
        ("please don't pause it", None),
        ("stop spamming the channel with this many games", None),  # long chatter is not a command
        ("pausing is fine", None),
        ("<@2222> pause", "pause"),  # someone else mentioned, short message: still the command
        ("", None),
        (ME, None),
    ],
)
def test_parse_command(content, action):
    assert parse_command(content, BOT) == action


def test_snowflake_at_matches_discords_epoch():
    # a known pair: 175928847299117063 was created 2016-04-30T11:18:25.796Z
    sample = 175928847299117063
    moment = NOW.fromtimestamp(((sample >> 22) + 1420070400000) / 1000, tz=NOW.tzinfo)
    assert int(snowflake_at(moment)) >> 22 == sample >> 22


def test_inbox_reads_people_oldest_first_and_skips_bots():
    fake = FakeDiscord()
    guild = fake.add_guild()
    channel = fake.add_channel(guild, "gembot-status")["id"]
    fake.post_as_user(channel, "7", "pause")
    fake.send_message(channel, {"content": "resume"})  # the bot itself: never a command
    fake.post_as_user(channel, "8", "resume", bot=True)  # another bot
    fake.post_as_user(channel, "9", "status")
    inbox = read_inbox(fake, channel, bot_user_id=BOT, after="1", now=NOW)
    assert [(c.action, c.user_id) for c in inbox.commands] == [("pause", "7"), ("status", "9")]
    again = read_inbox(fake, channel, bot_user_id=BOT, after=inbox.newest_id, now=NOW)
    assert again.commands == [] and again.newest_id == inbox.newest_id


def world_with_status():
    world = World()
    # FakeDiscord ids aren't real timestamps: start the command cursor before all of them
    world.state.meta.discord.commands_after = "1"
    return world, world.channel("status")


def status_notes(world, since):
    status = world.channel("status")
    return [p["embeds"][0]["description"] for cid, p in world.discord.sent[since:] if cid == status]


def test_pause_stops_everything_until_resume():
    world, status = world_with_status()
    world.run(NOW, hot_collectors)  # normal scan, sets the command cursor
    alarm = world.channel("alarm")
    alarms_before = sum(1 for cid, _ in world.discord.sent if cid == alarm)

    world.discord.post_as_user(status, "42", f"{ME} pause")
    sent = len(world.discord.sent)
    calls = {"collected": 0}

    def counting(ctx):
        made = hot_collectors(ctx)
        for collector in made.values():
            original = collector.collect

            def collect(original=original):
                calls["collected"] += 1
                return original()

            collector.collect = collect
        return made

    result = world.run(NOW + HALF_HOUR, counting)
    assert result.paused and world.state.meta.paused and world.state.meta.paused_by == "42"
    assert calls["collected"] == 0
    notes = status_notes(world, sent)
    assert len(notes) == 1 and "Paused by <@42>" in notes[0] and f"{ME} resume" in notes[0]

    # later scans while paused: no requests to sources, nothing posted at all
    sent = len(world.discord.sent)
    for i in range(2, 5):
        assert world.run(NOW + i * HALF_HOUR, counting).paused
    assert calls["collected"] == 0 and world.discord.sent[sent:] == []

    world.discord.post_as_user(status, "43", "resume")
    result = world.run(NOW + 5 * HALF_HOUR, counting)
    assert not result.paused and not world.state.meta.paused
    assert calls["collected"] > 0
    assert any("Resumed by <@43>" in n for n in status_notes(world, sent))
    assert sum(1 for cid, _ in world.discord.sent if cid == alarm) >= alarms_before


def test_status_and_repeated_commands_get_honest_answers():
    world, status = world_with_status()
    world.run(NOW, hot_collectors)
    for text in ("status", "pause", "pause", "status"):
        world.discord.post_as_user(status, "42", text)
    sent = len(world.discord.sent)
    world.run(NOW + HALF_HOUR, hot_collectors)
    (note,) = status_notes(world, sent)  # one message with every answer, in order
    lines = note.splitlines()
    assert lines[0].startswith("▶️ Running") and lines[1].startswith("⏸️ Paused by <@42>")
    assert lines[2].startswith("⏸️ Already paused") and lines[3].startswith("⏸️ Paused since <t:")
    world.discord.post_as_user(status, "42", "resume")
    world.discord.post_as_user(status, "42", "resume")
    sent = len(world.discord.sent)
    world.run(NOW + 2 * HALF_HOUR, hot_collectors)
    assert status_notes(world, sent)[0].splitlines()[1] == "▶️ I'm already running."


def test_messages_the_bot_cant_read_get_one_hint_about_message_content_intent():
    world, status = world_with_status()
    world.run(NOW, hot_collectors)
    world.discord.post_as_user(status, "42", "")  # what Discord sends when the intent is off
    sent = len(world.discord.sent)
    world.run(NOW + HALF_HOUR, hot_collectors)
    notes = status_notes(world, sent)
    assert any("Message Content Intent" in n and f"{ME} pause" in n for n in notes)
    world.discord.post_as_user(status, "42", "")
    sent = len(world.discord.sent)
    world.run(NOW + 2 * HALF_HOUR, hot_collectors)
    assert not any("Message Content Intent" in n for n in status_notes(world, sent))  # once a month


def test_a_pause_holds_when_discord_cannot_be_read():
    world, status = world_with_status()
    world.run(NOW, hot_collectors)
    world.discord.post_as_user(status, "42", "pause")
    world.run(NOW + HALF_HOUR, hot_collectors)
    world.discord.fail("get_messages", HttpError("HTTP 503", 503))
    sent = len(world.discord.sent)
    result = world.run(NOW + 2 * HALF_HOUR, hot_collectors)
    assert result.paused and world.discord.sent[sent:] == []
    assert any("could not read commands" in w for w in result.warnings)


def test_a_failed_inbox_read_never_stops_a_normal_scan():
    world, _ = world_with_status()
    world.discord.fail("get_messages", HttpError("HTTP 503", 503))
    result = world.run(NOW, hot_collectors)
    assert not result.paused and result.plan is not None and result.plan.alarms


def test_smoke_and_dry_runs_ignore_a_pause():
    world, status = world_with_status()
    world.run(NOW, hot_collectors)
    world.discord.post_as_user(status, "42", "pause")
    world.run(NOW + HALF_HOUR, hot_collectors)
    result = world.run(NOW + 2 * HALF_HOUR, hot_collectors, post=False)
    assert not result.paused and result.collected > 0
    assert world.state.meta.paused  # still paused for real scans


def test_first_read_only_looks_back_two_hours():
    fake = FakeDiscord()
    channel = fake.add_channel(fake.add_guild(), "gembot-status")["id"]
    fake.post_as_user(channel, "7", "pause")
    inbox = read_inbox(fake, channel, bot_user_id=BOT, after=None, now=NOW)
    # fake ids are tiny, so they all lie before "two hours ago": an old command is not replayed
    assert inbox.commands == [] and inbox.newest_id == snowflake_at(NOW - timedelta(hours=2))


@respx.mock
def test_rest_get_messages_sends_after_and_limit():
    route = respx.get("https://discord.com/api/v10/channels/55/messages").mock(
        return_value=httpx.Response(200, json=[{"id": "9", "content": "pause", "author": {"id": "7"}}])
    )
    client = DiscordClient("token", make_http(), Budget("discord", 5))
    assert client.get_messages("55", after="123", limit=500) == [
        {"id": "9", "content": "pause", "author": {"id": "7"}}
    ]
    params = route.calls.last.request.url.params
    assert params["after"] == "123" and params["limit"] == "100"
