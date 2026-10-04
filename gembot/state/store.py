"""Persisted state: JSON files on the ``bot-state`` branch, pruning, and the git push.

File layout (inside the state directory, normally ``./state``)::

    games.json      {"games": {game_id: Game}, "mentions": {mention key: Mention}}
    seen.json       {mention key: first seen}
    posted.json     {"messages": {message_id: PostedMessage}, "games": {game_id: GamePostState}}
    baselines.json  {channel: {mention key: BaselineSample}}
    weights.json    WeightsState
    labels.jsonl    one LabelExample per line (append-only history of 👍/👎 labels)
    meta.json       Meta (carries ``schema_version``)

Design rules:

* ``load`` never crashes a run: a missing file gives defaults, an unreadable file gives
  defaults *for that file only* (logged), an invalid record is skipped (logged).
* ``save`` is atomic per file (temp file in the same directory + ``os.replace``) and
  deterministic: sorted keys, compact separators, UTF-8, one record per line for the big
  maps, trailing newline. Identical state -> identical bytes -> small git diffs.
* ``Mention.comments`` bodies are never written (``Mention.signals`` is kept instead).
* ``GitStateRepo`` never raises for git failures; it logs and returns ``False``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, TypeAdapter, ValidationError

from gembot.config import StateSettings
from gembot.models import (
    BaselineSample,
    Game,
    GamePostState,
    LabelExample,
    Mention,
    Meta,
    PostedMessage,
    State,
    WeightsState,
    clean_text,
    ensure_utc,
)

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

STATE_FILES = (
    "games.json",
    "seen.json",
    "posted.json",
    "baselines.json",
    "weights.json",
    "labels.jsonl",
    "meta.json",
)

DAILY_ALARM_DAYS = 7  # meta.daily_alarms keeps this many days of counts
MAX_WEIGHT_HISTORY = 200  # weights.history keeps the newest N changes
SIZE_TEXT_KEEP = 500  # size cap: old mentions' text is cut to this many characters

PRUNE_KEYS = (
    "mentions",
    "games",
    "seen",
    "posted_messages",
    "posted_games",
    "baseline_samples",
    "baseline_channels",
    "http_cache",
    "daily_alarms",
    "game_aliases",
    "labels",
    "weight_history",
    "size_stripped",  # mentions whose detail (signals/history/long text) was dropped for size
    "size_mentions",  # mentions dropped for size
    "size_games",  # games dropped for size (last resort)
)

STATE_README = """\
# GemBot state

This branch is GemBot's memory between runs. It is written automatically by the scan
workflow at the end of every run: **do not edit it by hand** (the next run would
overwrite your change, or the push would conflict).

| file | what it holds |
|---|---|
| `games.json` | games and the posts that mention them (last 30 days) |
| `seen.json` | post IDs already processed (last 14 days) |
| `posted.json` | Discord messages GemBot sent and which games were alarmed / in a roundup |
| `baselines.json` | normal engagement per channel, used to spot unusually hot posts |
| `weights.json` | Gem Score weights learned from your 👍 / 👎 reactions |
| `labels.jsonl` | every 👍 / 👎 label, one per line |
| `meta.json` | run bookkeeping: last roundup, daily alarm counts, source health, Discord IDs |

