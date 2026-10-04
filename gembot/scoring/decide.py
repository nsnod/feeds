"""Alarm / roundup decisions (BUILD_SPEC 4.4).

* 🚨 **Alarm**: score >= ``alarm_score`` and at least ``alarm_min_signals`` of
  {velocity >= 0.5, hype >= 0.5, cross >= 0.6, meme >= 0.6}, never alarmed before.
  At most ``max_alarms_per_run`` per run and ``max_alarms_per_day`` per UTC day; the
  extras go to the next roundup marked "would have alarmed" (and stay there: a game that
  was routed to the roundup by a cap does not alarm in a later run).
* 📈 **Escalated**: an alarm for a game that already appeared in a roundup.
* 📋 **Roundup** (when due, every ``roundup_interval_minutes``): carried-over "would have
  alarmed" games first, then the best games with score >= ``roundup_score`` that were never
  posted, up to ``roundup_max_items``. A game already in a roundup may return only when its
  score rose by ``heating_up_delta`` or more ("📈 heating up"). Nothing qualifies -> an
  empty list and the pipeline posts nothing.

:func:`plan_posts` is pure; :func:`commit_plan` records what was sent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from gembot.config import DecisionSettings
from gembot.models import Decision, DecisionKind, Game, GamePostState, Meta, PostedState, ScoreResult


@dataclass
class PostPlan:
    alarms: list[Decision] = field(default_factory=list)
    roundup: list[Decision] = field(default_factory=list)  # empty when not due or nothing qualifies
    roundup_due: bool = False
    carried: list[str] = field(default_factory=list)  # game_ids that hit the alarm cap -> next roundup


def qualifies_for_alarm(result: ScoreResult, settings: DecisionSettings) -> bool:
    """Score and signal test only (state such as "already alarmed" is checked in plan_posts)."""
    if result.excluded or result.score < settings.alarm_score:
        return False
    values = result.features.as_dict()
    passed = sum(
        1
        for name, threshold in settings.alarm_signal_thresholds.items()
        if values.get(name, 0.0) >= threshold
    )
    return passed >= settings.alarm_min_signals


def is_roundup_due(meta: Meta, now: datetime, settings: DecisionSettings) -> bool:
    if meta.last_roundup_at is None:
        return True
    return now - meta.last_roundup_at >= timedelta(minutes=settings.roundup_interval_minutes)


def _excluded(game_id: str, results: dict[str, ScoreResult], games: dict[str, Game]) -> bool:
    result = results.get(game_id)
    game = games.get(game_id)
    return bool((result is not None and result.excluded) or (game is not None and game.excluded_reason))


def _current(
    game_id: str, results: dict[str, ScoreResult], games: dict[str, Game], now: datetime
) -> tuple[float | None, datetime | None]:
    """(score, scored_at): this run's result wins over the stored ``Game.last_score``."""
    if game_id in results:
        return results[game_id].score, now
    game = games.get(game_id)
    if game is None:
        return None, None
    return game.last_score, game.last_scored_at


def _alarm_blocked(gs: GamePostState | None) -> bool:
    """Already alarmed, or already routed to the roundup by a cap."""
    return gs is not None and (gs.alarmed_at is not None or gs.pending_roundup or gs.would_have_alarmed)


