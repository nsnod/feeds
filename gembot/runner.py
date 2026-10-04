"""Glue between the CLI, the state directory (bot-state branch) and the pipeline."""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import httpx

from gembot.config import Config
from gembot.discord.rest import DiscordAPI, DiscordClient
from gembot.http import Budget, HttpClient
from gembot.models import State
from gembot.pipeline import Pipeline, RunResult
from gembot.state.store import GitStateRepo, StateStore, prune

log = logging.getLogger("gembot.runner")


def make_http(
    config: Config,
    state: State | None,
    *,
    now: datetime,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> HttpClient:
    run = config.settings.run
    return HttpClient(
        user_agent=run.user_agent,
        timeout=run.http_timeout_s,
        retries=run.http_retries,
        max_backoff_s=run.max_backoff_s,
        transport=transport,
        sleep=sleep,
        cache=state.meta.http_cache if state is not None else {},
        clock=lambda: now,
    )


def make_discord(config: Config, http: HttpClient) -> DiscordAPI | None:
    token = config.secrets.discord_bot_token
    if not token:
        return None
    settings = config.settings
    return DiscordClient(
        token, http, Budget("discord", settings.budgets.discord), api_base=settings.discord.api_base
    )


def _is_git_checkout(path: Path) -> bool:
    return (path / ".git").exists()


def run_scan(
    config: Config,
    *,
    state_dir: Path,
    now: datetime,
    dry_run: bool = False,
    push: bool = True,
    transport: httpx.BaseTransport | None = None,
    discord: DiscordAPI | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> RunResult:
    """Load state, run the pipeline once, save (and push) state."""
    tmp: tempfile.TemporaryDirectory[str] | None = None
    work_dir = state_dir
    if dry_run:
        tmp = tempfile.TemporaryDirectory(prefix="gembot-dry-")
        work_dir = Path(tmp.name) / "state"
        if state_dir.exists():
            shutil.copytree(state_dir, work_dir, ignore=shutil.ignore_patterns(".git"))
        push = False
    try:
        store = StateStore(work_dir)
        state = store.load()
        pruned = prune(state, now, config.settings.state, config.settings.features.baseline_window_days)
        if any(pruned.values()):
            log.info("pruned: %s", {k: v for k, v in pruned.items() if v})
        with make_http(config, state, now=now, transport=transport, sleep=sleep) as http:
            api = None if dry_run else (discord or make_discord(config, http))
            pipeline = Pipeline(config, state, http=http, now=now, discord=api, post=not dry_run, sleep=sleep)
            result = pipeline.run()
        _log_summary(result)
        # what this run added counts toward the size cap too
        prune(state, now, config.settings.state, config.settings.features.baseline_window_days)
        store.save(state)
        log.info("state size: %.1f KB", store.size_bytes() / 1024)
        if push:
            if _is_git_checkout(state_dir):
                repo = GitStateRepo(state_dir, branch=config.settings.state.branch)
                ok = repo.commit_and_push(
                    f"state: run {now:%Y-%m-%dT%H:%M:%SZ}",
                    reapply=lambda: store.save(state),
                    retries=config.settings.state.push_retries,
                )
                if not ok:
                    result.warnings.append("could not push state to the bot-state branch (see log)")
            else:
                log.info("%s is not a git checkout; state saved locally only", state_dir)
        return result
    finally:
        if tmp is not None:
            tmp.cleanup()


def _log_summary(result: RunResult) -> None:
    plan = result.plan
    log.info(
        "run summary: %d mentions (%d new), %d games scored, %d alarm(s), %d roundup entr%s, %d message(s) posted",
        result.collected,
        result.new_mentions,
        len(result.results),
        len(plan.alarms) if plan else 0,
        len(plan.roundup) if plan else 0,
        "y" if plan and len(plan.roundup) == 1 else "ies",
        len(result.posted),
    )
    for item in result.top(5):
        log.info("  %5.1f  %s  %s", item.score, item.game_id, (item.reasons or [""])[0])


def run_setup_command(
    config: Config,
    *,
    state_dir: Path,
    now: datetime,
    force_welcome: bool = False,
    gateway: bool = True,
    push: bool = True,
    discord: DiscordAPI | None = None,
    transport: httpx.BaseTransport | None = None,
) -> int:
    from gembot.discord.setup import SetupError, connect_gateway_once, run_setup

    token = config.secrets.discord_bot_token
    if not token and discord is None:
        log.error(
            "DISCORD_BOT_TOKEN is not set. Add it under Settings -> Secrets and variables -> Actions "
            "(see README.md, step 2)."
        )
        return 2
    if gateway and token:
        if connect_gateway_once(token):
            log.info("connected to the Discord gateway once (needed before a new bot can post)")
        else:
            log.warning("could not connect to the Discord gateway; continuing with REST")
    store = StateStore(state_dir)
    state = store.load()
    with make_http(config, state, now=now, transport=transport) as http:
        api = discord or make_discord(config, http)
        assert api is not None
        try:
            result = run_setup(api, config, state, now=now, force_welcome=force_welcome)
        except SetupError as exc:
            log.error("%s", exc)
            return 1
    log.info(
        "server %s: channels %s (created: %s)", result.guild_id, result.channels, result.created or "none"
    )
    store.save(state)
    discord_meta = state.meta.discord.model_copy(deep=True)

    def reapply() -> None:
        # Someone else (a scan) pushed first: keep their state, re-apply only our channel ids.
        fresh = store.load()
        fresh.meta.discord = discord_meta
        store.save(fresh)

    if push and _is_git_checkout(state_dir):
        repo = GitStateRepo(state_dir, branch=config.settings.state.branch)
        if not repo.commit_and_push(f"state: setup {now:%Y-%m-%dT%H:%M:%SZ}", reapply=reapply, retries=3):
            log.error("could not push the channel ids to the bot-state branch")
            return 1
    return 0
