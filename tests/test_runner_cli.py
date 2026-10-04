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
from gembot.config import load_config
from gembot.discord.fake import FakeDiscord
from gembot.replay import ReplayTransport, Route, load_routes
from gembot.runner import make_discord, run_scan, run_setup_command
from gembot.smoke import render_summary, run_smoke
from gembot.state.store import StateStore
from tests.factories import (
    INCIDENT_FEEDS,
    NOW,
    TEST_CONFIG_DIR,
    config_dir_with_feeds,
    make_config,
    make_http,
)

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


def test_render_summary_lists_every_feed_below_the_sources():
    from gembot.collectors.base import FeedResult, SourceReport
    from gembot.pipeline import RunResult

    result = RunResult(now=NOW)
    result.reports["steam"] = SourceReport("steam", requests=3, mentions=40, ok_units=3)
    result.reports["rss"] = SourceReport(
        "rss",
        requests=2,
        mentions=5,
        ok_units=2,
        errors=["config/feeds.yaml: line 38: 'feeds' appears again (first on line 22)"],
        config_problems=["config/feeds.yaml: line 38: 'feeds' appears again (first on line 22)"],
        feed_results=[
            FeedResult(
                "config/feeds.yaml", "", "config", note="line 38: 'feeds' appears again (first on line 22)"
            ),
            FeedResult("GameGil | IG", "instagram", "ok", items=5),
            FeedResult("Blog", "rss", "warning", items=0, note="malformed feed, kept what could be read"),
            FeedResult("KreekCraft", "youtube", "error", note="HTTP 404"),
            FeedResult("Old", "rss", "paused", note="enabled: false in feeds.yaml"),
            FeedResult("Second IG", "instagram", "skipped", note="not requested: rss.app answered HTTP 429"),
        ],
    )
    text = render_summary(result, seconds=1.0)
    assert '| rss | ❌ config problem | 2 | 5 | 1 mistake(s) to fix, see "Your feeds" below |' in text
    assert "1 source(s) failed." in text
    table = text.split("### Your feeds\n\n", 1)[1].split("\n\n", 1)[0].splitlines()
    assert table == [
        "| Feed | Platform | Status | Items | Note |",
        "|---|---|---|---:|---|",
        "| config/feeds.yaml |  | ❌ fix feeds.yaml |  | line 38: 'feeds' appears again (first on line 22) |",
        "| GameGil \\| IG | instagram | ✅ ok | 5 |  |",
        "| Blog | rss | ⚠️ warning | 0 | malformed feed, kept what could be read |",
        "| KreekCraft | youtube | ❌ error |  | HTTP 404 |",
        "| Old | rss | ⏸️ paused |  | enabled: false in feeds.yaml |",
        "| Second IG | instagram | ⏭️ skipped |  | not requested: rss.app answered HTTP 429 |",
    ]
    assert text.index("| Source |") < text.index("### Your feeds") < text.index("### Top 10 scored games")
    # no feeds, no table; a config mistake without feed rows quotes the last errors
    other = RunResult(now=NOW)
    other.reports["x"] = SourceReport("x", errors=["first", "second", "last"], config_problems=["last"])
    text = render_summary(other, seconds=1.0)
    assert "| x | ❌ config problem | 0 | 0 | second; last |" in text and "### Your feeds" not in text


def test_the_readme_explains_every_status_a_smoke_run_shows():
    """README -> "Read a smoke run" has a row for each status of the sources table, and mentions
    the "Your feeds" table and each of its statuses."""
    from gembot.collectors.base import FeedResult, SourceReport
    from gembot.pipeline import RunResult
    from gembot.smoke import FEED_STATUS

    result = RunResult(now=NOW)
    for report in (
        SourceReport("a", ok_units=1),
        SourceReport("b", errors=["x"], ok_units=1, failed_units=1),
        SourceReport("c", errors=["x"], failed_units=1),
        SourceReport("d", errors=["x"], config_problems=["x"], feed_results=[FeedResult("f", "", "config")]),
        SourceReport("e", skipped=True, skip_reason="off"),
    ):
        result.reports[report.source] = report
    rows = [line.split(" | ") for line in render_summary(result, seconds=1.0).splitlines()]
    statuses = [row[1] for row in rows if row[0] in ("| a", "| b", "| c", "| d", "| e")]
    assert statuses == ["✅ ok", "⚠️ partial", "❌ failed", "❌ config problem", "⏭️ skipped"]
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    section = readme.split("### Read a smoke run", 1)[1].split("\n### ", 1)[0]
    for status in statuses:
        assert f"| {status} |" in section
    assert "**Your feeds** table" in section
    for status in FEED_STATUS:
        assert status.replace("config", "fix feeds.yaml") in section


