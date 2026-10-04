"""Alarm / roundup planning and committing (BUILD_SPEC 4.4) with hand-made score results."""

from __future__ import annotations

from datetime import timedelta

import pytest

from gembot.config import DecisionSettings
from gembot.models import DecisionKind, Features, GamePostState, Meta, PostedState, ScoreResult
from gembot.scoring.decide import PostPlan, commit_plan, is_roundup_due, plan_posts, qualifies_for_alarm
from tests.factories import NOW, make_game

SETTINGS = DecisionSettings()
HOT = Features(velocity=0.8, hype=0.7, cross=0.6, fit=0.9, fresh=1.0)
LUKEWARM = Features(velocity=0.3, hype=0.2, fit=0.9, fresh=1.0)


def result(game_id: str, score: float, features: Features = HOT, **kw) -> ScoreResult:
    return ScoreResult(game_id=game_id, score=score, features=features, **kw)


def games_for(*results: ScoreResult, **game_kw) -> dict:
    return {r.game_id: make_game(r.game_id, r.game_id.upper(), **game_kw) for r in results}


def plan(results, games=None, posted=None, meta=None, now=NOW, settings=SETTINGS) -> PostPlan:
    results = {r.game_id: r for r in results}
    games = games if games is not None else games_for(*results.values())
    return plan_posts(results, games, posted or PostedState(), meta or Meta(), now=now, settings=settings)


def not_due() -> Meta:
    return Meta(last_roundup_at=NOW - timedelta(minutes=30))


# ----------------------------------------------------------------------------- qualification


def test_qualifies_for_alarm():
    assert qualifies_for_alarm(result("a", 72), SETTINGS)
    assert not qualifies_for_alarm(result("a", 71.9), SETTINGS)
    one_signal = Features(velocity=0.9, hype=0.1, cross=0.0, meme=0.0)
    assert not qualifies_for_alarm(result("a", 90, one_signal), SETTINGS)
    meme_and_velocity = Features(velocity=0.5, meme=0.6)
    assert qualifies_for_alarm(result("a", 80, meme_and_velocity), SETTINGS)
    assert not qualifies_for_alarm(result("a", 99, excluded=True), SETTINGS)


def test_roundup_due():
    assert is_roundup_due(Meta(), NOW, SETTINGS)
    assert is_roundup_due(Meta(last_roundup_at=NOW - timedelta(minutes=115)), NOW, SETTINGS)
    assert not is_roundup_due(Meta(last_roundup_at=NOW - timedelta(minutes=114)), NOW, SETTINGS)


# ----------------------------------------------------------------------------- alarms


def test_alarm_once_and_never_again():
    p = plan([result("a", 80)], meta=not_due())
    assert [(d.game_id, d.kind, d.escalated) for d in p.alarms] == [("a", DecisionKind.ALARM, False)]
    posted = PostedState(games={"a": GamePostState(alarmed_at=NOW - timedelta(days=3), alarm_score=80)})
    assert plan([result("a", 95)], posted=posted, meta=not_due()).alarms == []


def test_alarm_caps_per_run_and_per_day():
    results = [result(f"g{i}", 90 - i) for i in range(5)]
    p = plan(results, meta=not_due())
    assert [d.game_id for d in p.alarms] == ["g0", "g1", "g2"]
    assert p.carried == ["g3", "g4"]
    assert p.roundup == []
    meta = not_due()
    meta.daily_alarms[NOW.date().isoformat()] = 11
    p = plan(results, meta=meta)
    assert [d.game_id for d in p.alarms] == ["g0"]
    assert p.carried == ["g1", "g2", "g3", "g4"]


def test_overflow_goes_into_a_due_roundup_first():
    results = [result(f"g{i}", 90 - i) for i in range(4)] + [result("calm", 60, LUKEWARM)]
    p = plan(results)
    assert [d.game_id for d in p.alarms] == ["g0", "g1", "g2"]
    assert [(d.game_id, d.would_have_alarmed) for d in p.roundup] == [("g3", True), ("calm", False)]
    assert p.roundup[0].kind == DecisionKind.ROUNDUP
    assert "per run" in p.roundup[0].note


def test_pending_games_do_not_alarm_later_and_lead_the_next_roundup():
    posted = PostedState(games={"late": GamePostState(pending_roundup=True, would_have_alarmed=True)})
    p = plan([result("late", 88), result("calm", 60, LUKEWARM)], posted=posted, meta=not_due())
    assert p.alarms == [] and p.roundup == []
    p = plan(
        [result("calm", 60, LUKEWARM)], games=games_for(result("late", 0), result("calm", 0)), posted=posted
    )
    assert [(d.game_id, d.would_have_alarmed) for d in p.roundup] == [("late", True), ("calm", False)]
    assert p.roundup[0].note == "carried over from the alarm cap"


def test_pending_game_that_was_pruned_or_excluded_is_dropped():
    posted = PostedState(
        games={
            "gone": GamePostState(pending_roundup=True),
            "bad": GamePostState(pending_roundup=True),
        }
    )
    games = games_for(result("bad", 0))
    games["bad"].excluded_reason = "banned keyword"
    assert plan([], games=games, posted=posted).roundup == []


def test_escalation_when_game_was_in_a_roundup():
    posted = PostedState(games={"a": GamePostState(roundup_at=NOW - timedelta(hours=6), roundup_score=55)})
    p = plan([result("a", 76)], posted=posted, meta=not_due())
    assert p.alarms[0].escalated
    assert p.alarms[0].note == "escalated from a roundup"


