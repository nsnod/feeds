"""Learning from 👍/👎: read reactions from people, turn them into labels, nudge the weights.

* :func:`collect_reactions` reads the reactions on recent alarm/roundup messages (budgeted).
  Only people count: the bot's own reactions and any other bot account are ignored.
* :func:`apply_feedback` turns ``(up, down)`` into a label (``up - down`` clamped to ±1) and
  folds only the *change* since the last run into the weights, so re-reading the same
  reactions never applies them twice (``PostedEntry.applied_label`` remembers what was used).
* :func:`maybe_weekly_note` writes the weekly "here is what I learned" note.

The weight maths lives in :mod:`gembot.scoring.weights`; it is imported lazily so this
module stays importable (and testable) on its own.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from types import ModuleType
from typing import Any

from gembot.config import FeedbackSettings
from gembot.discord.rest import REACTION_PAGE, DiscordAPI, DiscordError
from gembot.http import Budget, BudgetExceeded, HttpError
from gembot.models import LabelExample, PostedMessage, PostedState, WeightChange, WeightsState

log = logging.getLogger(__name__)

READ_KINDS = ("alarm", "roundup")
GONE_CODES = (10003, 10008)  # unknown channel / unknown message: deleted on Discord
MAX_HISTORY = 500
_IGNORED_IN_EMOJI = {chr(c) for c in range(0x1F3FB, 0x1F400)} | {chr(0xFE0F)}  # skin tones, VS16


def _weights_module() -> ModuleType:
    from gembot.scoring import weights

    return weights


# --------------------------------------------------------------------------------------
# reading reactions
# --------------------------------------------------------------------------------------


def _base_emoji(text: str) -> str:
    return "".join(ch for ch in text if ch not in _IGNORED_IN_EMOJI)


def _emoji_key(emoji: dict[str, Any]) -> str:
    """How a reaction is addressed in URLs: the unicode emoji, or ``name:id`` for a custom one."""
    name = str(emoji.get("name") or "")
    return f"{name}:{emoji['id']}" if emoji.get("id") else name


def _direction(key: str, settings: FeedbackSettings) -> str | None:
    """ "up" / "down" when ``key`` is the configured emoji (any skin tone), else None."""
    base = _base_emoji(key)
    if base and base == _base_emoji(settings.up_emoji):
        return "up"
    if base and base == _base_emoji(settings.down_emoji):
        return "down"
    return None


def _is_due(message: PostedMessage, now: datetime, settings: FeedbackSettings) -> bool:
    if message.kind not in READ_KINDS or not message.entries:
        return False
    if now - message.posted_at > timedelta(days=settings.max_post_age_days):
        return False
    # A message is first read ``recheck_minutes`` after it was posted (nobody has reacted yet
    # in the same run), then at most once per ``recheck_minutes``.
    last = message.reactions_checked_at or message.posted_at
    return now - last >= timedelta(minutes=settings.recheck_minutes)


def _reacting_users(
    api: DiscordAPI, message: PostedMessage, key: str, *, burst: bool, budget: Budget
) -> list[dict]:
    """Every user behind one reaction, 100 per page (``after`` = last user id), each page budgeted."""
    users: list[dict] = []
    after: str | None = None
    while True:
        budget.take()
        kwargs: dict[str, Any] = {"limit": REACTION_PAGE}
        if after:
            kwargs["after"] = after
        if burst:
            kwargs["burst"] = True
        page = api.get_reaction_users(message.channel_id, message.message_id, key, **kwargs)
        users.extend(page)
        if len(page) < REACTION_PAGE:
            return users
        last = str(page[-1].get("id") or "")
        if not last or last == after:  # no progress: stop instead of looping
            return users
        after = last


def _others(reaction: dict[str, Any]) -> tuple[int, int]:
    """(normal, super) reactions on one emoji that are not the bot's own.

    ``count`` includes super reactions and the bot's own; ``count_details`` splits them.
    """
    me = 1 if reaction.get("me") else 0
    me_burst = 1 if reaction.get("me_burst") else 0
    details = reaction.get("count_details")
    if isinstance(details, dict):
        return int(details.get("normal") or 0) - me, int(details.get("burst") or 0) - me_burst
    return int(reaction.get("count") or 0) - me - me_burst, 0


def count_human_reactions(
    api: DiscordAPI,
    message: PostedMessage,
    *,
    settings: FeedbackSettings,
    budget: Budget,
    bot_user_id: str | None,
) -> tuple[int, int]:
    """``(up, down)`` = distinct human users who reacted 👍 / 👎 (any skin tone, super reactions too).

    One ``get_message`` call, then the user lists only for emojis that have reactions beyond
    the bot's own (paged, 100 users per call). Every call is charged to ``budget``. Users with
    ``"bot": true`` (the key is absent for people) and ``bot_user_id`` never count.
    """
    budget.take()
    data = api.get_message(message.channel_id, message.message_id)
    people: dict[str, set[str]] = {"up": set(), "down": set()}
    for reaction in data.get("reactions", []) or []:  # the key is omitted when there are none
        key = _emoji_key(reaction.get("emoji") or {})
        direction = _direction(key, settings)
        if direction is None:
            continue
        normal, burst = _others(reaction)
        for is_burst, extra in ((False, normal), (True, burst)):
            if extra <= 0:  # only the bot's own reaction (or none)
                continue
            for user in _reacting_users(api, message, key, burst=is_burst, budget=budget):
                user_id = str(user.get("id") or "")
                if not user_id or user.get("bot") or (bot_user_id and user_id == str(bot_user_id)):
                    continue
                people[direction].add(user_id)
    return len(people["up"]), len(people["down"])


def collect_reactions(
    api: DiscordAPI,
    posted: PostedState,
    *,
    now: datetime,
    settings: FeedbackSettings,
    budget: Budget,
    bot_user_id: str | None,
) -> dict[str, tuple[int, int]]:
    """Read human 👍/👎 counts on recent alarm/roundup messages: ``{message_id: (up, down)}``.

    Only messages younger than ``max_post_age_days`` and not read in the last
    ``recheck_minutes`` are read, least recently read first, until ``budget`` runs out.
    Updates ``reactions_checked_at`` / ``last_up`` / ``last_down`` on every message read.
    """
    if not settings.enabled:
        return {}
    due = [m for m in posted.messages.values() if _is_due(m, now, settings)]
    due.sort(key=lambda m: (m.reactions_checked_at or m.posted_at, m.posted_at, m.message_id))
    results: dict[str, tuple[int, int]] = {}
    for index, message in enumerate(due):
        try:
            up, down = count_human_reactions(
                api, message, settings=settings, budget=budget, bot_user_id=bot_user_id
            )
        except BudgetExceeded:
            log.info("feedback: reaction budget used up, %d message(s) left for later runs", len(due) - index)
            break
        except DiscordError as exc:
            if exc.blocked:
                log.warning("feedback: stopped reading reactions: %s", exc)
                break
            if exc.code in GONE_CODES:
                log.info("feedback: message %s was deleted on Discord", message.message_id)
                message.reactions_checked_at = now
            else:
                log.warning("feedback: could not read reactions on %s: %s", message.message_id, exc)
            continue
        except HttpError as exc:
            log.warning("feedback: could not read reactions on %s: %s", message.message_id, exc)
            continue
        message.reactions_checked_at = now
        message.last_up, message.last_down = up, down
        results[message.message_id] = (up, down)
    return results


# --------------------------------------------------------------------------------------
# learning
# --------------------------------------------------------------------------------------


def label_for(up: int, down: int) -> float:
    return float(max(-1, min(1, up - down)))


def _changed(before: dict[str, float], after: dict[str, float]) -> bool:
    keys = set(before) | set(after)
    return any(abs(before.get(k, 0.0) - after.get(k, 0.0)) > 1e-12 for k in keys)


def apply_feedback(
    posted: PostedState,
    weights: WeightsState,
    reactions: dict[str, tuple[int, int]],
    *,
    now: datetime,
    defaults: dict[str, float],
    settings: FeedbackSettings,
) -> list[LabelExample]:
    """Fold new 👍/👎 labels into the weights; returns the new :class:`LabelExample` records.

    For each entry, ``label = clamp(up - down, -1, 1)``. Only the difference from the label
    already applied (``entry.applied_label``) updates the weights, so reading the same
    reactions again changes nothing, and taking a 👍 back undoes its push.
    """
    if not settings.enabled or not reactions:
        return []
    w = _weights_module()
    current = dict(w.current_weights(weights, defaults))
    start = dict(current)
    labels: list[LabelExample] = []
    messages = [posted.messages[m] for m in reactions if m in posted.messages]
    messages.sort(key=lambda m: (m.posted_at, m.message_id))
    for message in messages:
        if message.kind not in READ_KINDS:
            continue
        up, down = reactions[message.message_id]
        label = label_for(up, down)
        for entry in message.entries:
            if label == entry.applied_label:
                continue
            delta = label - entry.applied_label
            # one learning step per unit of change: a 👍 switched to 👎 (delta -2) first undoes
            # the 👍, then applies the 👎 (update_weights clamps its label to ±1)
            steps = max(1, round(abs(delta)))
            for _ in range(steps):
                current = dict(w.update_weights(current, entry.features, delta / steps, settings))
            entry.applied_label = label
            labels.append(
                LabelExample(
                    at=now,
                    message_id=message.message_id,
                    game_id=entry.game_id,
                    label=label,
                    up=up,
                    down=down,
                    features=entry.features,
                    score=entry.score,
                )
            )
    if labels and _changed(start, current):
        ups = sum(1 for x in labels if x.label > 0)
        downs = sum(1 for x in labels if x.label < 0)
        weights.current = current
        weights.history.append(
            WeightChange(
                at=now,
                weights=dict(current),
                reason=f"{len(labels)} new label(s): {ups} {settings.up_emoji}, {downs} {settings.down_emoji}",
            )
        )
        del weights.history[:-MAX_HISTORY]
    return labels


def maybe_weekly_note(
    weights: WeightsState,
    labels: list[LabelExample],
    *,
    now: datetime,
    settings: FeedbackSettings,
    defaults: dict[str, float],
    commit: bool = True,
) -> str | None:
    """Once every ``weekly_note_days``: a plain-English note on how the weights moved.

    Returns None when it is not time yet, or when nothing meaningful changed (the timestamp
    still advances so the check does not repeat every run). With ``commit=False`` a note that
    *is* returned is not marked as sent: call :func:`mark_weekly_note` once it was posted, so a
    failed post is retried on the next run.
    """
    if not settings.enabled:
        return None
    last = weights.last_weekly_note_at
    if last is not None and now - last < timedelta(days=settings.weekly_note_days):
        return None
    w = _weights_module()
    current = dict(w.current_weights(weights, defaults))
    before = dict(weights.weights_at_last_note) or dict(defaults)
    recent = [x for x in labels if last is None or x.at > last]
    note = w.describe_change(before, current, recent)
    if note is None or commit:
        mark_weekly_note(weights, now=now, defaults=defaults)
    return note


def mark_weekly_note(weights: WeightsState, *, now: datetime, defaults: dict[str, float]) -> None:
    """Remember that the weekly note was written: the next one compares against these weights."""
    weights.last_weekly_note_at = now
    weights.weights_at_last_note = dict(_weights_module().current_weights(weights, defaults))
