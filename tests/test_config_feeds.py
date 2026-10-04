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


def without_lines(text: str, *numbers: int) -> str:
    """``text`` with those (1-based) lines deleted, as a user following a problem's advice would."""
    return "".join(line for n, line in enumerate(text.splitlines(keepends=True), start=1) if n not in numbers)


# The three feeds of the user's config/feeds.yaml, written correctly.
REAL_FEEDS = """feeds:
  - name: "GameGil (@officialgamegil)"
    url: "https://rss.app/feeds/K1vwmXudAkt1exqO.xml"
    source: instagram
    audience: 8357

  - name: "Hellmei (@Hellmeitv)"
    url: "https://rss.app/feeds/5KcRbde1HFqAzPdx.xml"
    source: instagram
    audience: 6580

  - name: "KreekCraft (YouTube)"
    url: "https://www.youtube.com/feeds/videos.xml?channel_id=UCxsk7hqE_CwZWGEJEkGanbA"
    source: youtube
"""
REAL_NAMES = ["GameGil (@officialgamegil)", "Hellmei (@Hellmeitv)", "KreekCraft (YouTube)"]


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
        "line 38: an extra 'feeds:' line with no feed in it; ignored - delete line 38 and keep the one on line 22",
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


def test_doing_what_the_incident_problems_say_leaves_only_the_channel_id_to_fix(tmp_path):
    fixed = without_lines(INCIDENT_FEEDS.read_text(encoding="utf-8"), 28, 38)
    feeds = feeds_from(tmp_path, fixed)
    assert [feed.name for feed in feeds.feeds] == REAL_NAMES and feeds.problems == []
    with pytest.raises(BadFeedUrl, match="was 'UC' pasted twice"):
        check_feed_url(feeds.feeds[2].url)


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


def test_overriding_a_merged_key_inside_a_deeper_anchor_is_not_a_repeat(tmp_path):
    """The anchor sits deeper than the mapping that merges it, so plain PyYAML copies the merged
    keys into the anchor before the anchor itself is built; that must not look like a repeat."""
    (tmp_path / "settings.yaml").write_text(
        "features:\n  default_baseline_eph: &eph\n    <<: {reddit: 9}\n    reddit: 3\nbudgets:\n  <<: *eph\n",
        encoding="utf-8",
    )
    config = load_config(tmp_path, env={})  # used to fail: 'reddit' appears twice (lines 3 and 4)
    assert config.settings.features.default_baseline_eph["reddit"] == 3
    assert config.settings.budgets.reddit == 3
    feeds = feeds_from(
        tmp_path,
        "defaults:\n  more:\n    ig: &ig\n      <<: {source: tiktok, audience: 5}\n      source: instagram\n"
        "feeds:\n  - <<: *ig\n    name: a\n    url: https://rss.app/feeds/AbCdEfGhIjKlMnOp.xml\n",
    )
    assert [(f.name, f.source, f.audience) for f in feeds.feeds] == [("a", "instagram", 5)]
    assert len(feeds.problems) == 1 and "unknown top-level key 'defaults'" in feeds.problems[0]


def test_a_repeated_key_inside_a_feed_keeps_the_first_one(tmp_path):
    feeds = feeds_from(
        tmp_path,
        "feeds:\n  - name: a\n    url: https://a.example/feed.xml\n    url: https://b.example/feed.xml\n",
    )
    assert feeds.feeds[0].url == "https://a.example/feed.xml"
    assert feeds.problems == [
        "line 4: 'url' appears again (first on line 3); ignored - remove the extra line"
    ]


# ----------------------------------------------------------------------------- more than one "feeds:"


def test_uncommenting_the_example_block_keeps_the_real_feeds(tmp_path):
    """feeds.yaml's header says to remove the leading "# " from an example block that has its own
    "feeds:" line. Doing that must not drop the real feeds below it."""
    header = INCIDENT_FEEDS.read_text(encoding="utf-8").splitlines(keepends=True)[:21]  # as in config/
    assert header[12] == "# feeds:\n" and header[20] == "\n"
    text = "".join(line[2:] if 12 <= n <= 19 else line for n, line in enumerate(header)) + REAL_FEEDS
    feeds = feeds_from(tmp_path, text)
    assert [f.name for f in feeds.feeds] == [
        "Some indie curator (Instagram)",
        "Devlog channel (YouTube)",
        *REAL_NAMES,
    ]
    assert feeds.problems == [
        "line 22: a second 'feeds:' line (the first is on line 13); what is under it was read as more "
        "feeds - delete line 22 and keep every feed under line 13"
    ]
    for example in feeds.feeds[:2]:  # the placeholders are never requested
        with pytest.raises(
            BadFeedUrl, match=r"^this is an example URL \(the XXXX\.\.\. part is a placeholder\)"
        ):
            check_feed_url(example.url)
    fixed = feeds_from(tmp_path, without_lines(text, 22))
    assert [f.name for f in fixed.feeds] == [f.name for f in feeds.feeds] and fixed.problems == []