To wipe GemBot's memory, delete this branch and run the "Setup GemBot" workflow again;
it recreates the branch with just this README.
"""

_ENCODE_KW: dict[str, Any] = {"sort_keys": True, "separators": (",", ":"), "ensure_ascii": False}

_SEEN = TypeAdapter(dict[str, datetime])
_DATETIME = TypeAdapter(datetime)
_BASELINES = TypeAdapter(dict[str, dict[str, BaselineSample]])


# --------------------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------------------


def _encode(obj: Any) -> str:
    # clean_text: any lone surrogate left in a string would make the UTF-8 write fail
    return clean_text(json.dumps(obj, **_ENCODE_KW))


def _encode_lines(obj: Any, levels: int) -> str:
    """Compact sorted JSON, except the outer ``levels`` dict levels put one entry per line.

    The result is still valid JSON; the newlines only make git diffs record-sized.
    """
    if levels <= 0 or not isinstance(obj, dict) or not obj:
        return _encode(obj)
    items = [f"{_encode(key)}:{_encode_lines(obj[key], levels - 1)}" for key in sorted(obj)]
    return "{\n" + ",\n".join(items) + "\n}"


def _dump(model: BaseModel, **kwargs: Any) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=True, **kwargs)


def _dump_mention(mention: Mention) -> dict[str, Any]:
    # Comment bodies are never persisted; ``signals`` carries what scoring needs.
    return _dump(mention, exclude={"comments"})


def serialize_state(state: State) -> dict[str, str]:
    """Render every state file to text, exactly as ``StateStore.save`` writes it."""
    games = {
        "games": {key: _dump(game) for key, game in state.games.items()},
        "mentions": {key: _dump_mention(m) for key, m in state.mentions.items()},
    }
    meta = _dump(state.meta)
    meta["schema_version"] = SCHEMA_VERSION
    return {
        "games.json": _encode_lines(games, 2) + "\n",
        "seen.json": _encode_lines(_SEEN.dump_python(state.seen, mode="json"), 1) + "\n",
        "posted.json": _encode_lines(_dump(state.posted), 2) + "\n",
        "baselines.json": _encode_lines(_BASELINES.dump_python(state.baselines, mode="json"), 2) + "\n",
        "weights.json": _encode_lines(_dump(state.weights), 1) + "\n",
        "labels.jsonl": "".join(_encode(_dump(label)) + "\n" for label in state.labels),
        "meta.json": _encode_lines(meta, 2) + "\n",
    }


def estimate_size(state: State) -> int:
    """Bytes the state would occupy on disk after ``save``."""
    return sum(len(text.encode("utf-8")) for text in serialize_state(state).values())


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------------------
# Loading helpers (lenient: skip what is broken, keep the rest)
# --------------------------------------------------------------------------------------


def _records(raw: Any, validate: Callable[[Any], Any], what: str) -> dict[str, Any]:
    """Validate a ``{key: record}`` mapping record by record; invalid records are skipped."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        log.warning("state: %s is not a mapping (%s); using defaults for it", what, type(raw).__name__)
        return {}
    out: dict[str, Any] = {}
    bad: list[str] = []
    for key, value in raw.items():
        try:
            out[str(key)] = validate(value)
        except (ValidationError, ValueError, TypeError):
            bad.append(str(key))
    if bad:
        log.warning("state: %s: skipped %d invalid record(s), e.g. %r", what, len(bad), bad[0])
    return out


def _lenient_model[M: BaseModel](cls: type[M], raw: Any, what: str) -> M:
    """Validate ``raw`` as ``cls``; on failure keep every top-level field that is valid alone."""
    if raw is None:
        return cls()
    if not isinstance(raw, dict):
        log.warning("state: %s is not a mapping (%s); using defaults for it", what, type(raw).__name__)
        return cls()
    try:
        return cls.model_validate(raw)
    except ValidationError as exc:
        good: dict[str, Any] = {}
        for name, value in raw.items():
            if name not in cls.model_fields:
                continue
            try:
                cls.model_validate({name: value})
            except ValidationError:
                continue
            good[name] = value
        dropped = sorted(set(raw) & set(cls.model_fields) - set(good))
        log.warning(
            "state: %s: invalid field(s) %s reset to defaults (%d error(s))", what, dropped, exc.error_count()
        )
        return cls.model_validate(good)


def _validate_datetime(value: Any) -> datetime:
    return ensure_utc(_DATETIME.validate_python(value))


def _validate_samples(value: Any) -> dict[str, BaselineSample]:
    if not isinstance(value, dict):
        raise TypeError("baseline channel is not a mapping")
    return _records(value, BaselineSample.model_validate, "baselines.json samples")


