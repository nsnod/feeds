"""Reading config files: repeated keys everywhere, and the lenient feeds.yaml loader.

feeds.yaml is edited by hand on github.com; a mistake in it must never stop a scan. Each rule
below gets a small file of its own, plus the exact file that stopped every scan on 2026-10-04.
"""

from __future__ import annotations

import pytest

from gembot.collectors.rss import BadFeedUrl, check_feed_url
from gembot.config import ConfigError, Feeds, load_config, load_feeds
from tests.factories import INCIDENT_FEEDS


def feeds_from(tmp_path, text: str) -> Feeds:
    path = tmp_path / "feeds.yaml"
    path.write_text(text, encoding="utf-8")
    return load_feeds(path)


# ----------------------------------------------------------------------------- the incident


def test_the_incident_file_loads_all_three_feeds_and_names_each_mistake():
    feeds = load_feeds(INCIDENT_FEEDS)
    assert [feed.name for feed in feeds.feeds] == [
        "GameGil (@officialgamegil)",
        "Hellmei (@Hellmeitv)",
        "KreekCraft (YouTube)",
    ]
    assert [feed.source for feed in feeds.feeds] == ["instagram", "instagram", "youtube"]
    assert [feed.audience for feed in feeds.feeds] == [8357, 6580, None]
    assert feeds.problems == [
        "line 28: feed #1 \"GameGil (@officialgamegil)\": unknown key 'feeds' ignored - an extra 'feeds:' "
        "line ended up inside a feed; delete it (feeds.yaml needs exactly one 'feeds:' line, at the very top)",
        "line 38: 'feeds' appears again (first on line 22); ignored - remove the extra line",
    ]
    # the third mistake is found by the URL check (the RSS source and check-config run it)
    with pytest.raises(BadFeedUrl) as caught:
        check_feed_url(feeds.feeds[2].url)
    assert str(caught.value).startswith(
        "channel_id is 26 characters; YouTube channel ids are 24 and start with UC (was 'UC' pasted twice?)"
        " - try channel_id=UCxsk7hqE_CwZWGEJEkGanbA"
    )


def test_the_incident_file_does_not_stop_load_config(tmp_path):
    (tmp_path / "feeds.yaml").write_bytes(INCIDENT_FEEDS.read_bytes())
    config = load_config(tmp_path, env={})  # used to raise ConfigError: feeds -> [2]
    assert len(config.feeds.feeds) == 3 and len(config.feeds.problems) == 2


# ----------------------------------------------------------------------------- repeated keys


@pytest.mark.parametrize(
    ("name", "text", "key", "lines"),
    [
        (
            "settings.yaml",
            "decisions:\n  alarm_score: 70\n  roundup_score: 40\n  alarm_score: 90\n",
            "alarm_score",
            (2, 4),
        ),
        ("sources.yaml", "reddit:\n  limit: 50\nfit_keywords: {}\nreddit:\n  limit: 10\n", "reddit", (1, 4)),
        ("blocklist.yaml", "companies: [Ubisoft]\nkeywords: []\ncompanies: [EA]\n", "companies", (1, 3)),
    ],
)
def test_a_repeated_key_in_settings_sources_or_blocklist_is_an_error(tmp_path, name, text, key, lines):
    (tmp_path / name).write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_config(tmp_path, env={})
    message = str(caught.value)
    assert message.startswith(f"{name}: '{key}' appears twice (lines {lines[0]} and {lines[1]})")
    assert "YAML would quietly use only the last one" in message


def test_every_repeated_key_is_listed_once(tmp_path):
    (tmp_path / "settings.yaml").write_text("run: {}\nllm: {}\nrun: {}\nllm: {}\n", encoding="utf-8")
    with pytest.raises(
        ConfigError, match=r"'run' appears twice \(lines 1 and 3\); 'llm' appears twice \(lines 2 and 4\)"
    ):
        load_config(tmp_path, env={})