def test_a_leftover_empty_feeds_line_above_the_real_list_is_ignored(tmp_path):
    """The old template ended with "feeds: []"; plain YAML kept the last "feeds:", so this worked."""
    text = "feeds: []\n\nfeeds:\n  - name: A\n    url: https://rss.app/feeds/AbCdEfGhIjKlMnOp.xml\n"
    feeds = feeds_from(tmp_path, text)
    assert [f.name for f in feeds.feeds] == ["A"]
    assert feeds.problems == [
        "line 1: an extra, empty 'feeds:' line; ignored - delete line 1 and keep the one on line 3"
    ]
    fixed = feeds_from(tmp_path, without_lines(text, 1))
    assert [f.name for f in fixed.feeds] == ["A"] and fixed.problems == []


A = "{name: a, url: 'https://a.example/x.xml'}"
B = "{name: b, url: 'https://b.example/x.xml'}"


@pytest.mark.parametrize(
    ("text", "names", "problems"),
    [
        (
            f"feeds:\n  - {A}\nfeeds:\n  - {B}\n",
            ["a", "b"],
            [
                "line 3: a second 'feeds:' line (the first is on line 1); what is under it was read as more "
                "feeds - delete line 3 and keep every feed under line 1"
            ],
        ),
        (
            f"feeds: [2]\nfeeds:\n  - {A}\n",
            ["a"],
            [
                "line 1: an extra 'feeds:' line with no feed in it; ignored - delete line 1 and keep the one on line 2"
            ],
        ),
        (
            f"feeds: 2\nfeeds:\n  - {A}\n",
            ["a"],
            [
                "line 1: an extra 'feeds:' line that is not a list (found the number 2); ignored - delete line 1 "
                "and keep the one on line 2"
            ],
        ),
        (
            f"feeds:\n  - {A}\nfeeds:\n  name: b\n",
            ["a"],
            [
                "line 3: an extra 'feeds:' line that is not a list (found a group of settings); ignored - delete "
                "line 3 and keep the one on line 1"
            ],
        ),
        (
            "feeds:\nfeeds: []\n",
            [],
            ["line 2: an extra, empty 'feeds:' line; ignored - delete line 2 and keep the one on line 1"],
        ),
        (
            f"feeds:\n  - {A}\nfeeds:\n  - 2\n  - {B}\n",  # a list with a feed in it: every entry is read
            ["a", "b"],
            [
                "line 3: a second 'feeds:' line (the first is on line 1); what is under it was read as more "
                "feeds - delete line 3 and keep every feed under line 1",
                "line 4: feeds entry #2 is the number 2, not a feed; skipped - each feed starts with '- name:' "
                "and has its url and source on the lines below",
            ],
        ),
    ],
)
def test_every_feeds_list_is_read_and_each_extra_feeds_line_is_named(tmp_path, text, names, problems):
    feeds = feeds_from(tmp_path, text)
    assert [f.name for f in feeds.feeds] == names and feeds.problems == problems


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


QUOTES = 'put the text after the colon in double quotes, like name: "@handle: clips"'
SPACES = "check the spaces at the start of that line and the one above"