# --------------------------------------------------------------------------------------
# StateStore
# --------------------------------------------------------------------------------------


class StateStore:
    """Reads and writes the state directory (see the module docstring for the layout)."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    # ------------------------------------------------------------------ load
    def load(self) -> State:
        state = State()
        loaders: dict[str, Callable[[State], None]] = {
            "games.json": self._load_games,
            "seen.json": self._load_seen,
            "posted.json": self._load_posted,
            "baselines.json": self._load_baselines,
            "weights.json": self._load_weights,
            "labels.jsonl": self._load_labels,
            "meta.json": self._load_meta,
        }
        for name, loader in loaders.items():
            try:
                loader(state)
            except Exception as exc:  # belt and braces: a state file must never crash a run
                log.warning(
                    "state: could not load %s (%s: %s); using defaults for it", name, type(exc).__name__, exc
                )
        return state

    def _read_json(self, name: str) -> Any:
        path = self.root / name
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
            log.warning("state: %s is unreadable (%s); using defaults for it", name, exc)
            return None

    def _load_games(self, state: State) -> None:
        raw = self._read_json("games.json")
        if raw is None:
            return
        if not isinstance(raw, dict):
            log.warning("state: games.json is not a mapping; using defaults for it")
            return
        state.games = _records(raw.get("games"), Game.model_validate, "games.json games")
        state.mentions = _records(raw.get("mentions"), Mention.model_validate, "games.json mentions")

    def _load_seen(self, state: State) -> None:
        state.seen = _records(self._read_json("seen.json"), _validate_datetime, "seen.json")

    def _load_posted(self, state: State) -> None:
        raw = self._read_json("posted.json")
        if raw is None:
            return
        if not isinstance(raw, dict):
            log.warning("state: posted.json is not a mapping; using defaults for it")
            return
        state.posted.messages = _records(
            raw.get("messages"), PostedMessage.model_validate, "posted.json messages"
        )
        state.posted.games = _records(raw.get("games"), GamePostState.model_validate, "posted.json games")

    def _load_baselines(self, state: State) -> None:
        state.baselines = _records(self._read_json("baselines.json"), _validate_samples, "baselines.json")

    def _load_weights(self, state: State) -> None:
        state.weights = _lenient_model(WeightsState, self._read_json("weights.json"), "weights.json")

    def _load_meta(self, state: State) -> None:
        state.meta = _lenient_model(Meta, self._read_json("meta.json"), "meta.json")
        if state.meta.schema_version > SCHEMA_VERSION:
            log.warning(
                "state: meta.json has schema_version %d, newer than this GemBot (%d); unknown data is ignored",
                state.meta.schema_version,
                SCHEMA_VERSION,
            )

    def _load_labels(self, state: State) -> None:
        path = self.root / "labels.jsonl"
        if not path.is_file():
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, ValueError) as exc:
            log.warning("state: labels.jsonl is unreadable (%s); using defaults for it", exc)
            return
        bad = 0
        for line in lines:
            if not line.strip():
                continue
            try:
                state.labels.append(LabelExample.model_validate_json(line))
            except ValidationError:
                bad += 1
        if bad:
            log.warning("state: labels.jsonl: skipped %d unreadable line(s)", bad)

    # ------------------------------------------------------------------ save
    def save(self, state: State) -> None:
        """Write every state file atomically. Raises ``OSError`` if the disk write fails."""
        self.root.mkdir(parents=True, exist_ok=True)
        for name, text in serialize_state(state).items():
            _atomic_write(self.root / name, text)

    def size_bytes(self) -> int:
        """Bytes the state files currently use on disk (0 when nothing is saved yet)."""
        total = 0
        for name in STATE_FILES:
            path = self.root / name
            if path.is_file():
                total += path.stat().st_size
        return total


# --------------------------------------------------------------------------------------
# Pruning
# --------------------------------------------------------------------------------------


def _newest(*stamps: datetime | None) -> datetime | None:
    present = [ensure_utc(s) for s in stamps if s is not None]
    return max(present) if present else None


def _mention_newest(mention: Mention) -> datetime:
    newest = _newest(mention.observed_at, mention.first_seen, mention.created_at)
    assert newest is not None  # created_at is required
    return newest


def _parse_day(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _keep_last[T](items: list[T], keep: int) -> list[T]:
    keep = max(keep, 0)
    return items[len(items) - keep :] if len(items) > keep else items


def _alias_target(aliases: dict[str, str], game_id: str) -> str:
    """Follow alias chains (a -> b -> c) to the final id, guarding against cycles."""
    target = aliases[game_id]
    for _ in range(len(aliases)):
        if target not in aliases:
            break
        target = aliases[target]
    return target


def _unlink_mentions(state: State, keys: set[str]) -> None:
    if not keys:
        return
    for game in state.games.values():
        if any(k in keys for k in game.mention_keys):
            game.mention_keys = [k for k in game.mention_keys if k not in keys]


def prune(state: State, now: datetime, settings: StateSettings, baseline_days: int = 14) -> dict[str, int]:
    """Drop everything past its retention window, then enforce ``settings.max_bytes``.

    Mutates ``state`` in place and returns how many items were removed per category
    (every key of ``PRUNE_KEYS`` is present, zero when nothing was removed).
    """
    now = ensure_utc(now)
    removed = dict.fromkeys(PRUNE_KEYS, 0)

    # Games and their mentions (30 days by default).
    games_cutoff = now - timedelta(days=settings.games_days)
    old_games = {gid for gid, game in state.games.items() if ensure_utc(game.last_seen) < games_cutoff}
    for gid in old_games:
        del state.games[gid]
    removed["games"] = len(old_games)
    old_mentions = {
        key
        for key, m in state.mentions.items()
        if _mention_newest(m) < games_cutoff or (m.game_id is not None and m.game_id in old_games)
    }
    for key in old_mentions:
        del state.mentions[key]
    removed["mentions"] = len(old_mentions)
    _unlink_mentions(state, old_mentions)

    # Seen post IDs.
    seen_cutoff = now - timedelta(days=settings.seen_days)
    # Keys of mentions still stored stay: they are how "new" is told apart from "seen before".
    old_seen = [
        key for key, at in state.seen.items() if ensure_utc(at) < seen_cutoff and key not in state.mentions
    ]
    for key in old_seen:
        del state.seen[key]
    removed["seen"] = len(old_seen)

    # Posted Discord messages (reaction tracking) and per-game alarm/roundup memory.
    posted_cutoff = now - timedelta(days=settings.posted_days)
    old_messages = [
        mid for mid, msg in state.posted.messages.items() if ensure_utc(msg.posted_at) < posted_cutoff
    ]
    for mid in old_messages:
        del state.posted.messages[mid]
    removed["posted_messages"] = len(old_messages)

    memory_cutoff = now - timedelta(days=settings.alarm_memory_days)
    old_posted_games = []
    for gid, entry in state.posted.games.items():
        newest = _newest(entry.alarmed_at, entry.roundup_at)
        if newest is None:
            # Nothing was posted yet (e.g. only a pending carry-over): keep it while the game lives.
            if gid not in state.games and not entry.pending_roundup:
                old_posted_games.append(gid)
        elif newest < memory_cutoff:
            old_posted_games.append(gid)
    for gid in old_posted_games:
        del state.posted.games[gid]
    removed["posted_games"] = len(old_posted_games)

    # Baseline samples per channel.
    baseline_cutoff = now - timedelta(days=baseline_days)
    for channel in list(state.baselines):
        samples = state.baselines[channel]
        old = [key for key, sample in samples.items() if ensure_utc(sample.at) < baseline_cutoff]
        for key in old:
            del samples[key]
        removed["baseline_samples"] += len(old)
        if not samples:
            del state.baselines[channel]
            removed["baseline_channels"] += 1

    # Meta bookkeeping.
    meta = state.meta
    cache_cutoff = now - timedelta(days=settings.http_cache_days)
    old_cache = [url for url, entry in meta.http_cache.items() if ensure_utc(entry.at) < cache_cutoff]
    for url in old_cache:
        del meta.http_cache[url]
    removed["http_cache"] = len(old_cache)

    first_day = now.date() - timedelta(days=DAILY_ALARM_DAYS)
    old_days = [day for day in meta.daily_alarms if (parsed := _parse_day(day)) is None or parsed < first_day]
    for day in old_days:
        del meta.daily_alarms[day]
    removed["daily_alarms"] = len(old_days)

    dead_aliases = [a for a in meta.game_aliases if _alias_target(meta.game_aliases, a) not in state.games]
    for alias in dead_aliases:
        del meta.game_aliases[alias]
    removed["game_aliases"] = len(dead_aliases)

    # Labels (keep the newest) and weight history (keep the last N).
    if len(state.labels) > max(settings.max_labels, 0):
        ordered = sorted(state.labels, key=lambda label: ensure_utc(label.at))
        state.labels = _keep_last(ordered, settings.max_labels)
        removed["labels"] = len(ordered) - len(state.labels)
    history = state.weights.history
    state.weights.history = _keep_last(history, MAX_WEIGHT_HISTORY)
    removed["weight_history"] = len(history) - len(state.weights.history)

    _enforce_size(state, settings.max_bytes, removed)
    return removed


def _json_bytes(obj: Any) -> int:
    return len(_encode(obj).encode("utf-8"))


def _mention_bytes(key: str, mention: Mention) -> int:
    # Its line in games.json plus its entry in the owning game's mention_keys.
    return 2 * _json_bytes(key) + _json_bytes(_dump_mention(mention)) + 4


def _game_bytes(game_id: str, game: Game) -> int:
    return _json_bytes(game_id) + _json_bytes(_dump(game)) + 3


def _oldest_mentions(state: State) -> list[str]:
    return sorted(state.mentions, key=lambda k: (_mention_newest(state.mentions[k]), k))


def _strip_detail(mention: Mention) -> bool:
    """Drop the bulky-but-optional parts of a mention. Returns True if anything changed."""
    changed = bool(mention.comments or mention.signals or len(mention.history) > 1)
    changed = changed or len(mention.text) > SIZE_TEXT_KEEP
    mention.comments = []
    mention.signals = None
    mention.history = mention.history[-1:]
    mention.text = mention.text[:SIZE_TEXT_KEEP]
    return changed


def _enforce_size(state: State, max_bytes: int, removed: dict[str, int]) -> None:
    """Shrink the state below ``max_bytes``: first the detail of the oldest mentions, then
    the oldest mentions, then (last resort) the least recently seen games."""
    total = estimate_size(state)
    if total <= max_bytes:
        return
    start = total

    for key in _oldest_mentions(state):
        if total <= max_bytes:
            break
        mention = state.mentions[key]
        before = _mention_bytes(key, mention)
        if _strip_detail(mention):
            total -= before - _mention_bytes(key, mention)
            removed["size_stripped"] += 1

    # Per-item byte counts are estimates, so re-measure after each pass and go again if
    # needed. Every pass that starts over the cap drops at least one item, so this ends.
    total = estimate_size(state)
    while total > max_bytes and (state.mentions or state.games):
        if state.mentions:
            dropped: set[str] = set()
            for key in _oldest_mentions(state):
                if total <= max_bytes:
                    break
                total -= _mention_bytes(key, state.mentions.pop(key))
                dropped.add(key)
            _unlink_mentions(state, dropped)
            removed["size_mentions"] += len(dropped)
        else:  # alarm memory lives in posted.games, so dropping a Game never re-alarms it
            for gid in sorted(state.games, key=lambda g: (ensure_utc(state.games[g].last_seen), g)):
                if total <= max_bytes:
                    break
                total -= _game_bytes(gid, state.games.pop(gid))
                removed["size_games"] += 1
        total = estimate_size(state)

    level = logging.WARNING if total > max_bytes else logging.INFO
    log.log(
        level,
        "state: size cap %d bytes: %d -> %d bytes (stripped %d mention(s), dropped %d mention(s), %d game(s))",
        max_bytes,
        start,
        total,
        removed["size_stripped"],
        removed["size_mentions"],
        removed["size_games"],
    )


# --------------------------------------------------------------------------------------
# Git: commit + push the state directory, create the orphan branch
# --------------------------------------------------------------------------------------

GIT_TIMEOUT_S = 120.0
BOT_IDENTITY = (("user.name", "gembot"), ("user.email", "gembot@users.noreply.github.com"))
_CREDENTIALS_IN_URL = re.compile(r"(\w+://)[^/@\s]+@")

Runner = Callable[..., subprocess.CompletedProcess[str]]


def _scrub(text: str) -> str:
    return _CREDENTIALS_IN_URL.sub(r"\1***@", text.strip())[-500:]


def _run_git(
    run: Runner, cwd: Path, args: Sequence[str], *, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one git command; never raises (a failure to start git becomes returncode 127)."""
    cmd = ["git", *args]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        return run(
            cmd,
            cwd=str(cwd),
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT_S,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(cmd, 127, "", f"{type(exc).__name__}: {exc}")


def _identity_args(run: Runner, cwd: Path) -> list[str]:
    """``-c user.name=gembot -c user.email=...`` for whatever is not configured already."""
    args: list[str] = []
    for key, value in BOT_IDENTITY:
        result = _run_git(run, cwd, ["config", "--get", key])
        if result.returncode != 0 or not (result.stdout or "").strip():
            args += ["-c", f"{key}={value}"]
    return args


class GitStateRepo:
    """Commits the state directory (a checkout of the state branch) and pushes it."""

    def __init__(
        self,
        workdir: Path | str,
        branch: str = "bot-state",
        remote: str = "origin",
        run: Runner = subprocess.run,
    ) -> None:
        self.workdir = Path(workdir)
        self.branch = branch
        self.remote = remote
        self._run = run
        self._identity: list[str] | None = None

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return _run_git(self._run, self.workdir, args)

    def _ok(self, result: subprocess.CompletedProcess[str], what: str) -> bool:
        if result.returncode == 0:
            return True
        log.warning(
            "state git: %s failed (exit %d): %s", what, result.returncode, _scrub(result.stderr or "")
        )
        return False

    def _commit_changes(self, message: str) -> str:
        """Stage everything and commit. Returns "committed", "clean" or "error"."""
        if not self._ok(self._git("add", "-A"), "git add"):
            return "error"
        status = self._git("status", "--porcelain")
        if not self._ok(status, "git status"):
            return "error"
        if not (status.stdout or "").strip():
            return "clean"
        if self._identity is None:
            self._identity = _identity_args(self._run, self.workdir)
        if not self._ok(self._git(*self._identity, "commit", "-q", "-m", message), "git commit"):
            return "error"
        return "committed"

    def _push(self) -> bool:
        return self._ok(self._git("push", self.remote, f"HEAD:refs/heads/{self.branch}"), "git push")

    def _resync(self, message: str, reapply: Callable[[], None]) -> str:
        """Fetch the remote branch, reset onto it, re-apply our state and re-commit.

        Returns "committed", "clean" (our state already matches the remote), "retry"
        (fetch/reset failed, worth another attempt) or "fatal".
        """
        if not self._ok(self._git("fetch", "-q", self.remote, self.branch), "git fetch"):
            return "retry"
        if not self._ok(self._git("reset", "-q", "--hard", "FETCH_HEAD"), "git reset"):
            return "retry"
        try:
            reapply()
        except Exception:
            log.exception("state git: re-applying the state after a push conflict failed")
            return "fatal"
        result = self._commit_changes(message)
        return "fatal" if result == "error" else result

    def commit_and_push(self, message: str, reapply: Callable[[], None], retries: int = 3) -> bool:
        """Commit everything in ``workdir`` and push it to ``remote``/``branch``.

        On a rejected push: fetch the remote branch, ``reset --hard`` to it, call
        ``reapply()`` (which must rewrite our state files), re-commit and push again, at
        most ``retries`` times. Returns ``True`` when the remote has our state (or nothing
        changed), ``False`` otherwise. Never raises for git failures.
        """
        try:
            first = self._commit_changes(message)
            if first == "clean":
                log.info("state git: nothing changed, no commit")
                return True
            if first == "error":
                return False
            for attempt in range(retries + 1):
                if attempt:
                    log.warning(
                        "state git: push failed, re-applying on top of the remote (retry %d/%d)",
                        attempt,
                        retries,
                    )
                    outcome = self._resync(message, reapply)
                    if outcome == "clean":
                        log.info("state git: remote already has this state")
                        return True
                    if outcome == "fatal":
                        return False
                    if outcome == "retry":
                        continue
                if self._push():
                    return True
            log.error(
                "state git: could not push the state after %d retries; this run's state is lost", retries
            )
            return False
        except Exception:  # pragma: no cover - defensive: git trouble must never crash a run
            log.exception("state git: unexpected error")
            return False


def ensure_state_branch(
    repo_dir: Path | str, branch: str = "bot-state", remote: str = "origin", *, run: Runner = subprocess.run
) -> bool:
    """Create the orphan state branch on ``remote`` if it does not exist yet.

    Returns ``True`` if the branch was created, ``False`` if it already existed or could not
    be created (logged; never raises). :func:`state_branch_status` tells the two apart.
    """
    return state_branch_status(repo_dir, branch, remote, run=run) == "created"


def state_branch_status(
    repo_dir: Path | str, branch: str = "bot-state", remote: str = "origin", *, run: Runner = subprocess.run
) -> Literal["created", "exists", "error"]:
    """Create the orphan state branch on ``remote`` if needed; say what happened.

    The branch gets a single commit containing only ``README.md``. It is built with git
    plumbing (hash-object / mktree / commit-tree / push), so the caller's checkout, index
    and working tree are never touched. Never raises: failures are logged and give "error".
    """
    cwd = Path(repo_dir)
    ref = f"refs/heads/{branch}"
    try:
        probe = _run_git(run, cwd, ["ls-remote", "--exit-code", remote, ref])
        if probe.returncode == 0 and any(
            line.endswith(f"\t{ref}") for line in (probe.stdout or "").splitlines()
        ):
            return "exists"
        if probe.returncode not in (0, 2):
            log.warning("state git: cannot check %s on %s: %s", ref, remote, _scrub(probe.stderr or ""))
            return "error"

        blob = _run_git(run, cwd, ["hash-object", "-w", "--stdin"], stdin=STATE_README)
        if blob.returncode != 0:
            log.warning("state git: hash-object failed: %s", _scrub(blob.stderr or ""))
            return "error"
        tree = _run_git(run, cwd, ["mktree"], stdin=f"100644 blob {blob.stdout.strip()}\tREADME.md\n")
        if tree.returncode != 0:
            log.warning("state git: mktree failed: %s", _scrub(tree.stderr or ""))
            return "error"
        identity = _identity_args(run, cwd)
        commit = _run_git(
            run, cwd, [*identity, "commit-tree", tree.stdout.strip(), "-m", "Create the GemBot state branch"]
        )
        if commit.returncode != 0:
            log.warning("state git: commit-tree failed: %s", _scrub(commit.stderr or ""))
            return "error"
        push = _run_git(run, cwd, ["push", "-q", remote, f"{commit.stdout.strip()}:{ref}"])
        if push.returncode != 0:
            log.warning("state git: pushing the new %s branch failed: %s", branch, _scrub(push.stderr or ""))
            return "error"
        log.info("state git: created orphan branch %s on %s", branch, remote)
        return "created"
    except Exception:  # pragma: no cover - defensive
        log.exception("state git: creating the state branch failed")
        return "error"
