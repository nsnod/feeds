"""GemBot command line.

python -m gembot run [--dry-run] [--state-dir state] [--no-push]
python -m gembot setup [--state-dir state] [--force-welcome] [--skip-gateway]
python -m gembot smoke [--post-test] [--summary PATH]
python -m gembot connect-once
python -m gembot replay tests/fixtures/scenario_day/ [--check | --update-golden]
python -m gembot init-state [--repo-dir .]   # prints the state branch name
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from gembot.config import ConfigError, load_config

log = logging.getLogger("gembot")


def _parse_now(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx logs every request URL at INFO; keep the run log readable.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("discord").setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gembot", description="Find hidden-gem indie co-op games.")
    parser.add_argument("--config-dir", type=Path, default=None, help="directory with settings.yaml etc.")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="one scan: collect, score, post, learn, save state")
    run.add_argument("--state-dir", type=Path, default=Path("state"))
    run.add_argument("--dry-run", action="store_true", help="temp state copy, no Discord posts, no push")
    run.add_argument("--no-push", action="store_true", help="save state but do not git push")
    run.add_argument("--now", help="override the clock (ISO time, for debugging)")

    setup = sub.add_parser("setup", help="create channels, post the welcome + test alarm")
    setup.add_argument("--state-dir", type=Path, default=Path("state"))
    setup.add_argument("--force-welcome", action="store_true")
    setup.add_argument("--skip-gateway", action="store_true", help="skip the one-time gateway connection")
    setup.add_argument("--no-push", action="store_true")

    smoke = sub.add_parser("smoke", help="hit every real source once and print a summary (no state writes)")
    smoke.add_argument("--post-test", action="store_true", help="also post a TEST alarm to Discord")
    smoke.add_argument("--state-dir", type=Path, default=Path("state"), help="read-only: channel ids")
    smoke.add_argument("--summary", type=Path, default=None, help="append the markdown summary here")

    sub.add_parser("connect-once", help="connect to the Discord gateway once, then disconnect")

    replay = sub.add_parser("replay", help="run the whole pipeline offline over a recorded day")
    replay.add_argument("directory", type=Path)
    mode = replay.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="fail if the output differs from expected.json")
    mode.add_argument("--update-golden", action="store_true", help="rewrite expected.json")

    init = sub.add_parser("init-state", help="create the orphan bot-state branch if it is missing")
    init.add_argument("--repo-dir", type=Path, default=Path("."))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        config = load_config(args.config_dir)
    except ConfigError as exc:
        log.error("configuration problem: %s", exc)
        return 2

    if args.command == "run":
        from gembot.runner import run_scan

        result = run_scan(
            config,
            state_dir=args.state_dir,
            now=_parse_now(args.now),
            dry_run=args.dry_run,
            push=not (args.no_push or args.dry_run),
        )
        for warning in result.warnings:
            log.warning(warning)
        if result.state_push_failed:
            # fail the job: unsaved state means the next run would post the same alarms again
            log.error("the state was not pushed to the %s branch", config.settings.state.branch)
            return 1
        return 0

    if args.command == "setup":
        from gembot.runner import run_setup_command

        return run_setup_command(
            config,
            state_dir=args.state_dir,
            now=_parse_now(None),
            force_welcome=args.force_welcome,
            gateway=not args.skip_gateway,
            push=not args.no_push,
        )

    if args.command == "smoke":
        from gembot.smoke import run_smoke

        return run_smoke(
            config,
            now=_parse_now(None),
            post_test=args.post_test,
            state_dir=args.state_dir,
            summary_path=args.summary or _env_path("GITHUB_STEP_SUMMARY"),
        )

    if args.command == "connect-once":
        from gembot.discord.setup import connect_gateway_once

        token = config.secrets.discord_bot_token
        if not token:
            log.error("DISCORD_BOT_TOKEN is not set")
            return 2
        return 0 if connect_gateway_once(token) else 1

    if args.command == "replay":
        from gembot.replay import run_replay

        return run_replay(
            args.directory, config_dir=args.config_dir, check=args.check, update=args.update_golden
        )

    if args.command == "init-state":
        from gembot.state.store import state_branch_status

        branch = config.settings.state.branch
        status = state_branch_status(args.repo_dir, branch=branch)
        if status == "error":
            log.error("could not check or create the %s branch (see above)", branch)
            return 1
        log.info("state branch %s: %s", branch, status)
        print(branch)  # stdout is just the branch name, for the workflow to use
        return 0

    return 2  # pragma: no cover - argparse enforces the choices


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


if __name__ == "__main__":
    sys.exit(main())
