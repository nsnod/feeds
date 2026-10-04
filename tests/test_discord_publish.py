"""build_card and Publisher against the in-memory FakeDiscord."""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest

from gembot.config import Settings
from gembot.discord.fake import FakeDiscord
from gembot.discord.publish import Publisher, PublishError, build_card, mention_label
from gembot.discord.rest import DiscordError
from gembot.http import BudgetExceeded
from gembot.models import Decision, DecisionKind, Features, GemCard, LLMVerdict, SteamInfo
from tests.factories import NOW, make_game, make_mention

FEATURES = Features(velocity=0.8, hype=0.7, cross=0.6, fit=0.5, fresh=1.0)


@pytest.fixture
def fake() -> FakeDiscord:
    fake = FakeDiscord()
    guild = fake.add_guild("Friends")
    for name in ("gem-alarm", "gem-roundup", "gembot-status"):
        fake.add_channel(guild, name)
    return fake


@pytest.fixture
def channels(fake) -> dict[str, str]:
    by_name = {c["name"]: c["id"] for c in fake.channels.values()}
    return {
        "alarm": by_name["gem-alarm"],
        "roundup": by_name["gem-roundup"],
        "status": by_name["gembot-status"],
    }


@pytest.fixture
def slept() -> list[float]:
    return []


@pytest.fixture
def publisher(fake, channels, slept) -> Publisher:
    return Publisher(fake, channels, Settings(), sleep=slept.append)


def gem(title: str = "Gorilla Pizza Panic", **kw) -> GemCard:
    kw.setdefault("score", 75)
    return GemCard(
        game_id=f"t:{title.lower().replace(' ', '-')}", title=title, reasons=["Big on Reddit"], **kw
    )


# ---------------------------------------------------------------- build_card


def _mentions():
    reddit = make_mention(
        "reddit",
        "r1",
        likes=300,
        comments=40,
        channel="r/IndieDev",
        url="https://www.reddit.com/r/IndieDev/comments/r1/",
    )
    reddit_dup = make_mention(
        "reddit", "r1b", likes=5, channel="r/IndieDev", url="https://reddit.com/r/IndieDev/comments/r1"
    )
    bluesky = make_mention(
        "bluesky",
        "b1",
        likes=50,
        url="https://bsky.app/profile/dev/post/b1",
        media_thumb="https://img/b1.jpg",
    )
    steam = make_mention(
        "steam", "2001", likes=900, url="https://store.steampowered.com/app/2001/Gorilla_Pizza_Panic/"
    )
    youtube = make_mention("youtube", "y1", likes=20, url="https://youtube.com/watch?v=y1")
    rss = make_mention(
        "rss", "f1", likes=10, url="https://curator.example/post", extra={"feed_name": "Indie Curator"}
    )
    itch = make_mention("itch", "dev/gpp", likes=1, url="https://dev.itch.io/gpp")
    return {m.key: m for m in (reddit, reddit_dup, bluesky, steam, youtube, rss, itch)}


def test_build_card_orders_dedupes_and_labels_links():
    mentions = _mentions()
    game = make_game(
        "steam:2001",
        "Gorilla Pizza Panic",
        steam_appid=2001,
        itch_url="https://dev.itch.io/gpp/",
        mention_keys=list(mentions),
    )
    card = build_card(game, mentions, score=77.0, reasons=["a", "b"])
    assert card.url == "https://store.steampowered.com/app/2001/"
    assert card.links == [
        ("Steam", "https://store.steampowered.com/app/2001/"),
        ("itch.io", "https://dev.itch.io/gpp/"),
        ("Reddit r/IndieDev", "https://www.reddit.com/r/IndieDev/comments/r1/"),
        ("Bluesky", "https://bsky.app/profile/dev/post/b1"),
        ("YouTube", "https://youtube.com/watch?v=y1"),
    ]
    assert card.score == 77.0 and card.reasons == ["a", "b"]
    assert card.first_seen == game.first_seen
    assert not (card.escalated or card.would_have_alarmed or card.heating_up or card.test)

    more = build_card(game, mentions, score=1, reasons=[], max_links=10)
    assert [label for label, _ in more.links][-1] == "Indie Curator"
    assert len(more.links) == 6  # steam + itch mentions and the reddit duplicate were folded in


