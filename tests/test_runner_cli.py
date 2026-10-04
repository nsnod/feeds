"""Runner (state dir + git), CLI, smoke command and replay plumbing. No network: every HTTP
request goes to a MockTransport."""

from __future__ import annotations

import json
import subprocess
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from gembot import __main__ as cli
from gembot.discord.fake import FakeDiscord
from gembot.replay import ReplayTransport, Route, load_routes
from gembot.runner import make_discord, run_scan, run_setup_command
from gembot.smoke import render_summary, run_smoke
from gembot.state.store import StateStore
from tests.factories import NOW, make_config, make_http

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def offline(status: int = 404) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(status, text="offline", request=request))


def git(cwd: Path, *args: str) -> str:
    import os

    env = {**os.environ, **GIT_ENV}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


# ----------------------------------------------------------------------------- run_scan


def test_dry_run_uses_a_copy_and_never_touches_the_state_dir(tmp_path):
    state_dir = tmp_path / "state"
    StateStore(state_dir).save(StateStore(state_dir).load())
    before = {p.name: p.read_bytes() for p in state_dir.iterdir()}
    result = run_scan(
        make_config(), state_dir=state_dir, now=NOW, dry_run=True, transport=offline(), sleep=lambda s: None
    )
    after = {p.name: p.read_bytes() for p in state_dir.iterdir()}
    assert before == after
    assert result.reports  # every collector ran (and failed politely against the offline transport)
    assert all(not r.ok or r.skipped or not r.errors for r in result.reports.values())