@pytest.mark.parametrize(
    ("line", "where", "hint"),
    [
        ("    name: @Hellmeitv\n", "line 3, column 11", QUOTES),
        ("    name: Devlog: My Game\n", "line 3, column 17", QUOTES),
        ("    name: `Hellmei`\n", "line 3, column 11", QUOTES),
        ("    name: %done\n", "line 3, column 11", QUOTES),
        ("    name: *star\n", "line 3, column 11", QUOTES),
        ("    name: !important\n", "line 3, column 11", QUOTES),
        ("    name: | clips\n", "line 3, column 13", QUOTES),
        (
            "    name: [clips\n",
            "line 3, column 11",
            "a '[' or '{' there is never closed; if it is part of a name, ",
        ),
        ('    name: "clips\n', "line 3, column 11", "a quote mark there is never closed"),
        ("\tname: clips\n", "line 3, column 1", "use spaces, not tabs, at the start of that line"),
        ("    name: clips\n---\n", "line 4, column 1", "delete the '---' line"),
        ("      name: clips\n", "line 3, column 11", SPACES),  # too far right: read as more of the url
        ("   name: clips\n", "line 3, column 4", SPACES),
    ],
)
def test_yaml_errors_say_what_to_change(tmp_path, line, where, hint):
    feeds = feeds_from(
        tmp_path, f"feeds:\n  - url: https://rss.app/feeds/AbCdEfGhIjKlMnOp.xml\n{line}    source: x\n"
    )
    assert feeds.feeds == [] and len(feeds.problems) == 1
    problem = feeds.problems[0]
    assert problem.startswith(f"{where}: not valid YAML (") and "; no feeds were loaded - " in problem
    assert hint in problem.split("; no feeds were loaded - ", 1)[1]


def test_an_invisible_control_character_names_its_line(tmp_path):
    feeds = feeds_from(tmp_path, "feeds:\n  - name: a\n    url: https://a.example/x.xml\x07\n")
    assert feeds.feeds == [] and feeds.problems == [
        "line 3: not valid YAML (an invisible control character, #x0007); no feeds were loaded - retype that "
        "line (the character often comes along when copying a URL)"
    ]


def test_an_alias_bomb_is_a_problem_not_a_crash(tmp_path):
    """A few hundred bytes that unfold into 9^9 references: the error text must not unfold them."""
    lines = ["a: &a [lol, lol, lol, lol, lol, lol, lol, lol, lol]"]
    lines += [f"{c}: &{c} [{', '.join([f'*{p}'] * 9)}]" for p, c in zip("abcdefgh", "bcdefghi", strict=True)]
    text = "\n".join(lines) + "\nfeeds:\n  - name: A\n    url: https://a.example/b.xml\n    audience: *i\n"
    text += "  - name: B\n    url: *i\n"
    feeds = feeds_from(tmp_path, text)  # used to build a 2.7 GB repr() and run out of memory
    assert [(f.name, f.audience) for f in feeds.feeds] == [("A", None)]
    assert feeds.problems[-2:] == [
        "line 13: feed #1 \"A\": 'audience' must be a whole number like 25000 (got a list); ignored, the feed "
        "still loads",
        "line 15: feed #2 \"B\" skipped: 'url' must be text (put it in quotes) (got a list)",
    ]


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
        "feeds:\n  - name: Some Studio\n    url: https://rss.app/feeds/a.xml\n    enabled: paused\n",
    )
    assert feeds.feeds == []  # not fetched: the user may have meant to pause it
    assert feeds.problems == [
        "line 4: feed #1 \"Some Studio\" skipped: 'enabled' must be true or false (got 'paused')"
    ]


def test_feeds_indented_under_a_stray_feeds_line_still_load(tmp_path):
    """GitHub's editor keeps the indentation of the line above: after a stray "feeds:" inside a
    feed, the feeds typed below it end up under it."""
    text = (
        "feeds:\n"
        '  - name: "GameGil"\n'
        '    url: "https://rss.app/feeds/K1vwmXudAkt1exqO.xml"\n'
        "    source: instagram\n"
        "    feeds:\n"
        '    - name: "Hellmei"\n'
        '      url: "https://rss.app/feeds/5KcRbde1HFqAzPdx.xml"\n'
        "      source: instagram\n"
        '    - name: "KreekCraft"\n'
        "      source: youtube\n"
        "  - name: Blog\n"
        "    url: https://blog.example.com/feed.xml\n"
    )
    feeds = feeds_from(tmp_path, text)
    assert [f.name for f in feeds.feeds] == ["GameGil", "Hellmei", "Blog"]
    assert feeds.problems == [
        "line 5: feed #1 \"GameGil\": an extra 'feeds:' line ended up inside this feed, so the 2 feed(s) "
        "indented under it were read as part of it (they still load) - delete that 'feeds:' line and move "
        "those feeds left so each '- name:' lines up with the others",
        "line 9: feed #3 \"KreekCraft\" skipped: 'url' is missing",  # numbered as they appear in the file
    ]
    lines = text.splitlines(keepends=True)
    fixed = "".join(line[2:] if 6 <= n <= 10 else line for n, line in enumerate(lines, start=1) if n != 5)
    assert feeds_from(tmp_path, fixed).problems == [
        "line 8: feed #3 \"KreekCraft\" skipped: 'url' is missing"
    ]


