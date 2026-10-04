"""Tests for gembot/state/store.py: state files, pruning, and the bot-state git push."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from types import NoneType
from typing import Any, get_args

import pytest
from pydantic import BaseModel

from gembot.config import StateSettings
from gembot.models import (
    BaselineSample,
    CommentSignals,
    DiscordMeta,
    Features,
    GamePostState,
    HttpCacheEntry,
    LabelExample,
    LLMVerdict,
    Mention,
    Meta,
    PostedEntry,
    PostedMessage,
    Snapshot,
    SourceHealth,
    State,
    SteamInfo,
    WeightChange,
    WeightsState,
)
from gembot.state import (
    PRUNE_KEYS,
    SCHEMA_VERSION,
    STATE_FILES,
    GitStateRepo,
    StateStore,
    ensure_state_branch,
    estimate_size,
    prune,
    serialize_state,
)
from gembot.state import store as store_module
from tests.factories import NOW, make_comments, make_game, make_mention

SETTINGS = StateSettings()
WEIGHTS = {
    "velocity": 0.25,
    "underdog": 0.15,
    "cross": 0.15,
    "fit": 0.15,
    "hype": 0.15,
    "meme": 0.05,
    "fresh": 0.1,
}

# Which State fields each file holds (for the "one corrupt file" tests).
FILE_FIELDS = {
    "games.json": ("games", "mentions"),
    "seen.json": ("seen",),
    "posted.json": ("posted",),
    "baselines.json": ("baselines",),
    "weights.json": ("weights",),
    "labels.jsonl": ("labels",),
    "meta.json": ("meta",),
}


def full_state() -> State:
    """A State with every persisted record type and (nearly) every field populated."""
    features = Features(velocity=0.8, underdog=0.6, cross=0.6, fit=0.9, hype=0.7, meme=0.4, fresh=1.0)
    signals = CommentSignals(
        sampled=40,
        distinct_commenters=30,
        intent_comments=12,
        intent_commenters=11,
        negative_commenters=1,
        roblox_comments=5,
        roblox_commenters=5,
        post_comment_count=44,
        intent_examples=["wishlisted!", "me and the boys 🦍"],
        negative_terms=["asset flip"],
        roblox_examples=["roblox clone lol"],
    )
    steam = SteamInfo(
        appid=123,
        name="Gorilla Pizza Panic",
        type="game",
        developers=["Tiny Dev"],
        publishers=["Tiny Dev"],
        category_ids=[1, 9, 38],
        categories=["Multi-player", "Co-op", "Online Co-op"],
        genre_ids=[23],
        genres=["Indie"],
        tags=["Co-op", "Physics"],
        release_date_text="14 Oct, 2026",
        release_date=NOW.date() + timedelta(days=11),
        coming_soon=True,
        early_access=True,
        is_free=False,
        price="$4.99",
        header_image="https://cdn.example/header.jpg",
        short_description="Pizza chaos with friends ☃",
        followers=1500,
        fetched_at=NOW - timedelta(hours=1),
    )
    reddit = make_mention(
        "reddit",
        "t3_abc",
        title="Gorilla Pizza Panic — announce trailer 🍕",
        text="My co-op game about gorillas delivering pizza",
        audience=120_000,
        likes=312,
        comments=44,
        shares=3,
        links=["https://store.steampowered.com/app/123/"],
        channel="r/IndieDev",
        hours_ago=3,
        media_thumb="https://thumb.example/a.jpg",
        raw_tags=["Video"],
        first_seen=NOW - timedelta(hours=2),
        observed_at=NOW,
        history=[Snapshot(at=NOW - timedelta(hours=1), likes=100, comments=10), Snapshot(at=NOW, likes=312)],
        comments_fetched_at=NOW,
        signals=signals,
        game_id="steam:123",
        extra={"flair": "Video", "nested": {"a": None, "b": [1, 2]}},
    )
    reddit.engagement.ratio = 0.97
    reddit.comments = make_comments(["take my money", "roblox clone lol"])
    itch = make_mention(
        "itch",
        "dev/pizza",
        title="Pizza Panic",
        channel="itch:new-and-popular",
        hours_ago=20,
        rank=4,
        list_size=30,
        history=[Snapshot(at=NOW, rank=4, followers=10)],
        game_id="itch:dev/pizza",
        extra={"feed_name": "new-and-popular"},
    )
    game = make_game(
        "steam:123",
        "Gorilla Pizza Panic",
        aliases=["GPP"],
        developer="Tiny Dev",
        publisher="Tiny Dev",
        hard_ids=["steam:123"],
        steam_appid=123,
        mention_keys=[reddit.key],
        steam=steam,
        pitch="Gorillas deliver pizza, badly.",
        thumb="https://cdn.example/header.jpg",
        llm=LLMVerdict(
            game_title="Gorilla Pizza Panic", is_a_specific_game=True, friendslop_fit=0.9, one_line_pitch="x"
        ),
        last_score=81.5,
        last_scored_at=NOW,
        last_features=features,
        last_reasons=["312 upvotes in 3h on r/IndieDev (9× normal for that sub)"],
    )
    other = make_game(
        "itch:dev/pizza",
        "Pizza Panic",
        itch_url="https://dev.itch.io/pizza",
        hard_ids=["itch:dev/pizza"],
        mention_keys=[itch.key],
        excluded_reason="blocklisted publisher",
    )
    return State(
        games={game.game_id: game, other.game_id: other},
        mentions={reddit.key: reddit, itch.key: itch},
        seen={reddit.key: NOW - timedelta(hours=2), itch.key: NOW - timedelta(hours=20)},
        posted={
            "messages": {
                "1001": PostedMessage(
                    message_id="1001",
                    channel_id="555",
                    kind="alarm",
                    posted_at=NOW - timedelta(hours=1),
                    entries=[
                        PostedEntry(game_id="steam:123", score=81.5, features=features, applied_label=1.0)
                    ],
                    reactions_checked_at=NOW,
                    last_up=2,
                    last_down=0,
                )
            },
            "games": {
                "steam:123": GamePostState(
                    alarmed_at=NOW - timedelta(hours=1),
                    alarm_score=81.5,
                    roundup_at=NOW - timedelta(hours=5),
                    roundup_score=60.0,
                    would_have_alarmed=True,
                    pending_roundup=False,
                )
            },
        },
        baselines={
            "r/IndieDev": {
                "reddit:t3_old": BaselineSample(at=NOW - timedelta(days=2), eph=3.5),
                "reddit:t3_old2": BaselineSample(at=NOW - timedelta(days=1), eph=1.25),
            }
        },
        weights=WeightsState(
            current=WEIGHTS,
            history=[WeightChange(at=NOW - timedelta(days=1), weights=WEIGHTS, reason="👍 feedback")],
            last_weekly_note_at=NOW - timedelta(days=2),
            weights_at_last_note=WEIGHTS,
        ),
        labels=[
            LabelExample(
                at=NOW - timedelta(hours=1),
                message_id="1001",
                game_id="steam:123",
                label=1.0,
                up=2,
                down=0,
                features=features,
                score=81.5,
            )
        ],
        meta=Meta(
            schema_version=SCHEMA_VERSION,
            last_run_at=NOW,
            last_roundup_at=NOW - timedelta(hours=1),
            run_count=42,
            daily_alarms={"2026-10-03": 2},
            source_health={
                "reddit": SourceHealth(
                    consecutive_failures=2,
                    last_ok_at=NOW - timedelta(hours=2),
                    last_error_at=NOW,
                    last_error="HTTP 403",
                    alerted_broken=False,
                )
            },
            discord=DiscordMeta(
                guild_id="1",
                category_id="2",
                channels={"alarm": "3", "roundup": "4", "status": "5"},
                bot_user_id="9",
                welcome_message_id="10",
            ),
            http_cache={"https://itch.io/feed.xml": HttpCacheEntry(etag='"v1"', last_modified="Sat", at=NOW)},
            game_aliases={"t:pizza-000000": "steam:123"},
            reddit_baseline_ready=True,
        ),
    )


def without_comments(state: State) -> State:
    expected = state.model_copy(deep=True)
    for mention in expected.mentions.values():
        mention.comments = []
    return expected


def saved(tmp_path: Path, state: State | None = None) -> StateStore:
    store = StateStore(tmp_path / "state")
    store.save(state or full_state())
    return store


# ======================================================================== load / save


def test_round_trip_of_a_fully_populated_state(tmp_path):
    state = full_state()
    store = saved(tmp_path, state)
    assert sorted(p.name for p in store.root.iterdir()) == sorted(STATE_FILES)
    loaded = store.load()
    assert loaded.model_dump() == without_comments(state).model_dump()
    # Every part really came from its file.
    for name, fields in FILE_FIELDS.items():
        for field in fields:
            assert getattr(loaded, field) != getattr(State(), field), (name, field)


def test_comments_are_stripped_on_save_but_signals_are_kept(tmp_path):
    state = full_state()
    store = saved(tmp_path, state)
    raw = (store.root / "games.json").read_text(encoding="utf-8")
    assert "take my money" not in raw
    assert '"comments":[' not in raw  # no comment list at all (engagement.comments is a number)
    assert "roblox clone lol" in raw  # ...but it survives as a signals example
    loaded = store.load().mentions["reddit:t3_abc"]
    assert loaded.comments == []
    assert loaded.signals == state.mentions["reddit:t3_abc"].signals
    # The in-memory state is not modified by saving.
    assert len(state.mentions["reddit:t3_abc"].comments) == 2


def test_missing_directory_and_files_give_defaults(tmp_path):
    store = StateStore(tmp_path / "does-not-exist")
    assert store.load() == State()
    assert store.size_bytes() == 0
    store.root.mkdir()
    (store.root / "seen.json").write_text('{"reddit:1":"2026-10-03T10:00:00Z"}\n', encoding="utf-8")
    loaded = store.load()
    assert list(loaded.seen) == ["reddit:1"]
    assert loaded.games == {} and loaded.meta == Meta()


@pytest.mark.parametrize("name", STATE_FILES)
def test_one_corrupt_file_falls_back_to_defaults_for_that_file_only(tmp_path, caplog, name):
    state = full_state()
    store = saved(tmp_path, state)
    (store.root / name).write_text('{"games": {"broken', encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="gembot.state"):
        loaded = store.load()
    expected = without_comments(state)
    for file_name, fields in FILE_FIELDS.items():
        for field in fields:
            want = getattr(State(), field) if file_name == name else getattr(expected, field)
            assert getattr(loaded, field) == want, (file_name, field)
    assert name in caplog.text


def test_unreadable_bytes_and_wrong_top_level_types_give_defaults(tmp_path, caplog):
    store = saved(tmp_path)
    (store.root / "games.json").write_bytes(b"\xff\xfe\x00garbage")
    (store.root / "labels.jsonl").write_bytes(b"\xff\xfe\x00garbage")
    (store.root / "seen.json").write_text('"not a mapping"\n', encoding="utf-8")
    (store.root / "posted.json").write_text("[1, 2]\n", encoding="utf-8")
    (store.root / "meta.json").write_text("[]\n", encoding="utf-8")
    (store.root / "weights.json").write_text("5\n", encoding="utf-8")
    (store.root / "baselines.json").write_text("null\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="gembot.state"):
        loaded = store.load()
    assert loaded == State()
    for name in ("games.json", "labels.jsonl", "seen.json", "posted.json", "meta.json", "weights.json"):
        assert name in caplog.text


def test_games_json_with_a_non_mapping_top_level(tmp_path, caplog):
    store = saved(tmp_path)
    (store.root / "games.json").write_text("[]\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="gembot.state"):
        loaded = store.load()
    assert loaded.games == {} and loaded.mentions == {}
    assert loaded.meta.run_count == 42
    assert "games.json is not a mapping" in caplog.text


def test_invalid_records_are_skipped_and_the_rest_kept(tmp_path, caplog):
    store = saved(tmp_path)
    games = json.loads((store.root / "games.json").read_text(encoding="utf-8"))
    games["games"]["t:broken"] = {"title": "no id or dates"}
    games["mentions"]["reddit:broken"] = {"source": "reddit"}
    (store.root / "games.json").write_text(json.dumps(games), encoding="utf-8")
    seen = {"reddit:ok": "2026-10-03T10:00:00", "reddit:bad": "yesterday-ish"}
    (store.root / "seen.json").write_text(json.dumps(seen), encoding="utf-8")
    baselines = {
        "r/a": {"k1": {"at": "2026-10-02T00:00:00Z", "eph": 2.0}, "k2": {"eph": "x"}},
        "r/b": ["not", "a", "mapping"],
    }
    (store.root / "baselines.json").write_text(json.dumps(baselines), encoding="utf-8")
    posted = {"messages": {"1": {"message_id": "1"}}, "games": {"g": {"alarmed_at": "2026-10-01T00:00:00Z"}}}
    (store.root / "posted.json").write_text(json.dumps(posted), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="gembot.state"):
        loaded = store.load()
    assert set(loaded.games) == {"steam:123", "itch:dev/pizza"}
    assert set(loaded.mentions) == {"reddit:t3_abc", "itch:dev/pizza"}
    assert list(loaded.seen) == ["reddit:ok"]
    assert loaded.seen["reddit:ok"].tzinfo is not None  # naive timestamps are read as UTC
    assert list(loaded.baselines) == ["r/a"] and list(loaded.baselines["r/a"]) == ["k1"]
    assert loaded.posted.messages == {}
    assert loaded.posted.games["g"].alarmed_at is not None
    for what in (
        "games.json games",
        "games.json mentions",
        "seen.json",
        "baselines.json",
        "posted.json messages",
    ):
        assert what in caplog.text


def test_meta_and_weights_keep_their_valid_fields(tmp_path, caplog):
    store = saved(tmp_path)
    meta = json.loads((store.root / "meta.json").read_text(encoding="utf-8"))
    meta["run_count"] = "lots"
    meta["daily_alarms"] = {"2026-10-03": "two"}
    meta["added_by_a_newer_version"] = True
    (store.root / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    weights = {"current": WEIGHTS, "history": [{"weights": "nope"}]}
    (store.root / "weights.json").write_text(json.dumps(weights), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="gembot.state"):
        loaded = store.load()
    assert loaded.meta.run_count == 0 and loaded.meta.daily_alarms == {}
    assert loaded.meta.last_roundup_at == NOW - timedelta(hours=1)
    assert loaded.meta.discord.guild_id == "1"
    assert loaded.weights.current == WEIGHTS and loaded.weights.history == []
    assert "meta.json" in caplog.text and "run_count" in caplog.text


def test_unknown_keys_are_tolerated(tmp_path):
    store = saved(tmp_path)
    games = json.loads((store.root / "games.json").read_text(encoding="utf-8"))
    games["future_section"] = {"x": 1}
    games["games"]["steam:123"]["future_field"] = [1, 2]
    (store.root / "games.json").write_text(json.dumps(games), encoding="utf-8")
    meta = json.loads((store.root / "meta.json").read_text(encoding="utf-8"))
    meta["new_thing"] = {"a": 1}
    (store.root / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    loaded = store.load()
    assert loaded.games["steam:123"].title == "Gorilla Pizza Panic"
    assert loaded.meta.run_count == 42


def test_labels_jsonl_skips_bad_lines(tmp_path, caplog):
    store = saved(tmp_path)
    good = LabelExample(at=NOW, message_id="m", game_id="g", label=-1.0, down=1)
    lines = [
        good.model_dump_json(),
        "this is not json",
        '{"at": "not a date", "message_id": "m", "game_id": "g", "label": 1}',
        "",
        "   ",
        good.model_copy(update={"message_id": "m2"}).model_dump_json(),
    ]
    (store.root / "labels.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="gembot.state"):
        loaded = store.load()
    assert [label.message_id for label in loaded.labels] == ["m", "m2"]
    assert "skipped 2" in caplog.text


def test_a_crashing_loader_never_crashes_load(tmp_path, monkeypatch, caplog):
    store = saved(tmp_path)

    def boom(self, state):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(StateStore, "_load_seen", boom)
    with caplog.at_level(logging.WARNING, logger="gembot.state"):
        loaded = store.load()
    assert loaded.seen == {}
    assert loaded.meta.run_count == 42
    assert "seen.json" in caplog.text and "unexpected" in caplog.text


def test_save_is_deterministic_and_stable_across_a_reload(tmp_path):
    state = full_state()
    first = saved(tmp_path / "a", state)
    second = saved(tmp_path / "b", state)
    first_bytes = {name: (first.root / name).read_bytes() for name in STATE_FILES}
    assert first_bytes == {name: (second.root / name).read_bytes() for name in STATE_FILES}
    # load -> save writes exactly the same bytes again (no churn in the git history)
    first.save(first.load())
    assert first_bytes == {name: (first.root / name).read_bytes() for name in STATE_FILES}
    # and saving the same store twice leaves no temp files behind
    first.save(state)
    assert sorted(p.name for p in first.root.iterdir()) == sorted(STATE_FILES)


def test_saved_files_are_sorted_compact_utf8_with_one_record_per_line(tmp_path):
    store = saved(tmp_path)
    for name in STATE_FILES:
        text = (store.root / name).read_text(encoding="utf-8")
        assert text.endswith("\n")
        if name.endswith(".json"):
            data = json.loads(text)
            assert text.rstrip("\n").replace("\n", "") == json.dumps(
                data, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
    games_text = (store.root / "games.json").read_text(encoding="utf-8")
    assert "🍕" in games_text and "\\u" not in games_text  # ensure_ascii=False
    lines = games_text.splitlines()
    assert lines[0] == "{" and lines[1] == '"games":{' and lines[-1] == "}"
    assert sum(line.startswith('"steam:123":{') for line in lines) == 1
    labels = (store.root / "labels.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(labels) == 1 and json.loads(labels[0])["message_id"] == "1001"


def test_empty_state_saves_small_valid_files(tmp_path):
    store = saved(tmp_path, State())
    for name in STATE_FILES:
        text = (store.root / name).read_text(encoding="utf-8")
        if name.endswith(".json"):
            json.loads(text)
        else:
            assert text == ""
    assert store.load() == State()


def test_schema_version_is_written_to_meta(tmp_path, caplog):
    state = full_state()
    state.meta.schema_version = 0
    store = saved(tmp_path, state)
    assert (
        json.loads((store.root / "meta.json").read_text(encoding="utf-8"))["schema_version"] == SCHEMA_VERSION
    )
    assert state.meta.schema_version == 0  # save does not mutate the state
    meta = json.loads((store.root / "meta.json").read_text(encoding="utf-8"))
    meta["schema_version"] = SCHEMA_VERSION + 1
    (store.root / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="gembot.state"):
        loaded = store.load()
    assert loaded.meta.run_count == 42
    assert "newer than this GemBot" in caplog.text


def test_failed_write_keeps_the_old_file_and_no_temp_files(tmp_path, monkeypatch):
    store = saved(tmp_path)
    before = {name: (store.root / name).read_bytes() for name in STATE_FILES}
    changed = full_state()
    changed.meta.run_count = 999

    def failing_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(store_module.os, "replace", failing_replace)
    with pytest.raises(OSError, match="disk full"):
        store.save(changed)
    assert {name: (store.root / name).read_bytes() for name in STATE_FILES} == before
    assert sorted(p.name for p in store.root.iterdir()) == sorted(STATE_FILES)


def test_size_bytes_matches_files_and_estimate(tmp_path):
    state = full_state()
    store = saved(tmp_path, state)
    on_disk = sum((store.root / name).stat().st_size for name in STATE_FILES)
    assert store.size_bytes() == on_disk == estimate_size(state)
    assert set(serialize_state(state)) == set(STATE_FILES)


def _models_in(annotation: Any):
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
    for arg in get_args(annotation):
        yield from _models_in(arg)


def test_optional_fields_default_to_none_so_exclude_none_is_lossless():
    """save() drops None values; that is only lossless if every nullable field defaults to None."""
    todo: list[type[BaseModel]] = [State]
    visited: set[type[BaseModel]] = set()
    while todo:
        cls = todo.pop()
        if cls in visited:
            continue
        visited.add(cls)
        for name, field in cls.model_fields.items():
            if NoneType in get_args(field.annotation):
                assert field.default is None, (
                    f"{cls.__name__}.{name} is nullable but defaults to {field.default!r}"
                )
            todo.extend(_models_in(field.annotation))
    assert {Mention, SteamInfo, CommentSignals, PostedEntry, SourceHealth} <= visited


# ======================================================================== prune


def test_prune_returns_every_category_and_keeps_fresh_state():
    state = full_state()
    removed = prune(state, NOW, SETTINGS)
    assert set(removed) == set(PRUNE_KEYS)
    assert all(count == 0 for count in removed.values())
    assert without_comments(state).model_dump() == without_comments(full_state()).model_dump()


def test_prune_games_and_mentions_by_age():
    old = NOW - timedelta(days=31)
    keep_recent = make_mention("reddit", "recent", created_at=NOW - timedelta(days=1), game_id="t:fresh")
    keep_observed = make_mention(
        "reddit", "observed", created_at=NOW - timedelta(days=40), observed_at=NOW - timedelta(days=2)
    )
    keep_first_seen = make_mention(
        "reddit", "first-seen", created_at=NOW - timedelta(days=40), first_seen=NOW - timedelta(days=29)
    )
    boundary = make_mention("reddit", "boundary", created_at=NOW - timedelta(days=30))
    stale = make_mention(
        "reddit", "stale", created_at=NOW - timedelta(days=40), observed_at=NOW - timedelta(days=35)
    )
    of_old_game = make_mention("bluesky", "of-old-game", created_at=NOW - timedelta(days=1), game_id="t:old")
    mentions = [keep_recent, keep_observed, keep_first_seen, boundary, stale, of_old_game]
    fresh = make_game(
        "t:fresh", "Fresh", last_seen=NOW - timedelta(days=1), mention_keys=[m.key for m in mentions[:5]]
    )
    gone = make_game("t:old", "Old", first_seen=old, last_seen=old, mention_keys=[of_old_game.key])
    state = State(games={g.game_id: g for g in (fresh, gone)}, mentions={m.key: m for m in mentions})

    removed = prune(state, NOW, SETTINGS)

    assert removed["games"] == 1 and removed["mentions"] == 2
    assert set(state.games) == {"t:fresh"}
    assert set(state.mentions) == {m.key for m in mentions[:4]}
    assert state.games["t:fresh"].mention_keys == [m.key for m in mentions[:4]]


def test_prune_seen_and_posted_messages():
    state = State(
        seen={
            "reddit:old": NOW - timedelta(days=15),
            "reddit:new": NOW - timedelta(days=13),
        },
        posted={
            "messages": {
                "old": PostedMessage(
                    message_id="old", channel_id="c", kind="roundup", posted_at=NOW - timedelta(days=31)
                ),
                "new": PostedMessage(
                    message_id="new", channel_id="c", kind="alarm", posted_at=NOW - timedelta(days=29)
                ),
            }
        },
    )
    removed = prune(state, NOW, SETTINGS)
    assert removed["seen"] == 1 and list(state.seen) == ["reddit:new"]
    assert removed["posted_messages"] == 1 and list(state.posted.messages) == ["new"]


def test_alarm_memory_outlives_the_game_record():
    days = timedelta(days=1)
    state = State(
        games={"t:alive": make_game("t:alive", "Alive")},
        posted={
            "games": {
                # Game record is gone (pruned after 30 days) but the alarm is remembered.
                "t:alarmed-40d": GamePostState(alarmed_at=NOW - 40 * days, alarm_score=80),
                "t:alarmed-366d": GamePostState(alarmed_at=NOW - 366 * days, alarm_score=80),
                # Newest of alarmed_at / roundup_at counts.
                "t:roundup-recent": GamePostState(alarmed_at=NOW - 400 * days, roundup_at=NOW - 10 * days),
                "t:roundup-old": GamePostState(roundup_at=NOW - 400 * days),
                # Never posted: kept while the game lives or while a carry-over is pending.
                "t:alive": GamePostState(would_have_alarmed=True),
                "t:pending": GamePostState(pending_roundup=True),
                "t:orphan": GamePostState(),
            }
        },
    )
    removed = prune(state, NOW, SETTINGS)
    assert removed["posted_games"] == 3
    assert set(state.posted.games) == {"t:alarmed-40d", "t:roundup-recent", "t:alive", "t:pending"}


def test_prune_baselines_uses_the_baseline_window():
    state = State(
        baselines={
            "r/a": {
                "old": BaselineSample(at=NOW - timedelta(days=4), eph=1),
                "new": BaselineSample(at=NOW - timedelta(days=2), eph=2),
            },
            "r/b": {"old": BaselineSample(at=NOW - timedelta(days=5), eph=1)},
        }
    )
    removed = prune(state, NOW, SETTINGS, baseline_days=3)
    assert removed["baseline_samples"] == 2 and removed["baseline_channels"] == 1
    assert state.baselines == {"r/a": {"new": BaselineSample(at=NOW - timedelta(days=2), eph=2)}}
    # the default window is 14 days
    state.baselines["r/c"] = {"x": BaselineSample(at=NOW - timedelta(days=13), eph=1)}
    assert prune(state, NOW, SETTINGS)["baseline_samples"] == 0


def test_prune_meta_cache_daily_alarms_and_aliases():
    state = State(
        games={"steam:1": make_game("steam:1", "One")},
        meta=Meta(
            http_cache={
                "https://old": HttpCacheEntry(etag="a", at=NOW - timedelta(days=8)),
                "https://new": HttpCacheEntry(etag="b", at=NOW - timedelta(days=6)),
            },
            daily_alarms={"2026-09-25": 3, "2026-09-26": 1, "2026-10-03": 2, "garbage": 9},
            game_aliases={
                "t:a": "steam:1",  # alive
                "t:b": "t:a",  # chain to an alive game
                "t:c": "t:gone",  # target no longer exists
                "t:x": "t:y",  # cycle, no real game
                "t:y": "t:x",
            },
        ),
    )
    removed = prune(state, NOW, SETTINGS)
    assert removed["http_cache"] == 1 and list(state.meta.http_cache) == ["https://new"]
    assert removed["daily_alarms"] == 2 and state.meta.daily_alarms == {"2026-09-26": 1, "2026-10-03": 2}
    assert removed["game_aliases"] == 3 and state.meta.game_aliases == {"t:a": "steam:1", "t:b": "t:a"}


def test_prune_caps_labels_newest_first_and_weight_history():
    labels = [
        LabelExample(at=NOW - timedelta(hours=h), message_id=f"m{h}", game_id="g", label=1.0)
        for h in range(10)
    ]
    history = [
        WeightChange(at=NOW - timedelta(hours=300 - i), weights=WEIGHTS, reason=str(i)) for i in range(250)
    ]
    state = State(labels=list(reversed(labels[5:])) + labels[:5], weights=WeightsState(history=history))
    removed = prune(state, NOW, SETTINGS.model_copy(update={"max_labels": 4}))
    assert removed["labels"] == 6
    assert [label.message_id for label in state.labels] == ["m3", "m2", "m1", "m0"]  # oldest -> newest
    assert removed["weight_history"] == 50
    assert [change.reason for change in state.weights.history] == [str(i) for i in range(50, 250)]
    # max_labels = 0 keeps none
    assert prune(state, NOW, SETTINGS.model_copy(update={"max_labels": 0}))["labels"] == 4
    assert state.labels == []


def _bulky_state(count: int = 20) -> State:
    state = State()
    game = make_game("t:bulky", "Bulky", last_seen=NOW)
    for i in range(count):
        mention = make_mention(
            "reddit",
            f"p{i:02d}",
            text="long post body " * 100,
            created_at=NOW - timedelta(hours=count - i),  # p00 is the oldest
            history=[Snapshot(at=NOW - timedelta(minutes=30 * j), likes=j) for j in range(6)],
            signals=CommentSignals(sampled=50, intent_examples=[f"wishlisted {i} " * 40]),
            game_id=game.game_id,
        )
        mention.comments = make_comments(["c1", "c2"])
        state.mentions[mention.key] = mention
        game.mention_keys.append(mention.key)
    state.games[game.game_id] = game
    state.posted.games[game.game_id] = GamePostState(alarmed_at=NOW, alarm_score=90)
    return state


def test_size_cap_strips_detail_of_the_oldest_mentions_first():
    state = _bulky_state()
    full = estimate_size(state)
    limit = full - 3000  # stripping a few old mentions is enough
    removed = prune(state, NOW, SETTINGS.model_copy(update={"max_bytes": limit}))
    assert estimate_size(state) <= limit
    assert 1 <= removed["size_stripped"] < 20
    assert removed["size_mentions"] == 0 and removed["size_games"] == 0
    oldest, newest = state.mentions["reddit:p00"], state.mentions["reddit:p19"]
    assert oldest.signals is None and len(oldest.history) == 1 and len(oldest.text) == 500
    assert oldest.comments == []
    assert newest.signals is not None and len(newest.history) == 6


def test_size_cap_drops_the_oldest_mentions_and_unlinks_them():
    state = _bulky_state()
    limit = estimate_size(state) // 4
    removed = prune(state, NOW, SETTINGS.model_copy(update={"max_bytes": limit}))
    assert estimate_size(state) <= limit
    assert removed["size_stripped"] == 20
    assert 0 < removed["size_mentions"] < 20 and removed["size_games"] == 0
    assert "reddit:p00" not in state.mentions and "reddit:p19" in state.mentions
    assert state.games["t:bulky"].mention_keys == sorted(state.mentions)


def test_size_cap_drops_only_as_many_games_as_needed():
    state = State()
    for i in range(3):
        game = make_game(f"t:g{i}", "G" * 2000, last_seen=NOW - timedelta(days=3 - i))
        state.games[game.game_id] = game
    bare = make_mention("reddit", "bare", created_at=NOW)  # nothing to strip
    state.mentions[bare.key] = bare
    limit = estimate_size(state) - 1500  # dropping the mention is not enough, one game is
    removed = prune(state, NOW, SETTINGS.model_copy(update={"max_bytes": limit}))
    assert estimate_size(state) <= limit
    assert (removed["size_stripped"], removed["size_mentions"], removed["size_games"]) == (0, 1, 1)
    assert set(state.games) == {"t:g1", "t:g2"}


def test_size_cap_last_resort_drops_games_but_keeps_alarm_memory(caplog):
    state = _bulky_state()
    state.games["t:other"] = make_game("t:other", "Other", last_seen=NOW - timedelta(days=1))
    with caplog.at_level(logging.INFO, logger="gembot.state"):
        removed = prune(state, NOW, SETTINGS.model_copy(update={"max_bytes": 1}))
    assert state.mentions == {} and state.games == {}
    assert removed["size_mentions"] == 20 and removed["size_games"] == 2
    assert state.posted.games["t:bulky"].alarmed_at == NOW
    assert any(r.levelno == logging.WARNING and "size cap" in r.getMessage() for r in caplog.records)


def test_no_double_alarm_across_two_consecutive_simulated_runs(tmp_path):
    store = StateStore(tmp_path / "state")
    gid = "steam:123"

    # Run 1: the game alarms; the run records it and saves.
    run1 = store.load()
    prune(run1, NOW, SETTINGS)
    run1.games[gid] = make_game(gid, "Gorilla Pizza Panic", last_seen=NOW)
    run1.posted.games[gid] = GamePostState(alarmed_at=NOW, alarm_score=81.5)
    run1.meta.daily_alarms[NOW.date().isoformat()] = 1
    store.save(run1)

    # Run 2, 30 minutes later: the alarm is still remembered after load + prune.
    later = NOW + timedelta(minutes=30)
    run2 = store.load()
    prune(run2, later, SETTINGS)
    assert run2.posted.games[gid].alarmed_at == NOW
    assert run2.meta.alarms_on(NOW.date()) == 1
    store.save(run2)

    # 40 days later the Game record is pruned, but "already alarmed" survives a save/load.
    much_later = NOW + timedelta(days=40)
    run3 = store.load()
    prune(run3, much_later, SETTINGS)
    assert gid not in run3.games
    store.save(run3)
    run4 = store.load()
    assert run4.posted.games[gid].alarmed_at == NOW
    # ...until the alarm memory window (365 days) runs out.
    prune(run4, NOW + timedelta(days=366), SETTINGS)
    assert gid not in run4.posted.games


# ======================================================================== git

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=tester", "-c", "user.email=tester@example.com", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def git_env(tmp_path, monkeypatch):
    """Isolate git from the developer's global/system config (identity, signing, hooks)."""
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def remote(tmp_path, git_env) -> tuple[Path, Path]:
    """A local bare 'origin' with a main branch, and a 'seed' clone of it (the caller's checkout)."""
    bare = tmp_path / "remote.git"
    git(tmp_path, "-c", "init.defaultBranch=main", "init", "-q", "--bare", str(bare))
    seed = tmp_path / "seed"
    git(tmp_path, "-c", "init.defaultBranch=main", "clone", "-q", str(bare), str(seed))
    (seed / "app.py").write_text("print('hi')\n", encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "init")
    git(seed, "push", "-q", "origin", "HEAD:refs/heads/main")
    return bare, seed


