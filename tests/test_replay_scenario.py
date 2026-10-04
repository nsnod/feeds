"""The recorded scenario day replays end to end and produces exactly the golden payloads."""

from __future__ import annotations

import json

import yaml

from gembot.config import load_config
from gembot.replay import replay_day, run_replay
from tests.factories import FIXTURES, INCIDENT_FEEDS, config_dir_with_feeds

SCENARIO = FIXTURES / "scenario_day"


def test_scenario_day_matches_golden():
    assert run_replay(SCENARIO, check=True) == 0


def test_scenario_day_story():
    output = replay_day(SCENARIO)
    runs = output["runs"]
    assert [r["alarms"] for r in runs] == [[], ["steam:3141590"], [], [], []]
    assert runs[0]["roundup"] and "steam:3141590" in runs[0]["roundup"]  # roundup first...
    alarm = next(p for p in runs[1]["posted"] if p["channel"] == "alarm")["payload"]
    assert alarm["embeds"][0]["title"].startswith("📈 Escalated")  # ...then escalated to an alarm
    assert "🧱 Roblox-clone discourse" in alarm["embeds"][0]["description"]
    assert runs[2]["posted"] == []  # no double alarm, roundup not due
    assert runs[3]["roundup"] == ["itch:tavernfolk/tiny-tavern-brawl"]
    assert runs[4]["posted"] == []  # Steam 503 + itch Cloudflare: the run survives, nothing posted
    assert output["labels"] == 1  # the two 👍 on the alarm were learned once
    assert all(r["unmatched_requests"] == [] for r in runs)
    blob = json.dumps(output)
    assert "Mega Corp Shooter" not in blob  # big-publisher game never posted
    for secret in ("scenario-secret", "scen-ario0-pass-word", "SCENARIO-REFRESH"):
        assert secret not in blob


def test_the_broken_feeds_yaml_of_2026_10_04_changes_nothing_else_in_the_day(tmp_path):
    """With that file every scan used to stop at the config check. Now Steam, Reddit, itch and
    Bluesky run exactly as recorded; the two valid feeds are requested, the bad YouTube URL is not."""
    manifest = yaml.safe_load((SCENARIO / "manifest.yaml").read_text(encoding="utf-8"))
    env = {str(k): str(v) for k, v in manifest["env"].items()}
    config = load_config(config_dir_with_feeds(tmp_path, INCIDENT_FEEDS), env=env)
    assert len(config.feeds.feeds) == 3 and config.feeds.problems
    output = replay_day(SCENARIO, config=config)
    golden = json.loads((SCENARIO / "expected.json").read_text(encoding="utf-8"))
    for run, expected in zip(output["runs"], golden["runs"], strict=True):
        assert run["posted"] == expected["posted"]
        assert run["alarms"] == expected["alarms"] and run["roundup"] == expected["roundup"]
        assert run["unmatched_requests"] == [  # not recorded: answered 404, like an offline feed
            "GET https://rss.app/feeds/5KcRbde1HFqAzPdx.xml",
            "GET https://rss.app/feeds/K1vwmXudAkt1exqO.xml",
        ]
