"""Embed builders against golden files, Discord limits, score bar and time formatting.

Regenerate the golden files after an intended change with::

    UPDATE_GOLDEN=1 python -m pytest tests/test_discord_embeds.py
"""

from __future__ import annotations

import json
import os
from datetime import timedelta

import pytest

from gembot.discord import embeds as E
from gembot.discord.setup import make_test_card
from gembot.models import GemCard
from tests.factories import GOLDEN, NOW


def assert_golden(name: str, payload: dict) -> None:
    path = GOLDEN / name
    data = json.loads(json.dumps(payload))  # tuples -> lists, exactly like the wire format
    if os.environ.get("UPDATE_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    assert path.exists(), f"missing golden file {name}: run with UPDATE_GOLDEN=1"
    assert json.loads(path.read_text(encoding="utf-8")) == data, f"{name} changed (UPDATE_GOLDEN=1 to accept)"


def assert_valid(payload: dict) -> None:
    """Every Discord limit holds (counted in UTF-16 units, the stricter way)."""
    units = E.text_units
    assert units(payload.get("content") or "") <= E.CONTENT_LIMIT
    embeds = payload.get("embeds") or []
    assert len(embeds) <= E.EMBEDS_PER_MESSAGE
    for embed in embeds:
        assert units(embed.get("title", "")) <= E.TITLE_LIMIT
        assert units(embed.get("description", "")) <= E.DESCRIPTION_LIMIT
        fields = embed.get("fields") or []
        assert len(fields) <= E.FIELDS_LIMIT
        for field in fields:
            assert 1 <= units(field["name"]) <= E.FIELD_NAME_LIMIT
            assert 1 <= units(field["value"]) <= E.FIELD_VALUE_LIMIT
        assert units((embed.get("footer") or {}).get("text", "")) <= E.FOOTER_TEXT_LIMIT
        assert units((embed.get("author") or {}).get("name", "")) <= E.AUTHOR_NAME_LIMIT
    assert E.embeds_total(embeds) <= E.EMBED_TOTAL_LIMIT


def card(**overrides) -> GemCard:
    base = dict(
        game_id="steam:2001",
        title="Gorilla Pizza Panic",
        url="https://store.steampowered.com/app/2001/",
        score=74.4,
        reasons=[
            "312 upvotes in 3h on r/IndieDev (9× normal for that sub)",
            "Seen on Reddit, Bluesky and Steam in the last 24h",
            "17 different people said they wishlisted or want to play with friends",
        ],
        pitch="Four gorillas, one pizza oven, proximity chat and absolutely no plan.",
        links=[
            ("Steam", "https://store.steampowered.com/app/2001/"),
            ("Reddit r/IndieDev", "https://www.reddit.com/r/IndieDev/comments/abc/gorilla_pizza_panic/"),
            ("Bluesky", "https://bsky.app/profile/dev.bsky.social/post/3kxyz"),
        ],
        thumb="https://cdn.example.com/steam/2001/header.jpg",
        first_seen=NOW - timedelta(hours=5),
    )
    base.update(overrides)
    return GemCard(**base)


# ---------------------------------------------------------------- golden payloads


def test_alarm_golden():
    payload = E.alarm_payload(card(), now=NOW)
    assert_golden("alarm.json", payload)
    embed = payload["embeds"][0]
    assert embed["title"] == "🚨 GEM ALARM: Gorilla Pizza Panic"
    assert embed["description"].startswith("**Gem Score** ▰▰▰▰▰▰▰▱▱▱ 74\n\n• 312 upvotes")
    assert embed["footer"]["text"] == "First seen 5h ago · Sat 3 Oct, 07:00 UTC"
    assert embed["timestamp"] == "2026-10-03T07:00:00+00:00"
    assert "allowed_mentions" not in payload and "<@&" not in payload["content"]
    assert_valid(payload)


def test_alarm_escalated_with_role_ping_golden():
    payload = E.alarm_payload(card(escalated=True, score=81), ping_role_id="555000111", now=NOW)
    assert_golden("alarm_escalated_ping.json", payload)
    assert payload["content"].startswith("<@&555000111> 📈 Escalated · 🚨 **GEM ALARM:**")
    assert payload["allowed_mentions"] == {"parse": [], "roles": ["555000111"]}
    assert payload["embeds"][0]["title"].startswith("📈 Escalated · 🚨 GEM ALARM:")


def test_test_alarm_golden():
    payload = E.alarm_payload(make_test_card(NOW), now=NOW)
    assert_golden("alarm_test.json", payload)
    assert payload["embeds"][0]["title"] == "🧪 TEST — 🚨 GEM ALARM: GemBot Test Game"


def test_roundup_header_golden():
    payload = E.roundup_header_payload(NOW + timedelta(hours=2), 3)
    assert_golden("roundup_header.json", payload)
    assert payload["content"] == (
        "📋 **Gem Roundup — Sat 3 Oct, 14:00 UTC**\n3 games worth a look. React 👍/👎 on each one."
    )
    assert "1 game worth" in E.roundup_header_payload(NOW, 1)["content"]


def test_roundup_entry_golden():
    payload = E.roundup_entry_payload(card(would_have_alarmed=True, heating_up=True, score=68))
    assert_golden("roundup_entry.json", payload)
    embed = payload["embeds"][0]
    assert embed["title"] == "⏰ Would have alarmed · 📈 Heating up · Gorilla Pizza Panic"
    lines = embed["description"].split("\n")
    assert lines[0] == "Gem Score ▰▰▰▰▰▰▰▱▱▱ 68"
    assert lines[1].startswith("312 upvotes")
    assert lines[2].startswith("[Steam](https://store.steampowered.com/app/2001/) · [Reddit r/IndieDev](")
    assert embed["thumbnail"] == {"url": "https://cdn.example.com/steam/2001/header.jpg"}


def test_welcome_golden():
    payload = E.welcome_payload({"alarm": "11", "roundup": "12", "status": "13"})
    assert_golden("welcome.json", payload)
    text = json.dumps(payload, ensure_ascii=False)
    for needle in ("<#11>", "<#12>", "<#13>", "👍", "👎", "All Messages"):
        assert needle in text
    assert "#gem-alarm" in json.dumps(E.welcome_payload(), ensure_ascii=False)
    assert_valid(payload)


def test_status_golden():
    payload = E.status_payload(
        ["🔴 Reddit is failing (6 runs in a row): HTTP 403", "🟢 Bluesky is back", "", "  "]
    )
    assert_golden("status.json", payload)
    assert (
        payload["embeds"][0]["description"]
        == "🔴 Reddit is failing (6 runs in a row): HTTP 403\n🟢 Bluesky is back"
    )
    assert E.status_payload([])["embeds"][0]["description"] == "All good."


# ---------------------------------------------------------------- builders, edge cases


def test_minimal_alarm_has_no_empty_parts():
    payload = E.alarm_payload(GemCard(game_id="t:x", title="X", score=50))
    embed = payload["embeds"][0]
    assert embed["description"] == "**Gem Score** ▰▰▰▰▰▱▱▱▱▱ 50"
    for key in ("url", "fields", "thumbnail", "footer", "timestamp"):
        assert key not in embed
    with_time = E.alarm_payload(GemCard(game_id="t:x", title="X", first_seen=NOW))
    assert with_time["embeds"][0]["footer"]["text"] == "First seen Sat 3 Oct, 12:00 UTC"


def test_roundup_entry_minimal_and_test_flag():
    embed = E.roundup_entry_payload(GemCard(game_id="t:x", title="X", score=45, test=True))["embeds"][0]
    assert embed["title"] == "🧪 TEST — X"
    assert embed["description"] == "Gem Score ▰▰▰▰▰▱▱▱▱▱ 45"
    assert "url" not in embed and "thumbnail" not in embed


def test_links_line_escapes_and_stops_when_full():
    links = [("A [b]", "https://x.test/a (1)"), ("Empty", ""), ("C", "https://x.test/c")]
    assert E.links_line(links) == "[A (b)](https://x.test/a%20%281%29) · [C](https://x.test/c)"
    assert E.links_line([("A", "https://x.test/" + "a" * 50)] * 3, limit=70).count("[A]") == 1
    assert E.markdown_link("", "https://x.test") == "[link](https://x.test)"


@pytest.mark.parametrize(
    ("score", "bar"),
    [
        (0, "▱▱▱▱▱▱▱▱▱▱ 0"),
        (4.9, "▱▱▱▱▱▱▱▱▱▱ 5"),
        (5, "▰▱▱▱▱▱▱▱▱▱ 5"),
        (45, "▰▰▰▰▰▱▱▱▱▱ 45"),
        (74, "▰▰▰▰▰▰▰▱▱▱ 74"),
        (100, "▰▰▰▰▰▰▰▰▰▰ 100"),
        (137.5, "▰▰▰▰▰▰▰▰▰▰ 100"),
        (-12, "▱▱▱▱▱▱▱▱▱▱ 0"),
        (float("nan"), "▱▱▱▱▱▱▱▱▱▱ 0"),
        ("junk", "▱▱▱▱▱▱▱▱▱▱ 0"),
    ],
)
def test_score_bar(score, bar):
    assert E.score_bar(score) == bar


@pytest.mark.parametrize(
    ("delta", "text"),
    [
        (timedelta(seconds=20), "just now"),
        (timedelta(minutes=12), "12 min ago"),
        (timedelta(hours=5, minutes=59), "5h ago"),
        (timedelta(hours=47), "47h ago"),
        (timedelta(days=3, hours=2), "3 days ago"),
        (timedelta(hours=-1), "just now"),
    ],
)
def test_relative_time(delta, text):
    assert E.relative_time(NOW - delta, NOW) == text


def test_format_utc_converts_naive_and_other_zones():
    from datetime import datetime, timezone

    assert E.format_utc(datetime(2026, 1, 5, 9, 3)) == "Mon 5 Jan, 09:03 UTC"
    assert (
        E.format_utc(datetime(2026, 1, 5, 9, 3, tzinfo=timezone(timedelta(hours=2))))
        == "Mon 5 Jan, 07:03 UTC"
    )


# ---------------------------------------------------------------- truncation and limits


def test_truncate_cuts_at_word_boundary_with_ellipsis():
    text = "word " * 100
    out = E.truncate(text, 50)
    assert len(out) <= 50 and out.endswith("word…")
    assert E.truncate("short", 10) == "short"
    assert E.truncate("abcdefghij", 5) == "abcd…"
    assert E.truncate("abc", 1) == "…" and E.truncate("abc", 0) == ""
    assert E.truncate("🚨🚨🚨", 4) == "🚨…"  # never splits an emoji's surrogate pair
    assert E.shorten("  lots \n of\t space  ", 100) == "lots of space"


def test_long_title_description_and_content_are_truncated():
    payload = {
        "content": "c" * 2500,
        "embeds": [{"title": "T" * 300, "description": "word " * 1000, "footer": {"text": "f" * 3000}}],
    }
    out = E.enforce_limits(payload)
    assert_valid(out)
    embed = out["embeds"][0]
    assert len(embed["title"]) == 256 and embed["title"].endswith("…")
    assert len(embed["description"]) <= 4096 and embed["description"].endswith("…")
    assert len(out["content"]) == 2000
    assert len(payload["embeds"][0]["title"]) == 300  # input untouched


def test_forty_fields_become_twenty_five_valid_fields():
    fields = [{"name": f"field {i}", "value": "v" * 2000} for i in range(40)]
    fields[0] = {"name": "", "value": ""}
    out = E.enforce_limits({"embeds": [{"title": "many", "fields": fields}]})
    assert_valid(out)
    kept = out["embeds"][0]["fields"]
    assert 0 < len(kept) <= 25
    assert kept[0] == {"name": E.ZWSP, "value": E.ZWSP}


def test_twelve_embeds_and_total_over_6000_stay_valid():
    embeds = [
        {"title": f"Game {i}", "description": "d" * 3000, "author": {"name": "a" * 300}} for i in range(12)
    ]
    out = E.enforce_limits({"embeds": embeds})
    assert_valid(out)
    assert len(out["embeds"]) == 10
    assert all(e["description"] for e in out["embeds"])  # shrunk proportionally, not dropped


def test_pathological_titles_and_footers_drop_embeds_until_valid():
    embeds = [{"title": "🚨" * 200, "footer": {"text": "f" * 2048}, "description": "x"} for _ in range(10)]
    out = E.enforce_limits({"embeds": embeds})
    assert_valid(out)
    assert 1 <= len(out["embeds"]) < 10

    many_names = [{"name": "n" * 256, "value": "v"} for _ in range(25)]
    out = E.enforce_limits({"embeds": [{"title": "t", "fields": many_names}]})
    assert_valid(out)
    assert len(out["embeds"][0]["fields"]) < 25

    lonely = E.enforce_limits({"embeds": [{"title": "t" * 256, "footer": {"text": "f" * 2048}}] * 3})
    assert_valid(lonely)


def test_single_embed_without_fields_loses_description_last():
    embed = {"title": "t", "description": "d" * 4096, "footer": {"text": "f" * 2048}}
    out = E.enforce_limits({"embeds": [embed, dict(embed)]})
    assert_valid(out)
    assert len(out["embeds"]) == 2 and all(e.get("description") for e in out["embeds"])


def test_tiny_description_shrunk_to_nothing_is_removed():
    embeds = [{"title": "tiny", "description": "ab"}] + [{"title": "big", "description": "d" * 4096}] * 9
    out = E.enforce_limits({"embeds": embeds})
    assert_valid(out)
    assert "description" not in out["embeds"][0] and len(out["embeds"]) == 10


def test_enforce_limits_ignores_garbage_and_empty_lists():
    out = E.enforce_limits({"embeds": ["not an embed", {"title": "ok", "fields": []}]})
    assert out == {"embeds": [{"title": "ok"}]}


def test_builders_never_exceed_limits_with_huge_cards():
    huge = card(
        title="Pizza " * 100,
        reasons=["reason " * 300] * 30,
        pitch="pitch " * 500,
        links=[("Reddit r/" + "x" * 300, "https://example.com/" + "y" * 400)] * 10,
    )
    for payload in (
        E.alarm_payload(huge, ping_role_id="1", now=NOW),
        E.roundup_entry_payload(huge),
        E.status_payload(["line " * 100] * 100),
        E.text_payload("t" * 5000),
    ):
        assert_valid(payload)