def clone_state(bare: Path, dest: Path) -> Path:
    git(dest.parent, "clone", "-q", "-b", "bot-state", str(bare), str(dest))
    return dest


@needs_git
def test_ensure_state_branch_creates_an_orphan_once_and_leaves_the_checkout_alone(remote):
    bare, seed = remote
    (seed / "app.py").write_text("work in progress\n", encoding="utf-8")
    (seed / "staged.txt").write_text("staged\n", encoding="utf-8")
    git(seed, "add", "staged.txt")
    (seed / "untracked.txt").write_text("untracked\n", encoding="utf-8")

    def snapshot():
        return (
            git(seed, "status", "--porcelain"),
            git(seed, "rev-parse", "HEAD"),
            git(seed, "branch", "--show-current"),
            git(seed, "ls-files", "-s"),
            git(seed, "branch", "--list"),
        )

    before = snapshot()
    assert ensure_state_branch(seed) is True
    assert ensure_state_branch(seed) is False  # idempotent
    assert snapshot() == before
    assert (seed / "app.py").read_text(encoding="utf-8") == "work in progress\n"

    assert git(bare, "rev-list", "--count", "bot-state") == "1"
    assert git(bare, "ls-tree", "--name-only", "bot-state") == "README.md"
    assert (
        git(bare, "log", "-1", "--format=%an <%ae>", "bot-state")
        == "gembot <gembot@users.noreply.github.com>"
    )
    readme = git(bare, "show", "bot-state:README.md")
    assert "GemBot state" in readme and "do not edit it by hand" in readme
    no_common_history = subprocess.run(
        ["git", "merge-base", "main", "bot-state"], cwd=bare, capture_output=True
    )
    assert no_common_history.returncode == 1  # orphan: shares no history with main


