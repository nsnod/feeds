"""Reading 👍/👎 reactions and folding them into the weights (FakeDiscord, no network).

The weight maths belongs to ``gembot.scoring.weights`` (another module); these tests swap in
a small stand-in that follows the spec formula so the Discord side is tested on its own.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from gembot.config import FeedbackSettings, Settings
from gembot.discord import feedback as fb
from gembot.discord.fake import FakeDiscord
from gembot.discord.publish import Publisher
from gembot.discord.rest import DiscordError
from gembot.http import Budget, HttpError
from gembot.models import Features, GemCard, LabelExample, PostedMessage, PostedState, WeightsState
from tests.factories import NOW

DEFAULTS = Settings().weights
SETTINGS = FeedbackSettings()
HYPE = Features(hype=1.0, velocity=0.2, fresh=0.4)
POSTED_AT = NOW - timedelta(hours=1)


# ---------------------------------------------------------------- stand-in for scoring.weights


def _normalize(weights):
    total = sum(weights.values())
    return {k: v / total for k, v in weights.items()}


class StandInWeights:
    def __init__(self):
        self.updates: list[tuple[float, Features]] = []
        self.described: list[tuple[dict, dict, list]] = []
        self.note: str | None = "hype weight went from 0.15 → 0.19"

    @staticmethod
    def current_weights(state_weights: WeightsState, defaults):
        return _normalize(dict(state_weights.current or defaults))

    def update_weights(self, weights, features, label, settings):
        self.updates.append((label, features))
        values = features.as_dict()
        mean = sum(values.values()) / len(values)
        new = {
            k: min(
                max(w * (1 + settings.learning_rate * label * (values[k] - mean)), settings.weight_min),
                settings.weight_max,
            )
            for k, w in weights.items()
        }
        return _normalize(new)

    def describe_change(self, before, after, labels):
        self.described.append((dict(before), dict(after), list(labels)))
        return self.note


@pytest.fixture
def wmod(monkeypatch) -> StandInWeights:
    stand_in = StandInWeights()
    monkeypatch.setattr(fb, "_weights_module", lambda: stand_in)
    return stand_in


# ---------------------------------------------------------------- helpers


@pytest.fixture
def fake() -> FakeDiscord:
    fake = FakeDiscord()
    guild = fake.add_guild()
    for name in ("gem-alarm", "gem-roundup", "gembot-status"):
        fake.add_channel(guild, name)
    return fake


@pytest.fixture
def channels(fake):
    ids = [c["id"] for c in fake.channels.values()]
    return dict(zip(("alarm", "roundup", "status"), ids, strict=True))


@pytest.fixture
def publisher(fake, channels):
    return Publisher(fake, channels, Settings(), sleep=lambda _: None)


def post_alarm(publisher: Publisher, posted: PostedState, title="Hype Game", at=POSTED_AT, features=HYPE):
    card = GemCard(game_id=f"t:{title.lower().replace(' ', '-')}", title=title, score=80)
    message = publisher.post_alarm(card, features, at)
    posted.messages[message.message_id] = message
    return message


def collect(fake, posted, *, now=NOW, budget=None, settings=SETTINGS):
    return fb.collect_reactions(
        fake,
        posted,
        now=now,
        settings=settings,
        budget=budget or Budget("discord_feedback", 40),
        bot_user_id="1000",
    )


# ---------------------------------------------------------------- collect_reactions


def test_human_thumbs_up_is_counted_and_bot_reactions_are_not(fake, publisher):
    posted = PostedState()
    msg = post_alarm(publisher, posted)
    fake.react(msg.channel_id, msg.message_id, "👍", "7")
    fake.react(msg.channel_id, msg.message_id, "👍", "8")
    fake.react(msg.channel_id, msg.message_id, "👎", "2000", bot=True)  # another bot
    fake.react(msg.channel_id, msg.message_id, "🎉", "9")  # unrelated emoji

    assert collect(fake, posted) == {msg.message_id: (2, 0)}
    assert msg.reactions_checked_at == NOW and (msg.last_up, msg.last_down) == (2, 0)


def test_own_bot_user_is_ignored_by_id_even_without_bot_flag():
    fake = FakeDiscord(bot_user={"id": "1000", "username": "GemBot"})  # no "bot": True
    guild = fake.add_guild()
    channel = fake.add_channel(guild, "gem-alarm")["id"]
    publisher = Publisher(fake, {"alarm": channel}, Settings(), sleep=lambda _: None)
    posted = PostedState()
    msg = post_alarm(publisher, posted)
    assert "bot" not in fake.get_reaction_users(channel, msg.message_id, "👍")[0]
    fake.react(channel, msg.message_id, "👍", "7")
    assert collect(fake, posted) == {msg.message_id: (1, 0)}


def test_only_bot_reactions_never_fetch_user_lists(fake, publisher):
    posted = PostedState()
    msg = post_alarm(publisher, posted)
    assert collect(fake, posted) == {msg.message_id: (0, 0)}
    assert [c[0] for c in fake.calls if c[0].startswith("get_")] == ["get_message"]


def test_skin_tones_count_as_the_same_vote_once_per_person(fake, publisher):
    posted = PostedState()
    msg = post_alarm(publisher, posted)
    fake.react(msg.channel_id, msg.message_id, "👍🏽", "7")
    fake.react(msg.channel_id, msg.message_id, "👍", "7")
    fake.react(msg.channel_id, msg.message_id, "👎🏿", "8")
    assert collect(fake, posted) == {msg.message_id: (1, 1)}


def test_custom_emoji_settings_are_matched_by_name_and_id():
    settings = FeedbackSettings(up_emoji="gem:42", down_emoji="👎")
    api = SimpleNamespace(
        get_message=lambda c, m: {
            "reactions": [{"emoji": {"id": "42", "name": "gem"}, "count": 1, "me": False}]
        },
        get_reaction_users=lambda c, m, e, limit=100: [{"id": "7"}] if e == "gem:42" else [],
    )
    message = PostedMessage(
        message_id="1",
        channel_id="2",
        kind="alarm",
        posted_at=POSTED_AT,
        entries=[{"game_id": "g", "score": 1}],
    )
    assert fb.count_human_reactions(
        api, message, settings=settings, budget=Budget("b", 5), bot_user_id=None
    ) == (1, 0)


def test_only_recent_alarm_and_roundup_messages_are_read(fake, publisher, channels):
    posted = PostedState()
    fresh = post_alarm(publisher, posted, "Fresh")
    old = post_alarm(publisher, posted, "Old", at=NOW - timedelta(days=8))
    just_posted = post_alarm(publisher, posted, "Just posted", at=NOW - timedelta(minutes=5))
    for message in publisher.post_roundup([(GemCard(game_id="t:r", title="R"), HYPE)], POSTED_AT):
        posted.messages[message.message_id] = message
    status = publisher.post_status(["note"], POSTED_AT)
    posted.messages[status.message_id] = status
    test = publisher.post_alarm(GemCard(game_id="t:test", title="T", test=True), HYPE, POSTED_AT)
    posted.messages[test.message_id] = test

    result = collect(fake, posted)
    roundup_entry = next(m for m in posted.messages.values() if m.kind == "roundup")
    assert set(result) == {fresh.message_id, roundup_entry.message_id}
    assert old.reactions_checked_at is None and just_posted.reactions_checked_at is None


def test_recheck_window_and_oldest_checked_first(fake, publisher):
    posted = PostedState()
    a = post_alarm(publisher, posted, "A")
    b = post_alarm(publisher, posted, "B")
    a.reactions_checked_at = NOW - timedelta(minutes=10)  # too recent
    b.reactions_checked_at = NOW - timedelta(minutes=30)
    assert set(collect(fake, posted)) == {b.message_id}

    c = post_alarm(publisher, posted, "C")
    a.reactions_checked_at = NOW - timedelta(hours=3)
    b.reactions_checked_at = NOW - timedelta(minutes=30)
    c.reactions_checked_at = NOW - timedelta(hours=2)
    # budget for exactly one get_message: the least recently checked message wins
    assert set(collect(fake, posted, budget=Budget("f", 1))) == {a.message_id}


def test_budget_stops_reading_and_leaves_the_rest_for_later(fake, publisher):
    posted = PostedState()
    messages = [post_alarm(publisher, posted, f"G{i}", at=POSTED_AT - timedelta(minutes=i)) for i in range(3)]
    for message in messages:
        fake.react(message.channel_id, message.message_id, "👍", "7")
    budget = Budget("discord_feedback", 3)  # each message needs get_message + one user list
    result = collect(fake, posted, budget=budget)
    assert len(result) == 1 and budget.exhausted
    assert sum(1 for m in messages if m.reactions_checked_at is None) == 2


def test_deleted_and_failing_messages(fake, publisher):
    posted = PostedState()
    gone = post_alarm(publisher, posted, "Gone")
    del fake.messages[gone.message_id]
    broken = post_alarm(publisher, posted, "Broken")
    ok = post_alarm(publisher, posted, "Ok")

    real_get = fake.get_message

    def get_message(channel_id, message_id):
        if message_id == broken.message_id:
            raise HttpError("discord: HTTP 502", 502)
        return real_get(channel_id, message_id)

    fake.get_message = get_message
    assert set(collect(fake, posted)) == {ok.message_id}
    assert gone.reactions_checked_at == NOW  # deleted: do not retry it every run
    assert broken.reactions_checked_at is None  # transient: retry next run

    fake.get_message = lambda c, m: (_ for _ in ()).throw(DiscordError("Missing Access", 403, code=50001))
    for message in posted.messages.values():
        message.reactions_checked_at = None
    assert collect(fake, posted) == {}
    assert all(m.reactions_checked_at is None for m in posted.messages.values())


def test_disabled_feedback_reads_and_learns_nothing(fake, publisher, wmod):
    posted = PostedState()
    msg = post_alarm(publisher, posted)
    off = FeedbackSettings(enabled=False)
    assert collect(fake, posted, settings=off) == {}
    weights = WeightsState()
    assert (
        fb.apply_feedback(posted, weights, {msg.message_id: (1, 0)}, now=NOW, defaults=DEFAULTS, settings=off)
        == []
    )
    assert fb.maybe_weekly_note(weights, [], now=NOW, settings=off, defaults=DEFAULTS) is None
    assert wmod.updates == []


# ---------------------------------------------------------------- apply_feedback


def test_thumbs_up_raises_hype_weight_and_records_label(fake, publisher, wmod):
    posted = PostedState()
    msg = post_alarm(publisher, posted)
    fake.react(msg.channel_id, msg.message_id, "👍", "7")
    weights = WeightsState()

    labels = fb.apply_feedback(
        posted, weights, collect(fake, posted), now=NOW, defaults=DEFAULTS, settings=SETTINGS
    )
    assert [(x.message_id, x.game_id, x.label, x.up, x.down, x.score) for x in labels] == [
        (msg.message_id, "t:hype-game", 1.0, 1, 0, 80)
    ]
    assert labels[0].features == HYPE and labels[0].at == NOW
    assert wmod.updates == [(1.0, HYPE)]
    assert weights.current["hype"] > DEFAULTS["hype"]
    assert sum(weights.current.values()) == pytest.approx(1.0)
    assert all(SETTINGS.weight_min <= w <= SETTINGS.weight_max for w in weights.current.values())
    assert len(weights.history) == 1 and weights.history[0].weights == weights.current
    assert weights.history[0].reason == "1 new label(s): 1 👍, 0 👎"
    assert posted.messages[msg.message_id].entries[0].applied_label == 1.0


def test_rereading_the_same_reactions_does_not_reapply(fake, publisher, wmod):
    posted = PostedState()
    msg = post_alarm(publisher, posted)
    fake.react(msg.channel_id, msg.message_id, "👍", "7")
    fake.react(msg.channel_id, msg.message_id, "👍", "8")  # 2 up still clamps to +1
    weights = WeightsState()
    fb.apply_feedback(posted, weights, collect(fake, posted), now=NOW, defaults=DEFAULTS, settings=SETTINGS)
    after_first = dict(weights.current)

    later = NOW + timedelta(hours=1)
    again = fb.apply_feedback(
        posted, weights, collect(fake, posted, now=later), now=later, defaults=DEFAULTS, settings=SETTINGS
    )
    assert again == [] and weights.current == after_first and len(wmod.updates) == 1
    assert len(weights.history) == 1


def test_switching_vote_applies_only_the_difference(fake, publisher, wmod):
    posted = PostedState()
    msg = post_alarm(publisher, posted)
    fake.react(msg.channel_id, msg.message_id, "👍", "7")
    weights = WeightsState()
    fb.apply_feedback(posted, weights, collect(fake, posted), now=NOW, defaults=DEFAULTS, settings=SETTINGS)

    fake.unreact(msg.channel_id, msg.message_id, "👍", "7")
    fake.react(msg.channel_id, msg.message_id, "👎", "7")
    later = NOW + timedelta(hours=1)
    labels = fb.apply_feedback(
        posted, weights, collect(fake, posted, now=later), now=later, defaults=DEFAULTS, settings=SETTINGS
    )
    assert [u[0] for u in wmod.updates] == [1.0, -2.0]
    assert [(x.label, x.up, x.down) for x in labels] == [(-1.0, 0, 1)]
    assert len(weights.history) == 2

    fake.unreact(msg.channel_id, msg.message_id, "👎", "7")  # vote removed -> label 0, delta +1
    latest = later + timedelta(hours=1)
    labels = fb.apply_feedback(
        posted, weights, collect(fake, posted, now=latest), now=latest, defaults=DEFAULTS, settings=SETTINGS
    )
    assert [u[0] for u in wmod.updates][-1] == 1.0 and labels[0].label == 0.0


def test_mixed_votes_cancel_out_and_unknown_messages_are_skipped(fake, publisher, wmod):
    posted = PostedState()
    msg = post_alarm(publisher, posted)
    status = PostedMessage(message_id="s1", channel_id="x", kind="status", posted_at=POSTED_AT)
    posted.messages["s1"] = status
    weights = WeightsState()
    labels = fb.apply_feedback(
        posted,
        weights,
        {msg.message_id: (1, 1), "s1": (3, 0), "ghost": (1, 0)},
        now=NOW,
        defaults=DEFAULTS,
        settings=SETTINGS,
    )
    assert labels == [] and wmod.updates == [] and weights.current == {} and weights.history == []


def test_label_without_weight_change_is_still_recorded(fake, publisher, wmod):
    posted = PostedState()
    flat = Features()  # every feature equals the mean -> the formula changes nothing
    msg = post_alarm(publisher, posted, features=flat)
    weights = WeightsState()
    labels = fb.apply_feedback(
        posted, weights, {msg.message_id: (0, 2)}, now=NOW, defaults=DEFAULTS, settings=SETTINGS
    )
    assert [x.label for x in labels] == [-1.0]
    assert weights.current == {} and weights.history == []


def test_history_is_capped(fake, publisher, wmod, monkeypatch):
    monkeypatch.setattr(fb, "MAX_HISTORY", 2)
    posted = PostedState()
    weights = WeightsState()
    for i in range(4):
        msg = post_alarm(publisher, posted, f"G{i}")
        fb.apply_feedback(
            posted,
            weights,
            {msg.message_id: (1, 0)},
            now=NOW + timedelta(minutes=i),
            defaults=DEFAULTS,
            settings=SETTINGS,
        )
    assert len(weights.history) == 2 and weights.history[-1].at == NOW + timedelta(minutes=3)


def test_label_for_clamps():
    assert fb.label_for(5, 0) == 1.0 and fb.label_for(0, 3) == -1.0 and fb.label_for(2, 2) == 0.0


# ---------------------------------------------------------------- weekly note


def _label(at, label=1.0):
    return LabelExample(at=at, message_id="m", game_id="g", label=label)


def test_weekly_note_cadence(wmod):
    weights = WeightsState()
    wmod.note = None
    assert fb.maybe_weekly_note(weights, [], now=NOW, settings=SETTINGS, defaults=DEFAULTS) is None
    assert weights.last_weekly_note_at == NOW  # still advanced: no re-check every run
    assert wmod.described[0][0] == DEFAULTS and weights.weights_at_last_note == pytest.approx(DEFAULTS)

    soon = NOW + timedelta(days=3)
    assert fb.maybe_weekly_note(weights, [], now=soon, settings=SETTINGS, defaults=DEFAULTS) is None
    assert len(wmod.described) == 1 and weights.last_weekly_note_at == NOW

    weights.current = {**DEFAULTS, "hype": 0.19}
    wmod.note = "You've been 👍-ing games with strong comment hype; hype weight went from 0.15 → 0.19"
    week = NOW + timedelta(days=7)
    labels = [
        _label(NOW - timedelta(days=1)),
        _label(NOW + timedelta(days=2)),
        _label(NOW + timedelta(days=6), -1.0),
    ]
    note = fb.maybe_weekly_note(weights, labels, now=week, settings=SETTINGS, defaults=DEFAULTS)
    assert note == wmod.note
    before, after, recent = wmod.described[-1]
    assert before == pytest.approx(DEFAULTS) and after["hype"] > DEFAULTS["hype"]
    assert [x.at for x in recent] == [NOW + timedelta(days=2), NOW + timedelta(days=6)]
    assert weights.last_weekly_note_at == week and weights.weights_at_last_note == pytest.approx(after)


# ---------------------------------------------------------------- with the real scoring module


def test_real_weights_module_integration(fake, publisher):
    real = pytest.importorskip("gembot.scoring.weights")
    assert fb._weights_module() is real
    posted = PostedState()
    msg = post_alarm(publisher, posted)
    fake.react(msg.channel_id, msg.message_id, "👍", "7")
    weights = WeightsState()
    fb.apply_feedback(posted, weights, collect(fake, posted), now=NOW, defaults=DEFAULTS, settings=SETTINGS)
    assert weights.current["hype"] > DEFAULTS["hype"]
    assert sum(weights.current.values()) == pytest.approx(1.0)
    assert all(SETTINGS.weight_min <= w <= SETTINGS.weight_max for w in weights.current.values())