def test_merge_keys_may_be_overridden_and_a_flow_mapping_repeat_is_caught(tmp_path):
    (tmp_path / "settings.yaml").write_text(
        "decisions:\n  <<: {alarm_score: 50, roundup_score: 30}\n  alarm_score: 80\n", encoding="utf-8"
    )
    decisions = load_config(tmp_path, env={}).settings.decisions  # "<<" then an override: not a repeat
    assert (decisions.alarm_score, decisions.roundup_score) == (80, 30)
    (tmp_path / "settings.yaml").write_text(
        "decisions: {alarm_score: 50, alarm_score: 80}\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match=r"'alarm_score' appears twice \(lines 1 and 1\)"):
        load_config(tmp_path, env={})
    flow = feeds_from(tmp_path, "feeds:\n  - {name: a, url: 'https://a.example/x.xml', name: b}\n")
    assert flow.feeds[0].name == "a" and flow.problems == [
        "line 2: 'name' appears again (first on line 2); ignored - remove the extra line"
    ]


def test_yaml_merge_keys_still_work_in_feeds_and_own_keys_win(tmp_path):
    feeds = feeds_from(
        tmp_path,
        "feeds:\n"
        "  - &ig {name: one, url: 'https://rss.app/feeds/a.xml', source: instagram, audience: 5}\n"
        "  - <<: *ig\n"
        "    name: two\n"
        "    url: 'https://rss.app/feeds/b.xml'\n",
    )
    assert [(f.name, f.url, f.source, f.audience) for f in feeds.feeds] == [
        ("one", "https://rss.app/feeds/a.xml", "instagram", 5),
        ("two", "https://rss.app/feeds/b.xml", "instagram", 5),
    ]
    assert feeds.problems == []


def test_a_repeated_key_inside_a_feed_keeps_the_first_one(tmp_path):
    feeds = feeds_from(
        tmp_path,
        "feeds:\n  - name: a\n    url: https://a.example/feed.xml\n    url: https://b.example/feed.xml\n",
    )
    assert feeds.feeds[0].url == "https://a.example/feed.xml"
    assert feeds.problems == [
        "line 4: 'url' appears again (first on line 3); ignored - remove the extra line"
    ]


# ----------------------------------------------------------------------------- the file as a whole


@pytest.mark.parametrize("text", ["", "# only comments\n", "feeds:\n", "feeds: []\n"])
def test_empty_feeds_files_are_fine(tmp_path, text):
    assert feeds_from(tmp_path, text) == Feeds()


def test_a_missing_feeds_file_is_fine(tmp_path):
    assert load_feeds(tmp_path / "feeds.yaml") == Feeds()


def test_unparseable_yaml_means_no_feeds_and_quotes_line_and_column(tmp_path):
    feeds = feeds_from(tmp_path, "feeds:\n  - name: a\n   url: https://a.example/feed.xml\n")
    assert feeds.feeds == []
    assert len(feeds.problems) == 1
    problem = feeds.problems[0]
    assert problem.startswith("line 3, column 4: not valid YAML (")
    assert "no feeds were loaded" in problem


def test_a_tab_or_unhashable_key_is_a_yaml_problem_too(tmp_path):
    assert (
        feeds_from(tmp_path, "feeds:\n\t- name: a\n")
        .problems[0]
        .startswith("line 2, column 1: not valid YAML")
    )
    assert "unhashable key" in feeds_from(tmp_path, "feeds:\n  - ? [a]\n    : 1\n").problems[0]


def test_unreadable_bytes_are_a_problem_not_a_crash(tmp_path):
    (tmp_path / "feeds.yaml").write_bytes(b"feeds:\n  - name: \xff\xfe\n")
    feeds = load_feeds(tmp_path / "feeds.yaml")
    assert feeds.feeds == [] and feeds.problems[0].startswith("could not read the file")


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        (
            "- name: a\n  url: https://a.example/feed.xml\n",
            "line 1: the file must start with a 'feeds:' line followed by the list of feeds (found a list); "
            "no feeds were loaded",
        ),
        (
            "just some words\n",
            "the file must start with a 'feeds:' line followed by the list of feeds "
            "(found the text 'just some words'); no feeds were loaded",
        ),
        (
            "feeds: 2\n",
            "line 1: 'feeds' must be a list of feeds, each starting with '- name:' (found the number 2); "
            "no feeds were loaded",
        ),
        (
            "feeds:\n  name: a\n  url: https://a.example/feed.xml\n",
            "line 1: 'feeds' must be a list of feeds, each starting with '- name:' (found a group of settings); "
            "no feeds were loaded",
        ),
    ],
)
def test_a_wrong_shape_means_no_feeds_and_one_problem(tmp_path, text, problem):
    feeds = feeds_from(tmp_path, text)
    assert feeds.feeds == [] and feeds.problems == [problem]


def test_unknown_top_level_keys_are_ignored_with_a_problem(tmp_path):
    feeds = feeds_from(
        tmp_path,
        "feeds:\n  - name: a\n    url: https://a.example/feed.xml\nfeds:\n  - name: b\n",
    )
    assert [f.name for f in feeds.feeds] == ["a"]
    assert feeds.problems == [
        "line 4: unknown top-level key 'feds' ignored - did you mean 'feeds'? only 'feeds:' belongs at the "
        "left edge, check the spelling and the spaces in front"
    ]