@needs_git
def test_ensure_state_branch_with_an_unreachable_remote_returns_false(tmp_path, git_env, caplog):
    repo = tmp_path / "repo"
    git(tmp_path, "-c", "init.defaultBranch=main", "init", "-q", str(repo))
    git(repo, "remote", "add", "origin", str(tmp_path / "missing.git"))
    with caplog.at_level(logging.WARNING, logger="gembot.state"):
        assert ensure_state_branch(repo) is False
    assert "cannot check" in caplog.text


@needs_git
@pytest.mark.parametrize("failing", ["hash-object", "mktree", "commit-tree", "push"])
def test_ensure_state_branch_failures_return_false(remote, failing, caplog):
    bare, seed = remote

    def run(cmd, **kwargs):
        if failing in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", f"simulated {failing} failure")
        return subprocess.run(cmd, **kwargs)

    with caplog.at_level(logging.WARNING, logger="gembot.state"):
        assert ensure_state_branch(seed, run=run) is False
    assert f"simulated {failing} failure" in caplog.text
    assert subprocess.run(["git", "rev-parse", "--verify", "-q", "bot-state"], cwd=bare).returncode != 0


def _alarm_state(game_id: str, seen_key: str) -> State:
    state = State()
    state.seen[seen_key] = NOW
    state.posted.games[game_id] = GamePostState(alarmed_at=NOW, alarm_score=80)
    return state