def test_the_feeds_table_is_capped():
    from gembot.collectors.base import FeedResult, SourceReport
    from gembot.pipeline import RunResult
    from gembot.smoke import MAX_FEED_ROWS

    result = RunResult(now=NOW)
    rows = [FeedResult(f"feed {i}", "rss", "error", note="x" * 5000) for i in range(MAX_FEED_ROWS + 50)]
    result.reports["rss"] = SourceReport("rss", errors=["x"], feed_results=rows)
    text = render_summary(result, seconds=1.0)
    assert text.count("| ❌ error |") == MAX_FEED_ROWS and "…and 50 more row(s)" in text
    assert len(text.encode()) < 100_000  # notes are clipped per cell


def test_smoke_with_the_incident_feeds_file_still_reads_every_other_source(tmp_path):
    config = load_config(config_dir_with_feeds(tmp_path, INCIDENT_FEEDS), env={})
    summary = tmp_path / "summary.md"
    run_smoke(config, now=NOW, summary_path=summary, transport=offline(), sleep=lambda s: None)
    text = summary.read_text()
    sources = text.split("### Your feeds", 1)[0]
    for name in ("steam", "reddit", "itch", "bluesky"):
        assert f"| {name} | " in sources  # every source still ran (and failed politely, offline)
    assert '| rss | ❌ config problem | 2 | 0 | 3 mistake(s) to fix, see "Your feeds" below |' in sources
    assert "| KreekCraft (YouTube) | youtube | ❌ fix feeds.yaml |  | channel_id is 26 characters;" in text
    assert (
        "| config/feeds.yaml |  | ❌ fix feeds.yaml |  | line 38: an extra 'feeds:' line with no feed in it;"
        in text
    )
    assert (
        "| GameGil (@officialgamegil) | instagram | ❌ error |  | rss: HTTP 404 for https://rss.app/" in text
    )


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
    assert run_replay(tmp_path, config_dir=TEST_CONFIG_DIR) == 0


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


def test_check_config_reports_problems_and_lists_feeds(tmp_path, capsys):
    (tmp_path / "feeds.yaml").write_text(
        "feeds:\n  - name: A\n    url: https://rss.app/feeds/abc.xml\n    source: instagram\n"
        "  - name: B\n    url: https://example.com/feed.xml\n    enabled: false\n"
    )
    assert cli.main(["--config-dir", str(tmp_path), "check-config"]) == 0
    out = capsys.readouterr().out
    assert "config OK" in out and "2 feed(s)" in out and "B [rss] (paused)" in out
    (tmp_path / "feeds.yaml").write_text("feeds:\n  - name: A\n    url: https://x/feed\nfeeds: [2]\n")
    assert cli.main(["--config-dir", str(tmp_path), "check-config"]) == 1
    out = capsys.readouterr().out
    assert "config problem: feeds.yaml: line 4: an extra 'feeds:' line with no feed in it" in out
    assert "(until then the scan keeps reading the 1 feed(s) that work)" in out
    assert "1 problem(s) in" in out and "1 feed(s) loaded:\n  - A [rss]: https://x/feed" in out
    assert "config OK" not in out
    (tmp_path / "feeds.yaml").write_text("feeds:\n  - name: A\n    url: https://rss.app/feed/abc\n  - 2\n")
    assert cli.main(["--config-dir", str(tmp_path), "check-config"]) == 1
    out = capsys.readouterr().out
    assert "2 problem(s) in" in out and "(until then no feed is read)" in out  # not "keeps reading"
    (tmp_path / "settings.yaml").write_text("run: {}\nrun: {}\n")
    assert cli.main(["--config-dir", str(tmp_path), "check-config"]) == 1
    assert "config problem: settings.yaml: 'run' appears twice (lines 1 and 2)" in capsys.readouterr().out


def test_check_config_on_the_incident_file_lists_every_mistake_and_fails(tmp_path, capsys):
    directory = config_dir_with_feeds(tmp_path, INCIDENT_FEEDS)
    assert cli.main(["--config-dir", str(directory), "check-config"]) == 1
    out = capsys.readouterr().out
    problems = [line for line in out.splitlines() if line.startswith("config problem: ")]
    assert len(problems) == 3
    assert problems[0].startswith('config problem: feeds.yaml: line 28: feed #1 "GameGil (@officialgamegil)"')
    assert "unknown key 'feeds' ignored" in problems[0]
    assert problems[1] == (
        "config problem: feeds.yaml: line 38: an extra 'feeds:' line with no feed in it; ignored - delete line 38 "
        "and keep the one on line 22"
    )
    assert problems[2].startswith(
        "config problem: feeds.yaml: feed 'KreekCraft (YouTube)': channel_id is 26 characters; YouTube "
        "channel ids are 24 and start with UC (was 'UC' pasted twice?)"
    )
    assert "3 feed(s) loaded:" in out and "config OK" not in out
    assert "  - KreekCraft (YouTube) [youtube] (URL problem, see above): " in out
    assert "  - Hellmei (@Hellmeitv) [instagram]: https://rss.app/feeds/5KcRbde1HFqAzPdx.xml" in out