def test_excluded_games_never_alarm_or_roundup():
    r = result("big", 99)
    games = games_for(r)
    games["big"].excluded_reason = "big studio: Ubisoft"
    p = plan([r], games=games)
    assert p.alarms == [] and p.roundup == []
    p = plan([result("big2", 99, excluded=True)])
    assert p.alarms == [] and p.roundup == []


# ----------------------------------------------------------------------------- roundups


def test_roundup_uses_stored_scores_within_lookback_and_results_take_precedence():
    games = {
        "stored": make_game("stored", "Stored", last_score=61, last_scored_at=NOW - timedelta(hours=5)),
        "stale": make_game("stale", "Stale", last_score=70, last_scored_at=NOW - timedelta(hours=49)),
        "low": make_game("low", "Low", last_score=44.9, last_scored_at=NOW - timedelta(hours=1)),
        "never": make_game("never", "Never"),
        "fresh": make_game("fresh", "Fresh", last_score=30, last_scored_at=NOW - timedelta(hours=1)),
    }
    p = plan([result("fresh", 58, LUKEWARM)], games=games)
    assert [(d.game_id, d.score) for d in p.roundup] == [("stored", 61), ("fresh", 58)]


def test_roundup_skips_alarmed_and_already_posted_unless_heating_up():
    games = {
        gid: make_game(gid, gid, last_score=score, last_scored_at=NOW - timedelta(hours=1))
        for gid, score in [("alarmed", 80), ("posted", 59), ("hotter", 70), ("new", 50)]
    }
    posted = PostedState(
        games={
            "alarmed": GamePostState(alarmed_at=NOW - timedelta(days=1)),
            "posted": GamePostState(roundup_at=NOW - timedelta(hours=4), roundup_score=45),
            "hotter": GamePostState(roundup_at=NOW - timedelta(hours=4), roundup_score=55),
        }
    )
    p = plan([], games=games, posted=posted)
    assert [(d.game_id, d.heating_up) for d in p.roundup] == [("hotter", True), ("new", False)]
    assert p.roundup[0].note == "heating up: 55 → 70"


def test_roundup_caps_items_and_sorts_by_score():
    results = [result(f"g{i:02d}", 46 + i, LUKEWARM) for i in range(12)]
    p = plan(results)
    assert len(p.roundup) == 8
    assert [d.score for d in p.roundup] == sorted((d.score for d in p.roundup), reverse=True)
    assert p.roundup[0].game_id == "g11"


def test_nothing_qualifies_gives_empty_roundup_but_due():
    p = plan([result("meh", 30, LUKEWARM)])
    assert p.roundup_due and p.roundup == [] and p.alarms == []


def test_not_due_means_no_roundup():
    p = plan([result("ok", 60, LUKEWARM)], meta=not_due())
    assert not p.roundup_due and p.roundup == []


def test_plan_posts_is_pure():
    posted = PostedState(games={"late": GamePostState(pending_roundup=True)})
    meta = Meta()
    before = (posted.model_dump_json(), meta.model_dump_json())
    plan([result(f"g{i}", 90 - i) for i in range(5)], posted=posted, meta=meta)
    assert (posted.model_dump_json(), meta.model_dump_json()) == before


# ----------------------------------------------------------------------------- commit


def test_commit_plan_records_everything():
    posted = PostedState()
    meta = Meta()
    results = [result(f"g{i}", 90 - i) for i in range(4)] + [result("calm", 60, LUKEWARM)]
    p = plan(results, posted=posted, meta=meta)
    commit_plan(p, posted, meta, now=NOW)
    for gid in ("g0", "g1", "g2"):
        assert posted.games[gid].alarmed_at == NOW
    assert posted.games["g0"].alarm_score == 90
    assert meta.alarms_on(NOW.date()) == 3
    g3 = posted.games["g3"]
    assert (
        g3.would_have_alarmed and not g3.pending_roundup and g3.roundup_at == NOW and g3.roundup_score == 87
    )
    assert posted.games["calm"].roundup_at == NOW and not posted.games["calm"].would_have_alarmed
    assert meta.last_roundup_at == NOW


def test_commit_plan_keeps_overflow_pending_when_no_roundup_was_due():
    posted, meta = PostedState(), not_due()
    p = plan([result(f"g{i}", 90 - i) for i in range(5)], posted=posted, meta=meta)
    commit_plan(p, posted, meta, now=NOW)
    assert posted.games["g4"].pending_roundup and posted.games["g4"].would_have_alarmed
    assert posted.games["g4"].roundup_at is None
    assert meta.last_roundup_at == NOW - timedelta(minutes=30)


def test_commit_plan_advances_roundup_clock_even_when_empty():
    posted, meta = PostedState(), Meta()
    p = plan([], games={}, posted=posted, meta=meta)
    commit_plan(p, posted, meta, now=NOW)
    assert meta.last_roundup_at == NOW
    assert posted.games == {}


@pytest.mark.parametrize("runs", [2, 5])
def test_no_double_alarm_across_runs(runs):
    posted, meta = PostedState(), Meta()
    alarms = []
    for i in range(runs):
        now = NOW + timedelta(minutes=30 * i)
        p = plan([result("a", 85)], posted=posted, meta=meta, now=now)
        alarms += p.alarms
        commit_plan(p, posted, meta, now=now)
    assert len(alarms) == 1