@needs_git
def test_push_conflict_fetches_reapplies_and_succeeds(remote, tmp_path):
    bare, seed = remote
    assert ensure_state_branch(seed)
    clone_a = clone_state(bare, tmp_path / "a")
    clone_b = clone_state(bare, tmp_path / "b")  # will be stale once A pushes

    store_a = StateStore(clone_a)
    state_a = _alarm_state("steam:1", "reddit:a")
    store_a.save(state_a)
    assert GitStateRepo(clone_a).commit_and_push("state: run A", lambda: store_a.save(state_a))

    store_b = StateStore(clone_b)
    state_b = _alarm_state("steam:2", "reddit:b")
    store_b.save(state_b)
    reapplied: list[set[str]] = []

    def reapply():
        # After the reset, the files on disk are A's; merge B's run on top of them.
        merged = store_b.load()
        reapplied.append(set(merged.seen))
        merged.seen.update(state_b.seen)
        merged.posted.games.update(state_b.posted.games)
        store_b.save(merged)

    assert GitStateRepo(clone_b).commit_and_push("state: run B", reapply) is True
    assert reapplied == [{"reddit:a"}]

    final = StateStore(clone_state(bare, tmp_path / "final")).load()
    assert set(final.seen) == {"reddit:a", "reddit:b"}
    assert set(final.posted.games) == {"steam:1", "steam:2"}
    history = git(bare, "log", "--format=%an|%s", "bot-state").splitlines()
    assert history == ["gembot|state: run B", "gembot|state: run A", "gembot|Create the GemBot state branch"]