def test_check_config_lints_enabled_feeds_offline(tmp_path, capsys, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("check-config must not make requests")

    monkeypatch.setattr(httpx.Client, "send", no_network)
    directory = config_dir_with_feeds(
        tmp_path,
        "feeds:\n"
        "  - {name: Page, url: 'https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv', source: youtube}\n"
        "  - {name: Viewer, url: 'https://rss.app/feed/AbCdEf', source: instagram, enabled: false}\n",
    )
    assert cli.main(["--config-dir", str(directory), "check-config"]) == 0  # a paused feed is not linted
    out = capsys.readouterr().out
    assert "config OK" in out and "2 feed(s)" in out
    assert (
        "  - Page [youtube] (works, but: https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv is a channel page"
        in out
    )
    assert "  - Viewer [instagram] (paused): https://rss.app/feed/AbCdEf" in out
    (directory / "feeds.yaml").write_text("feeds:\n  - {name: Viewer, url: 'https://rss.app/feed/AbCdEf'}\n")
    assert cli.main(["--config-dir", str(directory), "check-config"]) == 1
    assert "config problem: feeds.yaml: feed 'Viewer': use the https://rss.app/feeds/AbCdEf.xml RSS URL" in (
        capsys.readouterr().out
    )


@pytest.mark.parametrize(
    ("url", "problem"),
    [
        (  # the account's address from README step 2 used to print "config OK"
            '"https://www.instagram.com/hellmeitv/"',
            "this is an Instagram profile page, not a feed: make an RSS.app feed for that account",
        ),
        ('"https://www.tiktok.com/@hellmeitv"', "this is a TikTok profile page, not a feed"),
        (
            "rss.app/feeds/5KcRbde1HFqAzPdx.xml",
            "not an http(s) URL: add https:// in front - use https://rss.app/feeds/5KcRbde1HFqAzPdx.xml",
        ),
        ("“https://rss.app/feeds/5KcRbde1HFqAzPdx.xml”", "replace the curly quotes with straight ones"),
    ],
)
def test_check_config_fails_on_a_url_that_is_not_a_feed_and_says_what_to_change(
    tmp_path, capsys, url, problem
):
    directory = config_dir_with_feeds(
        tmp_path,
        "feeds:\n"
        '  - name: "GameGil"\n    url: "https://rss.app/feeds/K1vwmXudAkt1exqO.xml"\n    source: instagram\n'
        f'  - name: "Hellmei"\n    url: {url}\n    source: instagram\n',
    )
    assert cli.main(["--config-dir", str(directory), "check-config"]) == 1
    out = capsys.readouterr().out
    assert f"config problem: feeds.yaml: feed 'Hellmei': {problem}" in out
    assert "(until then the scan keeps reading the 1 feed(s) that work)" in out and "config OK" not in out


def test_a_scan_with_the_incident_file_runs_instead_of_stopping(tmp_path, monkeypatch):
    seen: dict = {}

    def fake_run_scan(config, **kw):
        from gembot.pipeline import RunResult

        seen["feeds"] = [feed.name for feed in config.feeds.feeds]
        return RunResult(now=kw["now"])

    monkeypatch.setattr("gembot.runner.run_scan", fake_run_scan)
    directory = config_dir_with_feeds(tmp_path, INCIDENT_FEEDS)
    assert cli.main(["--config-dir", str(directory), "run", "--dry-run"]) == 0  # used to exit 2
    assert seen["feeds"] == ["GameGil (@officialgamegil)", "Hellmei (@Hellmeitv)", "KreekCraft (YouTube)"]


def test_replay_does_not_need_a_valid_user_config(tmp_path, monkeypatch):
    (tmp_path / "settings.yaml").write_text("decisions: {alarm_scor: 1}\n")  # broken on purpose
    monkeypatch.setenv("GEMBOT_CONFIG_DIR", str(tmp_path))
    scenario = TEST_CONFIG_DIR.parent / "scenario_day"
    assert cli.main(["replay", str(scenario), "--check"]) == 0
