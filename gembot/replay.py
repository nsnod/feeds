"""``python -m gembot replay <dir>``: run the whole pipeline offline over a recorded "day".

A replay directory contains:

* ``manifest.yaml``  - the runs to simulate::

      start: "2026-10-03T06:00:00Z"        # setup time (channels are created in a fake Discord)
      env: {BLUESKY_HANDLE: ..., ...}      # optional fake secrets (never real ones)
      runs:
        - at: "2026-10-03T06:07:00Z"
          responses: run1                  # sub-directory with routes.yaml + recorded bodies
          reactions:                       # optional: humans react before this run starts
            - {kind: alarm, game: "steam:123", emoji: "👍", users: 2}

* ``<run>/routes.yaml`` - recorded HTTP responses, matched in order::

      - url: "https://www.reddit.com/r/"   # URL prefix (or `regex:` for a regular expression)
        params: {limit: "100"}             # optional: these query params must match
        method: GET                        # optional (default: any)
        status: 200
        file: reddit_new.rss               # body from a file in the same directory ...
        headers: {content-type: application/atom+xml}
      - url: "https://api.example/x"
        json: {...}                         # ... or an inline JSON body

  Anything not listed answers ``404`` (and is reported), so a replay never touches the network.

* ``expected.json`` - the golden output: every Discord payload GemBot sent, per run.

The state is saved to and re-loaded from a temporary directory between runs, exactly like
the real bot does with the ``bot-state`` branch.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from gembot.config import Config, load_config
from gembot.discord.fake import FakeDiscord
from gembot.discord.setup import run_setup
from gembot.pipeline import Pipeline
from gembot.runner import make_http
from gembot.state.store import StateStore, prune

log = logging.getLogger("gembot.replay")


def _parse_time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass
class Route:
    url: str | None = None
    regex: re.Pattern[str] | None = None
    method: str | None = None
    params: dict[str, str] = field(default_factory=dict)
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    times: int | None = None  # answer at most this many times (then fall through)
    used: int = 0

    def matches(self, request: httpx.Request) -> bool:
        if self.times is not None and self.used >= self.times:
            return False
        if self.method and request.method.upper() != self.method.upper():
            return False
        url = str(request.url)
        if self.regex is not None:
            if not self.regex.search(url):
                return False
        elif self.url is not None and not url.startswith(self.url):
            return False
        query = request.url.params
        return all(query.get(key) == str(value) for key, value in self.params.items())


def load_routes(run_dir: Path) -> list[Route]:
    spec_path = run_dir / "routes.yaml"
    if not spec_path.exists():
        return []
    specs = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or []
    routes: list[Route] = []
    for spec in specs:
        if "file" in spec:
            body = (run_dir / spec["file"]).read_bytes()
        elif "json" in spec:
            body = json.dumps(spec["json"]).encode()
        else:
            body = str(spec.get("body", "")).encode()
        headers = {str(k).lower(): str(v) for k, v in (spec.get("headers") or {}).items()}
        if "content-type" not in headers:
            name = str(spec.get("file", ""))
            if "json" in spec or name.endswith(".json"):
                headers["content-type"] = "application/json"
            elif name.endswith((".xml", ".rss", ".atom")):
                headers["content-type"] = "application/xml"
            elif name.endswith(".html"):
                headers["content-type"] = "text/html"
        routes.append(
            Route(
                url=spec.get("url"),
                regex=re.compile(spec["regex"]) if spec.get("regex") else None,
                method=spec.get("method"),
                params={str(k): str(v) for k, v in (spec.get("params") or {}).items()},
                status=int(spec.get("status", 200)),
                body=body,
                headers=headers,
                times=spec.get("times"),
            )
        )
    return routes


class ReplayTransport(httpx.BaseTransport):
    """Answers requests from recorded routes; unknown requests get 404 and are remembered."""

    def __init__(self, routes: list[Route]):
        self.routes = routes
        self.unmatched: list[str] = []
        self.requests: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url}")
        for route in self.routes:
            if route.matches(request):
                route.used += 1
                return httpx.Response(
                    route.status, content=route.body, headers=route.headers, request=request
                )
        self.unmatched.append(f"{request.method} {request.url}")
        return httpx.Response(404, text="not recorded", request=request)


def _no_sleep(_: float) -> None:
    return None


def _apply_reactions(fake: FakeDiscord, state, reactions: list[dict[str, Any]]) -> None:
    for spec in reactions:
        kind = spec.get("kind", "alarm")
        game = spec["game"]
        target = None
        for message in state.posted.messages.values():
            if message.kind == kind and any(entry.game_id == game for entry in message.entries):
                target = message
        if target is None:
            raise ValueError(f"replay: no {kind} message for {game} to react to")
        users = spec.get("users", 1)
        for i in range(int(users)):
            fake.react(target.channel_id, target.message_id, spec.get("emoji", "👍"), f"7000{i:04d}")


def replay_day(directory: Path, *, config: Config | None = None) -> dict[str, Any]:
    """Run every recorded run; return the golden-comparable output."""
    manifest = yaml.safe_load((directory / "manifest.yaml").read_text(encoding="utf-8"))
    env = {str(k): str(v) for k, v in (manifest.get("env") or {}).items()}
    if config is None:
        # A scenario pins its own config (manifest "config_dir", relative to the scenario), so
        # the user's editable config/ folder can't change a recorded day's golden output.
        local = directory / str(manifest.get("config_dir") or "config")
        fallback = os.environ.get("GEMBOT_CONFIG_DIR")  # the manifest env holds only fake secrets
        config = load_config(local if local.exists() else fallback, env=env)
    start = _parse_time(manifest["start"])
    fake = FakeDiscord(guilds=[str(manifest.get("guild_id", "424242424242424242"))])
    roles: dict[str, str] = {}
    output: dict[str, Any] = {"runs": []}
    with tempfile.TemporaryDirectory(prefix="gembot-replay-") as tmp:
        store = StateStore(Path(tmp) / "state")
        state = store.load()
        run_setup(fake, config, state, now=start)
        store.save(state)
        roles = {cid: role for role, cid in state.meta.discord.channels.items()}
        setup_messages = len(fake.sent)
        for run in manifest["runs"]:
            now = _parse_time(run["at"])
            state = store.load()
            _apply_reactions(fake, state, run.get("reactions") or [])
            transport = ReplayTransport(load_routes(directory / run.get("responses", "")))
            before = len(fake.sent)
            prune(state, now, config.settings.state, config.settings.features.baseline_window_days)
            with make_http(config, state, now=now, transport=transport, sleep=_no_sleep) as http:
                result = Pipeline(
                    config, state, http=http, now=now, discord=fake, sleep=_no_sleep, env={}
                ).run()
            prune(state, now, config.settings.state, config.settings.features.baseline_window_days)
            store.save(state)
            sent = fake.sent[before:]
            output["runs"].append(
                {
                    "at": now.isoformat().replace("+00:00", "Z"),
                    "posted": [{"channel": roles.get(cid, cid), "payload": payload} for cid, payload in sent],
                    "alarms": [d.game_id for d in (result.plan.alarms if result.plan else [])],
                    "roundup": [d.game_id for d in (result.plan.roundup if result.plan else [])],
                    "unmatched_requests": sorted(set(transport.unmatched)),
                }
            )
        output["setup_messages"] = setup_messages
        final = store.load()
        output["weights"] = {k: round(v, 4) for k, v in (final.weights.current or {}).items()}
        output["labels"] = len(final.labels)
    return output


def _dump(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def run_replay(
    directory: Path, *, config_dir: Path | None = None, check: bool = False, update: bool = False
) -> int:
    config = None
    if config_dir is not None:
        manifest = yaml.safe_load((directory / "manifest.yaml").read_text(encoding="utf-8"))
        env = {str(k): str(v) for k, v in (manifest.get("env") or {}).items()}
        config = load_config(config_dir, env=env)
    output = replay_day(directory, config=config)
    text = _dump(output)
    expected_path = directory / "expected.json"
    alarms = sum(len(run["alarms"]) for run in output["runs"])
    roundup = sum(len(run["roundup"]) for run in output["runs"])
    print(f"replayed {len(output['runs'])} runs: {alarms} alarm(s), {roundup} roundup entr(y/ies)")
    for run in output["runs"]:
        print(f"  {run['at']}: alarms={run['alarms']} roundup={run['roundup']}")
        for url in run["unmatched_requests"]:
            print(f"    (not recorded -> 404) {url}")
    if update:
        expected_path.write_text(text, encoding="utf-8")
        print(f"wrote {expected_path}")
        return 0
    if check:
        if not expected_path.exists():
            print(f"missing {expected_path}; run with --update-golden first")
            return 1
        if expected_path.read_text(encoding="utf-8") != text:
            import difflib

            diff = difflib.unified_diff(
                expected_path.read_text(encoding="utf-8").splitlines(),
                text.splitlines(),
                "expected.json",
                "actual",
                lineterm="",
            )
            print("\n".join(list(diff)[:200]))
            print("replay output differs from expected.json")
            return 1
        print("replay output matches expected.json")
    return 0