@needs_git
def test_nothing_changed_returns_true_without_a_commit(remote, tmp_path):
    bare, seed = remote
    ensure_state_branch(seed)
    clone = clone_state(bare, tmp_path / "a")
    store = StateStore(clone)
    store.save(full_state())
    repo = GitStateRepo(clone)
    assert repo.commit_and_push("state: run 1", lambda: None)
    head = git(bare, "rev-parse", "bot-state")
    store.save(full_state())  # identical bytes
    calls: list[int] = []
    assert repo.commit_and_push("state: run 2", lambda: calls.append(1))
    assert git(bare, "rev-parse", "bot-state") == head and calls == []


@needs_git
def test_identical_state_after_reapply_counts_as_pushed(remote, tmp_path):
    bare, seed = remote
    ensure_state_branch(seed)
    clone_a = clone_state(bare, tmp_path / "a")
    clone_b = clone_state(bare, tmp_path / "b")
    state = full_state()
    StateStore(clone_a).save(state)
    assert GitStateRepo(clone_a).commit_and_push("state: run A", lambda: None)
    head = git(bare, "rev-parse", "bot-state")
    StateStore(clone_b).save(state)
    assert GitStateRepo(clone_b).commit_and_push("state: run B", lambda: StateStore(clone_b).save(state))
    assert git(bare, "rev-parse", "bot-state") == head


