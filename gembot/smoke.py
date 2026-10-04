"""``python -m gembot smoke``: hit every real source once and report what happened.

Runs the full pipeline against the live sources on a *temporary copy* of the state (so
nothing is ever written back), never posts scores to Discord, and prints a summary table
(requests, mentions, failures, timings, top 10 games). The same markdown is appended to
``$GITHUB_STEP_SUMMARY`` when it is set, so it shows up on the workflow run page.

With ``--post-test`` it additionally posts one clearly-labelled TEST alarm to
``#gem-alarm`` (channel ids come from the state directory written by the Setup workflow).
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path

from gembot.config import Config
from gembot.models import State
from gembot.pipeline import Pipeline, RunResult
from gembot.state.store import StateStore

log = logging.getLogger("gembot.smoke")

SUMMARY_LIMIT = 900_000  # $GITHUB_STEP_SUMMARY is capped at 1 MiB per step


def _cell(text: object, limit: int = 140) -> str:
    value = str(text).replace("|", "\\|").replace("\n", " ").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def render_summary(result: RunResult, *, seconds: float, posted_test: str | None = None) -> str:
    lines = [
        "## GemBot smoke test",
        "",
        f"Run at {result.now:%Y-%m-%d %H:%M} UTC, took **{seconds:.1f} s** "
        "(GitHub bills each job rounded up to a whole minute).",
        "",
        "| Source | Status | Requests | Mentions | Notes |",
        "|---|---|---:|---:|---|",
    ]
    failures = 0
    for name, report in result.reports.items():
        if report.skipped:
            status, note = "⏭️ skipped", report.skip_reason or ""
        elif report.ok and not report.errors:
            status, note = "✅ ok", "; ".join(report.warnings[:2])
        elif report.ok:
            status, note = "⚠️ partial", "; ".join(report.errors[:2])
        else:
            failures += 1
            status, note = "❌ failed", "; ".join(report.errors[:2])
        lines.append(
            f"| {name} | {status} | {report.requests} | {report.mentions} | {_cell(note, 300) if note else ''} |"
        )
    lines += [
        "",
        f"**{result.collected}** mentions collected, **{len(result.results)}** games scored, "
        f"{failures} source(s) failed.",
        "",
        "### Top 10 scored games",
        "",
    ]
    top = result.top(10)
    if top:
        lines += ["| Score | Game | Why |", "|---:|---|---|"]
        for item in top:
            reason = item.reasons[0] if item.reasons else ""
            lines.append(f"| {item.score:.1f} | `{_cell(item.game_id, 60)}` | {_cell(reason)} |")
    else:
        lines.append("_No games scored (no source returned usable mentions)._")
    plan = result.plan
    if plan is not None:
        lines += [
            "",
            f"Would have posted: **{len(plan.alarms)}** alarm(s), **{len(plan.roundup)}** roundup "
            f"entr{'y' if len(plan.roundup) == 1 else 'ies'} (nothing was posted).",
        ]
    if result.warnings:
        lines += ["", "### Warnings", ""] + [f"- {_cell(w, 400)}" for w in result.warnings[:20]]
    if posted_test:
        lines += ["", f"Discord: {posted_test}"]
    text = "\n".join(lines) + "\n"
    if len(text.encode()) > SUMMARY_LIMIT:
        text = text.encode()[:SUMMARY_LIMIT].decode(errors="ignore") + "\n\n_(summary truncated)_\n"
    return text


def _load_state_copy(state_dir: Path | None) -> State:
    """Read the real state (if any) from a throwaway copy, so the smoke run can't modify it."""
    if state_dir is None or not state_dir.exists():
        return State()
    with tempfile.TemporaryDirectory(prefix="gembot-smoke-") as tmp:
        copy = Path(tmp) / "state"
        shutil.copytree(state_dir, copy, ignore=shutil.ignore_patterns(".git"))
        return StateStore(copy).load()


def post_test_alarm(config: Config, state: State, *, now: datetime, http) -> str:
    from gembot.discord.publish import Publisher
    from gembot.discord.setup import make_test_card
    from gembot.runner import make_discord

    api = make_discord(config, http)
    if api is None:
        return "TEST alarm skipped: DISCORD_BOT_TOKEN is not set."
    channels = state.meta.discord.channels
    if "alarm" not in channels:
        return "TEST alarm skipped: no channel ids yet - run the 'Setup GemBot' workflow first."
    publisher = Publisher(api, channels, config.settings, sleep=http.sleep)
    message = publisher.post_alarm(make_test_card(now), _zero_features(), now)
    return f"posted a TEST alarm (message {message.message_id}) to #{config.settings.discord.alarm_channel}."


def _zero_features():
    from gembot.models import Features

    return Features()


def run_smoke(
    config: Config,
    *,
    now: datetime,
    post_test: bool = False,
    state_dir: Path | None = None,
    summary_path: Path | None = None,
    transport=None,
    sleep=time.sleep,
) -> int:
    from gembot.runner import make_http

    state = _load_state_copy(state_dir)
    started = time.monotonic()
    with make_http(config, state, now=now, transport=transport, sleep=sleep) as http:
        pipeline = Pipeline(config, state, http=http, now=now, discord=None, post=False, sleep=sleep)
        result = pipeline.run()
        posted = None
        if post_test:
            try:
                posted = post_test_alarm(config, state, now=now, http=http)
            except Exception as exc:  # report, don't crash the summary
                posted = f"TEST alarm FAILED: {exc}"
    seconds = time.monotonic() - started
    text = render_summary(result, seconds=seconds, posted_test=posted)
    print(text)
    if summary_path is not None:
        try:
            with summary_path.open("a", encoding="utf-8") as handle:
                handle.write(text)
        except OSError as exc:
            log.warning("could not write the job summary: %s", exc)
    enabled = [r for r in result.reports.values() if not r.skipped]
    if enabled and all(not r.ok for r in enabled):
        log.error("every enabled source failed")
        return 1
    if posted and "FAILED" in posted:
        return 1
    return 0
