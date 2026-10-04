"""The recorded scenario day replays end to end and produces exactly the golden payloads."""

from __future__ import annotations

import json

from gembot.replay import replay_day, run_replay
from tests.factories import FIXTURES

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