def test_the_problems_list_cannot_be_written_from_the_file(tmp_path):
    feeds = feeds_from(tmp_path, "feeds: []\nproblems: ['all good, nothing to see']\n")
    assert len(feeds.problems) == 1
    assert feeds.problems[0].startswith("line 2: unknown top-level key 'problems' ignored")
    assert "nothing to see" not in feeds.problems[0]


# ----------------------------------------------------------------------------- one feed at a time


def test_entries_that_are_not_feeds_are_skipped_by_position(tmp_path):
    feeds = feeds_from(
        tmp_path,
        "feeds:\n  - 2\n  -\n  - just a url\n  - name: ok\n    url: https://a.example/feed.xml\n  - [a, b]\n"
        f"  - true\n  - 2026-10-04\n  - {'x' * 100}\n",
    )
    assert [f.name for f in feeds.feeds] == ["ok"]
    tail = (
        "not a feed; skipped - each feed starts with '- name:' and has its url and source on the lines below"
    )
    assert feeds.problems == [
        f"line 2: feeds entry #1 is the number 2, {tail}",
        f"line 3: feeds entry #2 is empty, {tail}",
        f"line 4: feeds entry #3 is the text 'just a url', {tail}",
        f"line 7: feeds entry #5 is a list, {tail}",
        f"line 8: feeds entry #6 is the word true, {tail}",
        f"line 9: feeds entry #7 is a date, {tail}",
        f"line 10: feeds entry #8 is the text '{'x' * 58}…, {tail}",  # long values are shortened
    ]


def test_unknown_keys_in_a_feed_are_ignored_and_the_feed_still_loads(tmp_path):
    feeds = feeds_from(
        tmp_path,
        "feeds:\n"
        "  - name: Tiny Pixel\n"
        "    url: https://www.youtube.com/feeds/videos.xml?channel_id=UCabcdefghijklmnopqrstuv\n"
        "    sorce: youtube\n"
        "    followers: 1200\n",
    )
    assert [(f.name, f.source) for f in feeds.feeds] == [("Tiny Pixel", "rss")]  # source fell back to rss
    assert feeds.problems == [
        "line 4: feed #1 \"Tiny Pixel\": unknown key 'sorce' ignored - did you mean 'source'? check its "
        "spelling and indentation (a feed has name, url, source, audience, enabled)",
        "line 5: feed #1 \"Tiny Pixel\": unknown key 'followers' ignored - check its spelling and "
        "indentation (a feed has name, url, source, audience, enabled)",
    ]


@pytest.mark.parametrize(
    ("entry", "problem"),
    [
        ("{name: a}", "line 2: feed #1 \"a\" skipped: 'url' is missing"),
        ("{url: 'https://a.example/x.xml'}", "line 2: feed #1 skipped: 'name' is missing"),
        ("{name: a, url: }", "line 2: feed #1 \"a\" skipped: 'url' is empty"),
        (
            "{name: a, url: 'https://a.example/x.xml', audience: 25K}",
            "line 2: feed #1 \"a\" skipped: 'audience' must be a whole number like 25000 (got '25K')",
        ),
        (
            "{name: a, url: 'https://a.example/x.xml', enabled: maybe}",
            "line 2: feed #1 \"a\" skipped: 'enabled' must be true or false (got 'maybe')",
        ),
        (
            "{name: 7, url: 'https://a.example/x.xml'}",
            "line 2: feed #1 skipped: 'name' must be text (put it in quotes) (got 7)",
        ),
    ],
)
def test_a_feed_with_a_missing_or_invalid_field_is_skipped(tmp_path, entry, problem):
    feeds = feeds_from(tmp_path, f"feeds:\n  - {entry}\n  - {{name: good, url: 'https://b.example/x.xml'}}\n")
    assert [f.name for f in feeds.feeds] == ["good"]  # the valid entry always loads
    assert feeds.problems == [problem]


def test_a_field_problem_names_the_line_of_that_field(tmp_path):
    feeds = feeds_from(
        tmp_path,
        "feeds:\n  - name: Some Studio\n    url: https://rss.app/feeds/a.xml\n    audience: 25K\n",
    )
    assert feeds.feeds == []
    assert feeds.problems == [
        "line 4: feed #1 \"Some Studio\" skipped: 'audience' must be a whole number like 25000 (got '25K')"
    ]


def test_problems_are_sorted_by_line(tmp_path):
    feeds = feeds_from(
        tmp_path,
        "feeds:\n  - 1\n  - {name: a, url: 'https://a.example/x.xml', sorce: rss}\nfeeds: []\nextra: 1\n",
    )
    lines = [int(problem.split(":")[0].removeprefix("line ")) for problem in feeds.problems]
    assert lines == sorted(lines) == [2, 3, 4, 5]