@needs_git
def test_configured_identity_is_respected(remote, tmp_path):
    bare, seed = remote
    ensure_state_branch(seed)
    clone = clone_state(bare, tmp_path / "a")
    git(clone, "config", "user.name", "Actions Bot")
    git(clone, "config", "user.email", "bot@example.com")
    StateStore(clone).save(full_state())
    assert GitStateRepo(clone).commit_and_push("state: run 1", lambda: None)
    assert git(bare, "log", "-1", "--format=%an <%ae>", "bot-state") == "Actions Bot <bot@example.com>"


@needs_git
def test_retries_exhausted_returns_false(remote, tmp_path, caplog):
    bare, seed = remote
    ensure_state_branch(seed)
    clone = clone_state(bare, tmp_path / "a")
    head = git(bare, "rev-parse", "bot-state")
    hook = bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'rejected by test hook' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    store = StateStore(clone)
    state = full_state()
    store.save(state)
    calls: list[int] = []

    def reapply():
        calls.append(1)
        store.save(state)

    with caplog.at_level(logging.WARNING, logger="gembot.state"):
        assert GitStateRepo(clone).commit_and_push("state: run 1", reapply, retries=2) is False
    assert len(calls) == 2
    assert git(bare, "rev-parse", "bot-state") == head
    assert "rejected by test hook" in caplog.text
    assert "could not push the state after 2 retries" in caplog.text