def test_build_card_without_hard_ids_uses_best_post_and_mention_thumb():
    mentions = _mentions()
    keys = [k for k in mentions if not k.startswith(("steam", "itch"))]
    game = make_game("t:gpp", "Gorilla Pizza Panic", mention_keys=[*keys, "reddit:missing"])
    card = build_card(game, mentions, score=50, reasons=[], max_links=2)
    assert card.url == "https://www.reddit.com/r/IndieDev/comments/r1/"
    assert card.links[0][0] == "Reddit r/IndieDev" and len(card.links) == 2
    assert card.thumb == "https://img/b1.jpg"
    assert card.pitch is None


def test_build_card_pitch_thumb_title_and_flags():
    steam = SteamInfo(
        appid=7,
        name="Steam Name",
        short_description="A &quot;chaotic&quot; <b>co-op</b> game " + "with friends " * 30,
        header_image="https://cdn/steam/7.jpg",
    )
    game = make_game("steam:7", "", steam=steam, steam_appid=7)
    decision = Decision(
        game_id="steam:7", kind=DecisionKind.ROUNDUP, escalated=True, would_have_alarmed=True, heating_up=True
    )
    card = build_card(game, {}, score=60, reasons=[], decision=decision)
    assert card.title == "Steam Name"
    assert card.thumb == "https://cdn/steam/7.jpg"
    assert card.pitch.startswith('A "chaotic" co-op game with friends')
    assert len(card.pitch) <= 140 and card.pitch.endswith("…")
    assert card.escalated and card.would_have_alarmed and card.heating_up

    game.llm = LLMVerdict(one_line_pitch="LLM pitch")
    assert build_card(game, {}, score=1, reasons=[]).pitch == "LLM pitch"
    game.pitch = "Own pitch"
    game.thumb = "https://own/thumb.png"
    card = build_card(game, {}, score=1, reasons=[])
    assert card.pitch == "Own pitch" and card.thumb == "https://own/thumb.png"
    assert build_card(make_game("t:z", ""), {}, score=1, reasons=[]).title == "t:z"


def test_mention_label_variants():
    assert mention_label(make_mention("reddit", "1", channel="r/godot")) == "Reddit r/godot"
    assert mention_label(make_mention("reddit", "1")) == "Reddit"
    assert mention_label(make_mention("rss", "1")) == "RSS"
    assert mention_label(make_mention("tiktok", "1")) == "TikTok"
    assert mention_label(make_mention("mastodon", "1")) == "Mastodon"


# ---------------------------------------------------------------- alarms


def test_post_alarm_sends_reacts_and_records(fake, channels, publisher):
    posted = publisher.post_alarm(gem(), FEATURES, NOW)
    assert fake.sent[0][0] == channels["alarm"]
    payload = fake.sent[0][1]
    assert payload["embeds"][0]["title"] == "🚨 GEM ALARM: Gorilla Pizza Panic"
    assert payload["allowed_mentions"] == {"parse": []}
    assert posted.kind == "alarm" and posted.channel_id == channels["alarm"]
    assert posted.posted_at == NOW
    assert [(e.game_id, e.score, e.features) for e in posted.entries] == [
        ("t:gorilla-pizza-panic", 75, FEATURES)
    ]
    reactions = fake.messages[posted.message_id]["reactions"]
    assert list(reactions) == ["👍", "👎"]
    assert all(users[0]["id"] == "1000" for users in reactions.values())


def test_post_alarm_with_role_ping(fake, channels):
    publisher = Publisher(fake, channels, Settings(), ping_role_id="4242", sleep=lambda _: None)
    publisher.post_alarm(gem(escalated=True), FEATURES, NOW)
    payload = fake.sent[0][1]
    assert payload["content"].startswith("<@&4242> 📈 Escalated")
    assert payload["allowed_mentions"] == {"parse": [], "roles": ["4242"]}


def test_test_alarm_is_recorded_as_test_without_entries_or_ping(fake, channels):
    publisher = Publisher(fake, channels, Settings(), ping_role_id="4242", sleep=lambda _: None)
    posted = publisher.post_alarm(gem(test=True), FEATURES, NOW)
    assert posted.kind == "test" and posted.entries == []
    assert "<@&" not in fake.sent[0][1]["content"]
    assert fake.sent[0][1]["allowed_mentions"] == {"parse": []}


def test_reaction_failure_never_loses_the_posted_message(fake, publisher, caplog):
    fake.fail("add_reaction", DiscordError("Missing Permissions", 403, code=50013), times=1)
    with caplog.at_level(logging.WARNING):
        posted = publisher.post_alarm(gem(), FEATURES, NOW)
    assert posted.kind == "alarm" and posted.entries
    assert list(fake.messages[posted.message_id]["reactions"]) == ["👎"]  # 👍 failed, 👎 still added
    assert "could not add 👍" in caplog.text

    fake.fail("add_reaction", RuntimeError("weird"))
    assert publisher.post_alarm(gem("Other"), FEATURES, NOW).entries