def plan_posts(
    results: dict[str, ScoreResult],
    games: dict[str, Game],
    posted: PostedState,
    meta: Meta,
    *,
    now: datetime,
    settings: DecisionSettings,
) -> PostPlan:
    """Decide this run's alarms and (if due) the roundup. Pure: nothing is mutated."""
    plan = PostPlan(roundup_due=is_roundup_due(meta, now, settings))
    states = posted.games
    used_today = meta.alarms_on(now.date())
    overflow: list[Decision] = []

    for result in sorted(results.values(), key=lambda r: (-r.score, r.game_id)):
        gid = result.game_id
        gs = states.get(gid)
        if _alarm_blocked(gs) or _excluded(gid, results, games) or not qualifies_for_alarm(result, settings):
            continue
        escalated = gs is not None and gs.roundup_at is not None
        if len(plan.alarms) >= settings.max_alarms_per_run:
            note = f"alarm cap: {settings.max_alarms_per_run} per run"
        elif used_today + len(plan.alarms) >= settings.max_alarms_per_day:
            note = f"alarm cap: {settings.max_alarms_per_day} per day"
        else:
            plan.alarms.append(
                Decision(
                    game_id=gid,
                    kind=DecisionKind.ALARM,
                    score=result.score,
                    escalated=escalated,
                    note="escalated from a roundup" if escalated else "",
                )
            )
            continue
        overflow.append(
            Decision(
                game_id=gid, kind=DecisionKind.ROUNDUP, score=result.score, would_have_alarmed=True, note=note
            )
        )
    plan.carried = [d.game_id for d in overflow]

    if not plan.roundup_due:
        return plan

    alarming = {d.game_id for d in plan.alarms}
    pending = list(overflow)
    taken = alarming | set(plan.carried)
    for gid, gs in sorted(states.items()):
        if not gs.pending_roundup or gs.alarmed_at is not None or gid in taken:
            continue
        if (gid not in games and gid not in results) or _excluded(gid, results, games):
            continue
        score, _ = _current(gid, results, games, now)
        pending.append(
            Decision(
                game_id=gid,
                kind=DecisionKind.ROUNDUP,
                score=score or 0.0,
                would_have_alarmed=True,
                note="carried over from the alarm cap",
            )
        )
        taken.add(gid)
    pending.sort(key=lambda d: (-d.score, d.game_id))

    regular: list[Decision] = []
    cutoff = now - timedelta(hours=settings.roundup_lookback_hours)
    for gid in sorted(set(games) | set(results)):
        gs = states.get(gid)
        if gid in taken or (gs is not None and gs.alarmed_at is not None) or _excluded(gid, results, games):
            continue
        score, scored_at = _current(gid, results, games, now)
        if score is None or scored_at is None or scored_at < cutoff or score < settings.roundup_score:
            continue
        heating_up = False
        note = ""
        if gs is not None and gs.roundup_at is not None:
            before = gs.roundup_score or 0.0
            if score < before + settings.heating_up_delta:
                continue
            heating_up = True
            note = f"heating up: {before:.0f} → {score:.0f}"
        regular.append(
            Decision(game_id=gid, kind=DecisionKind.ROUNDUP, score=score, heating_up=heating_up, note=note)
        )
    regular.sort(key=lambda d: (-d.score, d.game_id))

    plan.roundup = (pending + regular)[: max(settings.roundup_max_items, 0)]
    return plan


def commit_plan(plan: PostPlan, posted: PostedState, meta: Meta, *, now: datetime) -> None:
    """Record a plan once its messages were sent.

    * alarms: ``alarmed_at`` / ``alarm_score``, today's alarm count + 1
    * cap overflow (``carried``): ``pending_roundup`` and ``would_have_alarmed``
    * roundup entries: ``roundup_at`` / ``roundup_score``, no longer pending
    * when a roundup was due, ``meta.last_roundup_at = now`` even if nothing qualified, so
      the 2-hour cadence holds instead of re-checking every 30 minutes.
    """
    day = now.date().isoformat()
    for decision in plan.alarms:
        gs = posted.games.setdefault(decision.game_id, GamePostState())
        gs.alarmed_at = now
        gs.alarm_score = decision.score
        gs.pending_roundup = False
        meta.daily_alarms[day] = meta.daily_alarms.get(day, 0) + 1
    for game_id in plan.carried:
        gs = posted.games.setdefault(game_id, GamePostState())
        gs.pending_roundup = True
        gs.would_have_alarmed = True
    for decision in plan.roundup:
        gs = posted.games.setdefault(decision.game_id, GamePostState())
        gs.roundup_at = now
        gs.roundup_score = decision.score
        gs.pending_roundup = False
        if decision.would_have_alarmed:
            gs.would_have_alarmed = True
    if plan.roundup_due:
        meta.last_roundup_at = now