@needs_git
def test_reapply_error_returns_false(remote, tmp_path, caplog):
    bare, seed = remote
    ensure_state_branch(seed)
    clone_a = clone_state(bare, tmp_path / "a")
    clone_b = clone_state(bare, tmp_path / "b")
    StateStore(clone_a).save(_alarm_state("steam:1", "reddit:a"))
    assert GitStateRepo(clone_a).commit_and_push("state: run A", lambda: None)
    StateStore(clone_b).save(_alarm_state("steam:2", "reddit:b"))

    def reapply():
        raise ValueError("cannot merge")

    with caplog.at_level(logging.WARNING, logger="gembot.state"):
        assert GitStateRepo(clone_b).commit_and_push("state: run B", reapply) is False
    assert "re-applying" in caplog.text


# ---------------------------------------------------------------- scripted git (error paths)


class FakeGit:
    """A scripted ``subprocess.run`` stand-in. ``fail`` maps a git subcommand to the 1-based
    call numbers that fail (``None`` = every call)."""

    def __init__(self, fail: dict[str, set[int] | None] | None = None, stdout: dict[str, str] | None = None):
        self.fail = fail or {}
        self.stdout = {"status": " M games.json\n", **(stdout or {})}
        self.calls: list[list[str]] = []
        self.kwargs: list[dict[str, Any]] = []

    @staticmethod
    def subcommand(cmd: list[str]) -> str:
        args = iter(cmd[1:])
        for arg in args:
            if arg == "-c":
                next(args)
                continue
            return arg
        return ""

    def count(self, sub: str) -> int:
        return sum(self.subcommand(cmd) == sub for cmd in self.calls)

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        self.kwargs.append(kwargs)
        sub = self.subcommand(cmd)
        if sub in self.fail and (self.fail[sub] is None or self.count(sub) in self.fail[sub]):
            return subprocess.CompletedProcess(
                cmd, 1, "", f"fatal: {sub} failed for https://user:SECRET@github.com/o/r"
            )
        if sub == "config":
            return subprocess.CompletedProcess(cmd, 1, "", "")
        return subprocess.CompletedProcess(cmd, 0, self.stdout.get(sub, ""), "")