def test_scan_saves_state_locally_without_git(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMBOT_DEFAULT_BRANCH_COMMIT_TS", "")
    state_dir = tmp_path / "state"
    result = run_scan(
        make_config(), state_dir=state_dir, now=NOW, push=True, transport=offline(), sleep=lambda s: None
    )
    assert (state_dir / "meta.json").exists()
    meta = StateStore(state_dir).load().meta
    assert meta.run_count == 1 and meta.last_run_at == NOW
    assert result.warnings == [] or all("Discord" in w for w in result.warnings)


def test_scan_commits_and_pushes_to_the_state_branch(tmp_path, monkeypatch):
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    state_dir = tmp_path / "state"
    git(tmp_path, "init", "-q", "-b", "bot-state", str(state_dir))
    (state_dir / "README.md").write_text("state\n")
    git(state_dir, "add", "-A")
    git(state_dir, "commit", "-q", "-m", "init")
    git(state_dir, "remote", "add", "origin", str(remote))
    git(state_dir, "push", "-q", "origin", "HEAD:refs/heads/bot-state")
    run_scan(make_config(), state_dir=state_dir, now=NOW, transport=offline(), sleep=lambda s: None)
    log = git(state_dir, "log", "--format=%s", "origin/bot-state")
    assert "state: run 2026-10-03T12:00:00Z" in log.splitlines()[0]
    files = git(state_dir, "ls-tree", "--name-only", "origin/bot-state").split()
    assert {"meta.json", "games.json", "README.md"} <= set(files)


def test_scan_reports_a_failed_state_push(tmp_path, monkeypatch):
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    state_dir = tmp_path / "state"
    git(tmp_path, "init", "-q", "-b", "bot-state", str(state_dir))
    git(state_dir, "remote", "add", "origin", str(tmp_path / "missing.git"))  # nowhere to push
    result = run_scan(make_config(), state_dir=state_dir, now=NOW, transport=offline(), sleep=lambda s: None)
    assert result.state_push_failed
    assert any("could not push state" in w for w in result.warnings)
    assert (state_dir / "meta.json").exists()  # still saved locally


def test_make_discord_needs_a_token():
    assert make_discord(make_config(), make_http()) is None
    config = make_config(env={"DISCORD_BOT_TOKEN": "abc"})
    assert make_discord(config, make_http()) is not None


# ----------------------------------------------------------------------------- setup command


def test_setup_command_creates_channels_and_is_idempotent(tmp_path):
    fake = FakeDiscord(guilds=["55"])
    config = make_config(env={"DISCORD_BOT_TOKEN": "abc"})
    state_dir = tmp_path / "state"
    code = run_setup_command(config, state_dir=state_dir, now=NOW, gateway=False, discord=fake, push=False)
    assert code == 0
    channels = StateStore(state_dir).load().meta.discord.channels
    assert set(channels) == {"alarm", "roundup", "status"}
    sent = len(fake.sent)
    assert (
        run_setup_command(config, state_dir=state_dir, now=NOW, gateway=False, discord=fake, push=False) == 0
    )
    assert len(fake.sent) == sent  # no duplicate welcome
    assert len(fake.channels) == 4  # category + 3 channels, still


def test_setup_command_errors(tmp_path):
    assert run_setup_command(make_config(), state_dir=tmp_path / "s", now=NOW, gateway=False) == 2
    fake = FakeDiscord(guilds=["1", "2"])  # two servers and no DISCORD_GUILD_ID
    config = make_config(env={"DISCORD_BOT_TOKEN": "abc"})
    assert run_setup_command(config, state_dir=tmp_path / "s", now=NOW, gateway=False, discord=fake) == 1


def test_setup_command_tries_the_gateway_once(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "gembot.discord.setup.connect_gateway_once", lambda token: calls.append(token) or False
    )
    fake = FakeDiscord(guilds=["55"])
    config = make_config(env={"DISCORD_BOT_TOKEN": "abc"})
    assert run_setup_command(config, state_dir=tmp_path / "s", now=NOW, discord=fake, push=False) == 0
    assert calls == ["abc"]


# ----------------------------------------------------------------------------- smoke


def test_smoke_summary_table_and_exit_codes(tmp_path):
    summary = tmp_path / "summary.md"
    code = run_smoke(
        make_config(),
        now=NOW,
        state_dir=tmp_path / "none",
        summary_path=summary,
        transport=offline(),
        sleep=lambda s: None,
    )
    text = summary.read_text()
    assert "## GemBot smoke test" in text and "| Source | Status |" in text
    assert "⏭️ skipped" in text  # e.g. RSS without feeds, X without a token
    assert "❌ failed" in text
    assert code == 1  # every enabled source failed against the offline transport


def test_smoke_post_test_without_token_or_channels(tmp_path):
    summary = tmp_path / "s.md"
    run_smoke(
        make_config(),
        now=NOW,
        post_test=True,
        summary_path=summary,
        transport=offline(),
        sleep=lambda s: None,
    )
    assert "TEST alarm skipped: DISCORD_BOT_TOKEN" in summary.read_text()
    config = make_config(env={"DISCORD_BOT_TOKEN": "abc"})
    run_smoke(
        config, now=NOW, post_test=True, summary_path=summary, transport=offline(), sleep=lambda s: None
    )
    assert "run the 'Setup GemBot' workflow first" in summary.read_text()


def test_smoke_post_test_posts_to_the_alarm_channel(tmp_path):
    state_dir = tmp_path / "state"
    store = StateStore(state_dir)
    state = store.load()
    state.meta.discord.channels = {"alarm": "11", "roundup": "12", "status": "13"}
    store.save(state)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "discord.com":
            if request.method == "POST":
                return httpx.Response(200, json={"id": "999", "channel_id": "11", "embeds": [{}]})
            return httpx.Response(204)
        return httpx.Response(404)

    config = make_config(env={"DISCORD_BOT_TOKEN": "abc"})
    summary = tmp_path / "s.md"
    run_smoke(
        config,
        now=NOW,
        post_test=True,
        state_dir=state_dir,
        summary_path=summary,
        transport=httpx.MockTransport(handler),
        sleep=lambda s: None,
    )
    assert "posted a TEST alarm (message 999)" in summary.read_text()
    posts = [r for r in seen if r.method == "POST" and r.url.host == "discord.com"]
    assert posts and posts[0].url.path.endswith("/channels/11/messages")
    body = json.loads(posts[0].content)
    assert "TEST" in body["embeds"][0]["title"]
    # the real state dir was never modified by the smoke run
    assert StateStore(state_dir).load().meta.run_count == 0


def test_render_summary_names_games_and_shows_the_thresholds():
    from gembot.models import Features, ScoreResult
    from gembot.pipeline import RunResult

    result = RunResult(now=NOW)
    result.results = {
        "steam:1": ScoreResult(game_id="steam:1", score=39.8, features=Features(), reasons=["#1 on Steam"]),
        "steam:2": ScoreResult(game_id="steam:2", score=30.0, features=Features(), reasons=[]),
    }
    text = render_summary(result, seconds=1.0, titles={"steam:1": "Moon | Soup"}, thresholds=(45, 72))
    assert "| 39.8 | Moon \\| Soup (`steam:1`) | #1 on Steam |" in text
    assert "| 30.0 | `steam:2` |" in text
    assert "needs **45**+, an alarm **72**+" in text


def test_render_summary_truncates_huge_output():
    from gembot.collectors.base import SourceReport
    from gembot.pipeline import RunResult

    result = RunResult(now=NOW)
    for i in range(3000):
        result.reports[f"s{i}"] = SourceReport(f"s{i}", errors=["x" * 300])
    text = render_summary(result, seconds=1.0)
    assert len(text.encode()) < 1_000_000 and "truncated" in text


# ----------------------------------------------------------------------------- CLI


def test_cli_dispatches_every_command(monkeypatch, tmp_path, capsys):
    calls: dict[str, dict] = {}

    def fake_run_scan(config, **kw):
        from gembot.pipeline import RunResult

        calls["run"] = kw
        result = RunResult(now=kw["now"])
        result.warnings.append("just a warning")
        return result

    monkeypatch.setattr("gembot.runner.run_scan", fake_run_scan)
    monkeypatch.setattr(
        "gembot.runner.run_setup_command", lambda config, **kw: calls.setdefault("setup", kw) and 0
    )
    monkeypatch.setattr("gembot.smoke.run_smoke", lambda config, **kw: calls.setdefault("smoke", kw) and 0)
    monkeypatch.setattr(
        "gembot.replay.run_replay", lambda d, **kw: calls.setdefault("replay", {"dir": d, **kw}) and 0
    )
    monkeypatch.setattr("gembot.state.store.state_branch_status", lambda d, branch: "created")
    monkeypatch.setattr("gembot.discord.setup.connect_gateway_once", lambda token: True)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    assert cli.main(["run", "--dry-run", "--state-dir", str(tmp_path), "--now", "2026-10-03T12:00:00Z"]) == 0
    assert calls["run"]["dry_run"] and not calls["run"]["push"] and calls["run"]["now"] == NOW
    assert cli.main(["run", "--no-push"]) == 0 and calls["run"]["push"] is False
    assert cli.main(["setup", "--force-welcome", "--skip-gateway", "--no-push"]) == 0
    assert calls["setup"]["force_welcome"] and not calls["setup"]["gateway"] and not calls["setup"]["push"]
    assert cli.main(["smoke", "--post-test", "--summary", str(tmp_path / "s.md")]) == 0
    assert calls["smoke"]["post_test"] and calls["smoke"]["summary_path"] == tmp_path / "s.md"
    assert cli.main(["replay", str(tmp_path), "--check"]) == 0 and calls["replay"]["check"]
    capsys.readouterr()
    assert cli.main(["init-state", "--repo-dir", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == "bot-state"  # the workflow reads the branch name
    monkeypatch.setattr("gembot.state.store.state_branch_status", lambda d, branch: "error")
    assert cli.main(["init-state", "--repo-dir", str(tmp_path)]) == 1
    assert cli.main(["connect-once"]) == 2  # no token
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "abc")
    assert cli.main(["connect-once"]) == 0


def test_cli_run_fails_the_job_when_the_state_could_not_be_pushed(monkeypatch):
    def fake_run_scan(config, **kw):
        from gembot.pipeline import RunResult

        result = RunResult(now=kw["now"])
        result.state_push_failed = True
        return result

    monkeypatch.setattr("gembot.runner.run_scan", fake_run_scan)
    assert cli.main(["run"]) == 1


def test_cli_reports_config_errors(tmp_path):
    (tmp_path / "settings.yaml").write_text("decisions: {alarm_scor: 1}\n")
    assert cli.main(["--config-dir", str(tmp_path), "run", "--dry-run"]) == 2


def test_parse_now():
    assert cli._parse_now("2026-10-03T12:00:00Z") == NOW
    assert cli._parse_now("2026-10-03T12:00:00") == NOW
    assert cli._parse_now(None).tzinfo is not None


# ----------------------------------------------------------------------------- replay plumbing


def test_routes_and_transport(tmp_path):
    (tmp_path / "body.json").write_text('{"ok": true}')
    (tmp_path / "feed.xml").write_text("<rss/>")
    (tmp_path / "routes.yaml").write_text(
        "- {url: 'https://a.test/x', params: {q: '1'}, file: body.json}\n"
        "- {regex: 'b\\.test/.*\\.xml$', file: feed.xml, times: 1}\n"
        "- {url: 'https://c.test/', method: POST, json: {made: 1}, status: 201}\n"
        "- {url: 'https://d.test/', body: 'hello', headers: {X-Thing: 'y'}}\n"
    )
    routes = load_routes(tmp_path)
    assert routes[0].headers["content-type"] == "application/json"
    assert routes[1].headers["content-type"] == "application/xml"
    transport = ReplayTransport(routes)
    with httpx.Client(transport=transport) as client:
        assert client.get("https://a.test/x", params={"q": 1}).json() == {"ok": True}
        assert client.get("https://a.test/x", params={"q": 2}).status_code == 404
        assert client.get("https://b.test/f.xml").text == "<rss/>"
        assert client.get("https://b.test/f.xml").status_code == 404  # times: 1
        assert client.post("https://c.test/").status_code == 201
        assert client.get("https://c.test/").status_code == 404  # method mismatch
        assert client.get("https://d.test/").headers["x-thing"] == "y"
    assert len(transport.unmatched) == 3
    assert load_routes(tmp_path / "missing") == []
    assert Route(url="https://x").matches(httpx.Request("GET", "https://x/y"))


def test_replay_check_reports_missing_and_mismatched_golden(tmp_path, capsys):
    from gembot.replay import run_replay

    (tmp_path / "manifest.yaml").write_text(
        "start: '2026-10-03T06:00:00Z'\nruns:\n  - {at: '2026-10-03T06:07:00Z', responses: r1}\n"
    )
    (tmp_path / "r1").mkdir()
    assert run_replay(tmp_path, check=True) == 1
    assert "missing" in capsys.readouterr().out
    assert run_replay(tmp_path, update=True) == 0
    assert run_replay(tmp_path, check=True) == 0
    (tmp_path / "expected.json").write_text("{}\n")
    assert run_replay(tmp_path, check=True) == 1
    assert "differs" in capsys.readouterr().out
    assert run_replay(tmp_path, config_dir=Path("config")) == 0


def test_replay_reactions_need_an_existing_message(tmp_path):
    from gembot.replay import replay_day

    (tmp_path / "manifest.yaml").write_text(
        "start: '2026-10-03T06:00:00Z'\nruns:\n"
        "  - at: '2026-10-03T06:07:00Z'\n    responses: r1\n"
        "    reactions: [{kind: alarm, game: 'steam:1', emoji: '👍', users: 1}]\n"
    )
    with pytest.raises(ValueError, match="no alarm message"):
        replay_day(tmp_path)


def test_now_is_timezone_aware_in_replay_parse():
    from gembot.replay import _parse_time

    assert _parse_time("2026-10-03T12:00:00") == NOW
    assert _parse_time(NOW) == NOW
    assert _parse_time(NOW.replace(tzinfo=None)) == NOW
    assert _parse_time("2026-10-03T12:00:00Z") + timedelta(0) == NOW