def test_a_loop_of_aliases_under_a_stray_feeds_line_ends(tmp_path):
    feeds = feeds_from(tmp_path, "feeds:\n  - &a {name: a, url: 'https://a.example/x.xml', feeds: [*a]}\n")
    assert {f.name for f in feeds.feeds} == {"a"} and feeds.problems


@pytest.mark.parametrize(
    ("written", "audience"),
    [
        ("8357", 8357),
        ("8_357", 8357),
        ("'8,357'", 8357),
        ("8 357", 8357),
        ("25K", 25000),
        ("1.2M", 1_200_000),
    ],
)
def test_follower_counts_are_read_the_way_profiles_show_them(tmp_path, written, audience):
    feeds = feeds_from(
        tmp_path, f"feeds:\n  - name: a\n    url: https://a.example/x.xml\n    audience: {written}\n"
    )
    assert [f.audience for f in feeds.feeds] == [audience] and feeds.problems == []


@pytest.mark.parametrize(
    ("field", "problem"),
    [
        ("audience: lots", "'audience' must be a whole number like 25000 (got 'lots')"),
        ("audience: 8.357", "'audience' must be a whole number like 25000 (got 8.357)"),
        ("audience: [1, 2]", "'audience' must be a whole number like 25000 (got a list)"),
        ("source:", "'source' is empty"),
        ("source: 7", "'source' must be text (put it in quotes) (got 7)"),
    ],
)
def test_a_bad_optional_value_is_left_out_and_the_feed_still_loads(tmp_path, field, problem):
    feeds = feeds_from(
        tmp_path, f"feeds:\n  - name: GameGil\n    url: https://rss.app/feeds/a.xml\n    {field}\n"
    )
    assert [(f.name, f.source, f.audience) for f in feeds.feeds] == [("GameGil", "rss", None)]
    assert feeds.problems == [f'line 4: feed #1 "GameGil": {problem}; ignored, the feed still loads']


@pytest.mark.parametrize(
    ("written", "read_as", "advice"),
    [
        ("yotube", "youtube", "fix the spelling"),
        ("instagarm", "instagram", "fix the spelling"),
        ("TikTk", "tiktok", "fix the spelling"),
        ("facebook", "rss", "use instagram, tiktok, youtube or rss"),
    ],
)
def test_a_misspelled_source_is_read_as_the_platform_it_means(tmp_path, written, read_as, advice):
    feeds = feeds_from(
        tmp_path, f"feeds:\n  - name: K\n    url: https://a.example/x.xml\n    source: {written}\n"
    )
    assert [f.source for f in feeds.feeds] == [read_as]
    assert feeds.problems == [
        f"line 4: feed #1 \"K\": source '{written.lower()}' is not a platform GemBot knows; read as "
        f"'{read_as}' - {advice}"
    ]


def test_every_platform_gembot_scores_and_any_added_in_settings_is_a_known_source(tmp_path):
    feeds = "".join(
        f"  - {{name: {source}, url: 'https://a.example/{source}.xml', source: {source}}}\n"
        for source in ("instagram", "TikTok", "youtube", "rss", "reddit", "bluesky", "twitch")
    )
    (tmp_path / "feeds.yaml").write_text(f"feeds:\n{feeds}", encoding="utf-8")
    (tmp_path / "settings.yaml").write_text(
        "features:\n  default_baseline_eph: {twitch: 4.0}\n", encoding="utf-8"
    )
    config = load_config(tmp_path, env={})
    assert config.feeds.problems == []
    assert [f.source for f in config.feeds.feeds][-2:] == ["bluesky", "twitch"]


def test_problems_are_sorted_by_line(tmp_path):
    feeds = feeds_from(
        tmp_path,
        "feeds:\n  - 1\n  - {name: a, url: 'https://a.example/x.xml', sorce: rss}\nfeeds: []\nextra: 1\n",
    )
    lines = [int(problem.split(":")[0].removeprefix("line ")) for problem in feeds.problems]
    assert lines == sorted(lines) == [2, 3, 4, 5]