def test_send_failure_propagates_and_missing_channel_is_explained(fake, channels):
    fake.fail("send_message", DiscordError("Missing Access", 403, code=50001))
    with pytest.raises(DiscordError):
        Publisher(fake, channels, Settings()).post_alarm(gem(), FEATURES, NOW)
    with pytest.raises(PublishError, match="Setup GemBot"):
        Publisher(fake, {}, Settings()).post_alarm(gem(), FEATURES, NOW)


# ---------------------------------------------------------------- roundups


def test_roundup_posts_header_then_one_message_per_game(fake, channels, publisher, slept):
    cards = [(gem("Alpha", score=70, would_have_alarmed=True), FEATURES), (gem("Beta", score=55), Features())]
    posted = publisher.post_roundup(cards, NOW)
    assert [p.kind for p in posted] == ["roundup_header", "roundup", "roundup"]
    assert posted[0].entries == []
    assert [p.entries[0].game_id for p in posted[1:]] == ["t:alpha", "t:beta"]
    assert all(cid == channels["roundup"] for cid, _ in fake.sent)
    assert fake.sent[0][1]["content"].startswith("📋 **Gem Roundup — Sat 3 Oct, 12:00 UTC**\n2 games")
    assert fake.sent[1][1]["embeds"][0]["title"] == "⏰ Would have alarmed · Alpha"
    assert (
        "reactions" not in fake.messages[posted[0].message_id]
        or not fake.messages[posted[0].message_id]["reactions"]
    )
    for message in posted[1:]:
        assert list(fake.messages[message.message_id]["reactions"]) == ["👍", "👎"]
    assert slept == [0.5, 0.5]


def test_empty_roundup_posts_nothing(fake, publisher):
    assert publisher.post_roundup([], NOW) == []
    assert fake.sent == [] and fake.calls == []


def test_roundup_entry_failure_is_isolated_and_budget_stops(fake, channels):
    class Flaky(FakeDiscord):
        def send_message(self, channel_id, payload):
            title = (payload.get("embeds") or [{}])[0].get("title", "")
            if title == "Broken":
                raise DiscordError("Invalid Form Body", 400, code=50035)
            if title == "Over budget":
                raise BudgetExceeded("discord: request budget used up")
            return super().send_message(channel_id, payload)

    flaky = Flaky()
    flaky.guilds, flaky.channels = fake.guilds, fake.channels
    publisher = Publisher(flaky, channels, Settings(), sleep=lambda _: None, message_delay_s=0)
    cards = [(gem(t), Features()) for t in ("Alpha", "Broken", "Gamma", "Over budget", "Never sent")]
    posted = publisher.post_roundup(cards, NOW)
    assert [p.kind for p in posted] == ["roundup_header", "roundup", "roundup"]
    assert [p.entries[0].game_id for p in posted[1:]] == ["t:alpha", "t:gamma"]
    assert len(flaky.sent) == 3


def test_roundup_header_failure_propagates(fake, publisher):
    fake.fail("send_message", DiscordError("Missing Access", 403, code=50001))
    with pytest.raises(DiscordError):
        publisher.post_roundup([(gem(), FEATURES)], NOW)


def test_custom_emojis_from_settings(fake, channels):
    settings = Settings.model_validate({"feedback": {"up_emoji": "🔥", "down_emoji": "💤"}})
    publisher = Publisher(fake, channels, settings, sleep=lambda _: None)
    posted = publisher.post_roundup([(gem(), FEATURES)], NOW)
    assert "React 🔥/💤" in fake.sent[0][1]["content"]
    assert list(fake.messages[posted[1].message_id]["reactions"]) == ["🔥", "💤"]


# ---------------------------------------------------------------- status


def test_post_status(fake, channels, publisher):
    assert publisher.post_status([], NOW) is None
    assert publisher.post_status(["", "   "], NOW) is None
    posted = publisher.post_status(["🔴 Reddit is failing"], NOW + timedelta(minutes=5))
    assert posted.kind == "status" and posted.channel_id == channels["status"]
    assert posted.posted_at == NOW + timedelta(minutes=5)
    assert fake.sent[-1][1]["embeds"][0]["description"] == "🔴 Reddit is failing"
    assert fake.messages[posted.message_id]["reactions"] == {}