@pytest.mark.parametrize("failing", ["add", "status", "commit"])
def test_commit_errors_return_false(tmp_path, failing):
    fake = FakeGit(fail={failing: None})
    assert GitStateRepo(tmp_path, run=fake).commit_and_push("m", lambda: None) is False
    assert fake.count("push") == 0


def test_commit_uses_bot_identity_and_non_interactive_git(tmp_path):
    fake = FakeGit()
    assert GitStateRepo(tmp_path, branch="state-x", remote="up", run=fake).commit_and_push(
        "msg", lambda: None
    )
    commit = next(cmd for cmd in fake.calls if FakeGit.subcommand(cmd) == "commit")
    assert commit[:5] == ["git", "-c", "user.name=gembot", "-c", "user.email=gembot@users.noreply.github.com"]
    assert commit[-2:] == ["-m", "msg"]
    assert ["git", "push", "up", "HEAD:refs/heads/state-x"] in fake.calls
    assert all(kw["env"]["GIT_TERMINAL_PROMPT"] == "0" and kw["cwd"] == str(tmp_path) for kw in fake.kwargs)


def test_fetch_failures_use_up_the_retries(tmp_path, caplog):
    fake = FakeGit(fail={"push": None, "fetch": None})
    calls: list[int] = []
    with caplog.at_level(logging.WARNING, logger="gembot.state"):
        assert (
            GitStateRepo(tmp_path, run=fake).commit_and_push("m", lambda: calls.append(1), retries=3) is False
        )
    assert fake.count("push") == 1 and fake.count("fetch") == 3 and calls == []
    assert "SECRET" not in caplog.text and "https://***@github.com" in caplog.text


def test_reset_failure_then_success_on_the_next_retry(tmp_path):
    fake = FakeGit(fail={"push": {1}, "reset": {1}})
    calls: list[int] = []
    assert GitStateRepo(tmp_path, run=fake).commit_and_push("m", lambda: calls.append(1), retries=3) is True
    assert fake.count("reset") == 2 and fake.count("push") == 2 and calls == [1]


def test_recommit_failure_after_reapply_returns_false(tmp_path):
    fake = FakeGit(fail={"push": None, "commit": {2}})
    assert GitStateRepo(tmp_path, run=fake).commit_and_push("m", lambda: None) is False
    assert fake.count("push") == 1


def test_every_push_rejected_returns_false_after_retries(tmp_path):
    fake = FakeGit(fail={"push": None})
    calls: list[int] = []
    assert GitStateRepo(tmp_path, run=fake).commit_and_push("m", lambda: calls.append(1), retries=2) is False
    assert fake.count("push") == 3 and len(calls) == 2
    assert (
        GitStateRepo(tmp_path, run=FakeGit(fail={"push": None})).commit_and_push("m", lambda: None, retries=0)
        is False
    )


def test_missing_git_binary_never_raises(tmp_path, caplog):
    def run(cmd, **kwargs):
        raise FileNotFoundError("git")

    with caplog.at_level(logging.WARNING, logger="gembot.state"):
        assert GitStateRepo(tmp_path, run=run).commit_and_push("m", lambda: None) is False
        assert ensure_state_branch(tmp_path, run=run) is False
    assert "FileNotFoundError" in caplog.text


def test_ensure_state_branch_scripted_paths(tmp_path):
    exists = FakeGit(stdout={"ls-remote": "abc123\trefs/heads/bot-state\n"})
    assert ensure_state_branch(tmp_path, run=exists) is False
    assert exists.count("push") == 0
    # ls-remote matched only a longer ref name: the branch itself is missing, so create it.
    similar = FakeGit(
        stdout={
            "ls-remote": "abc123\trefs/heads/old/refs/heads/bot-state-x\n",
            "hash-object": "b1\n",
            "mktree": "t1\n",
        }
    )
    similar.stdout["commit-tree"] = "c1\n"
    assert ensure_state_branch(tmp_path, run=similar) is True
    assert ["git", "push", "-q", "origin", "c1:refs/heads/bot-state"] in similar.calls
    mktree_input = similar.kwargs[[FakeGit.subcommand(c) for c in similar.calls].index("mktree")]["input"]
    assert mktree_input == "100644 blob b1\tREADME.md\n"
