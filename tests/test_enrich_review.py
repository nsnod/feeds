"""Adversarial review of game resolution, comment signals, comment sampling and the LLM pass.

Every test here encodes behaviour a Discord reader would call wrong (wrong game posted, two
games merged, one game split, junk titles, hype/negativity miscounted). They are expected
to FAIL against the current code; each docstring names the finding.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from gembot.collectors.base import CollectContext, Collector
from gembot.config import ResolverSource
from gembot.enrich.comments import enrich_game_comments
from gembot.enrich.entity import Resolver, hard_ids_for, title_candidates
from gembot.enrich.signals import (
    analyze_comments,
    intent_hits,
    is_bot_name,
    is_roblox_joke,
    negative_hits,
)
from gembot.models import Comment, Game, Mention
from tests.factories import NOW, make_comments, make_config, make_game, make_http, make_mention

LETHAL_COMPANY = "https://store.steampowered.com/app/1966720/Lethal_Company/"
DREAD_SHIFT = "https://store.steampowered.com/app/3527290/Dread_Shift/"


def resolve(games: dict[str, Game], mentions: list[Mention], **kw):
    store = {m.key: m for m in mentions}
    kw.setdefault("now", NOW)
    kw.setdefault("settings", ResolverSource())
    return Resolver(games, store, **kw).resolve(mentions)


def same_game(a: Mention, b: Mention) -> bool:
    return a.game_id is not None and a.game_id == b.game_id


# ============================================================================ hard ids


def test_reference_steam_link_before_the_devs_own_link_is_not_the_posts_game():
    """HIGH: "If you liked <Lethal Company link> you'll love our game: <own link>" resolves to
    Lethal Company's app id, so GemBot would post Lethal Company (appdetails then retitle it)."""
    post = make_mention(
        "reddit",
        "dread",
        title="Dread Shift demo is out!",
        text=f"If you liked Lethal Company ({LETHAL_COMPANY}) you'll love our game: {DREAD_SHIFT}",
        author="dread_dev",
        channel="r/IndieDev",
    )
    games: dict[str, Game] = {}
    result = resolve(games, [post])
    assert result.assignments[post.key] == "steam:3527290"
    assert "steam:1966720" not in games.get(result.assignments[post.key], make_game()).hard_ids


def test_reference_steam_link_is_not_bridged_onto_the_devs_itch_game():
    """HIGH: the "one Steam + one itch link = same game" rule glues a referenced Steam game
    (Lethal Company) onto the dev's own itch game; the game id/best URL become Lethal Company."""
    post = make_mention(
        "reddit",
        "itch",
        title="My co-op horror game Dread Shift is on itch!",
        text=f"Play it free: https://dreaddev.itch.io/dread-shift - heavily inspired by {LETHAL_COMPANY}",
        author="dread_dev",
    )
    games: dict[str, Game] = {}
    result = resolve(games, [post])
    game = games[result.assignments[post.key]]
    assert "steam:1966720" not in game.hard_ids
    assert game.steam_appid != 1966720


@pytest.mark.parametrize(
    ("text", "wrong_id"),
    [
        # Bluesky stores the shortened display text in record.text (host + 13 path chars + "...");
        # Bluesky RSS feeds / RSS.app captions carry that text without the link facet.
        ("Demo is up on itch! gorilladev.itch.io/gorilla-pizz...", "itch:gorilladev/gorilla-pizz"),
        ("Wishlist Gorilla Pizza Panic on Steam: store.steampowered.com/app/35272…", "steam:35272"),
        ("Wishlist Gorilla Pizza Panic on Steam: store.steampowered.com/app/35272...", "steam:35272"),
    ],
)
def test_truncated_display_urls_do_not_become_hard_ids(text, wrong_id):
    """MEDIUM-LOW: "..."/"…" is stripped as trailing punctuation, so a truncated link becomes a
    different (existing or 404) Steam app / itch page."""
    mention = make_mention("rss", "trunc", title="", text=text, author="gorilla")
    assert wrong_id not in hard_ids_for(mention)


# ============================================================================ titles: reference games


@pytest.mark.parametrize(
    ("title", "reference"),
    [
        ("Supermarket Together but it's a pizza shop - demo out now", "Supermarket Together"),
        ("Schedule 1 but in space, demo out now", "Schedule 1"),
        ("Like Lethal Company? Try our demo 👻 #indiegame", "Like Lethal Company"),
        ("As requested in r/LethalCompany, here is our co-op game demo", "LethalCompany"),
    ],
)
def test_reference_games_in_pitches_are_not_titles(title, reference):
    """HIGH: "X but Y" / "Like X?" pitches title the game after the referenced (released) game."""
    assert reference not in title_candidates(make_mention("reddit", "ref", title=title, author="dev"))


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            ("reddit", "Supermarket Together but it's a pizza shop - demo out now", "", "pizza_dev"),
            ("reddit", "Supermarket Together but you run a gas station - demo out now", "", "gas_dev"),
        ),
        (
            ("reddit", "Schedule 1 but in space, demo out now", "", "space_dev"),
            ("bluesky", "", "Schedule 1 but underwater, demo out now! #indiedev", "fish.bsky.social"),
        ),
        (
            ("tiktok", "Like Lethal Company? Try our demo 👻 #indiegame", "", "ghostdev"),
            ("tiktok", "Like Lethal Company? Try our demo 🐀 #indiegame #coop", "", "ratdev"),
        ),
    ],
)
def test_pitches_of_different_games_do_not_merge_under_a_reference_title(first, second):
    """HIGH: two different devs' "<reference> but ..." posts merge into ONE game titled after the
    reference game (wrong title + two games merged)."""
    a = make_mention(first[0], "a", title=first[1], text=first[2], author=first[3], hours_ago=5)
    b = make_mention(second[0], "b", title=second[1], text=second[2], author=second[3], hours_ago=1)
    games: dict[str, Game] = {}
    resolve(games, [a, b])
    assert not same_game(a, b), {gid: g.title for gid, g in games.items()}


# ============================================================================ titles: junk Title Case runs


@pytest.mark.parametrize(
    "title",
    [
        "Top 10 Upcoming Co-op Games of 2026",  # -> "Top 10 Upcoming"
        "10 Upcoming Co-op Horror Games You Need To Wishlist",  # -> "10 Upcoming"
        "5 Hidden Gem Co-op Games on Steam",  # -> "5 Hidden Gem"
        "Upcoming Indie Games - October 2026",  # -> "Upcoming Indie"
        "Making a Multiplayer Game in Godot - Devlog #4",  # -> the whole sentence
        "Adding Multiplayer to My Game - Devlog 2",  # -> "Adding Multiplayer to My"
        "Solo Dev Makes Co-op Horror Game",  # -> "Solo Dev Makes"
        "We Added Ragdolls To Our Game",  # -> "Added Ragdolls"
        "My Indie Game Got 10,000 Wishlists in 1 Week",  # -> "Got 10"
        "Steam Next Fest Results - What We Learned",  # -> "Next Fest Results"
        "We Made a Friendslop Game (Lethal Company Inspired)",  # -> "Made a Friendslop", ...
        "Hiring: Unity Developer for Co-op Game",  # -> "Hiring"
    ],
)
def test_title_case_sentences_and_listicles_are_not_game_titles(title):
    """MEDIUM: YouTube/Reddit Title Case sentences become "games" with junk names."""
    assert title_candidates(make_mention("youtube", "junk", title=title, author="SomeChannel")) == []


@pytest.mark.parametrize(
    ("title", "best"),
    [
        ("Early Access Launch Postmortem: Vault Rats", "Vault Rats"),
        ("PAX West demo booth for Vault Rats - come say hi!", "Vault Rats"),
        ("Devlog #7 - Adding Proximity Chat to Spooky Shift", "Spooky Shift"),
        ("Spooky Shift Devlog #8: Ragdoll Physics", "Spooky Shift"),
    ],
)
def test_the_named_game_beats_the_surrounding_title_case_phrase(title, best):
    """MEDIUM: the game name loses to "Early Access Launch Postmortem" / "PAX West" / the devlog
    topic sentence, or (``<Game> Devlog #8: topic``) no candidate is found at all."""
    candidates = title_candidates(make_mention("youtube", "v", title=title, author="SomeChannel"))
    assert candidates[:1] == [best]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            ("reddit", "Early Access Launch Postmortem: Vault Rats", "ratdev"),
            ("reddit", "Early Access Launch Postmortem: Moss Movers", "mossdev"),
        ),
        (
            ("youtube", "Making a Multiplayer Game in Godot - Devlog #4", "ChannelA"),
            ("youtube", "Making a Multiplayer Game in Godot - Devlog #1", "ChannelB"),
        ),
        (
            ("youtube", "Top 10 Upcoming Co-op Games of 2026", "ChannelA"),
            ("youtube", "Top 10 Upcoming Co-op Games (October 2026)", "ChannelB"),
        ),
    ],
)
def test_identical_junk_titles_from_different_creators_do_not_merge(first, second):
    """HIGH: Vault Rats and Moss Movers (two real, different games) merge into one game titled
    "Early Access Launch Postmortem"; two channels' devlogs/listicles merge and fake "cross"."""
    a = make_mention(first[0], "a", title=first[1], author=first[2], hours_ago=5)
    b = make_mention(second[0], "b", title=second[1], author=second[2], hours_ago=1)
    games: dict[str, Game] = {}
    resolve(games, [a, b])
    assert not same_game(a, b), {gid: g.title for gid, g in games.items()}


def test_one_devlog_channel_about_one_game_resolves_to_one_game():
    """MEDIUM: the spec's own "Devlog channel (YouTube)" feed splits one game into several
    "games" (and drops one video): "Adding Proximity Chat to Spooky Shift" + "Spooky Shift"."""
    videos = [
        make_mention(
            "youtube", "v1", title="Devlog #7 - Adding Proximity Chat to Spooky Shift", hours_ago=30
        ),
        make_mention("youtube", "v2", title="Spooky Shift Devlog #8: Ragdoll Physics", hours_ago=20),
        make_mention("youtube", "v3", title="Spooky Shift - Official Trailer", hours_ago=10),
    ]
    for video in videos:
        video.author = "Spooky Shift Dev"
    games: dict[str, Game] = {}
    result = resolve(games, videos)
    assert result.dropped == []
    assert [g.title for g in games.values()] == ["Spooky Shift"]


@pytest.mark.parametrize(
    ("title", "junk"),
    [
        ("Huge thanks to u/PixelPete for the music! Our co-op game demo is out", "PixelPete"),
        ("Thanks to r/IndieGameDevs our co-op game demo got 500 players", "IndieGameDevs"),
        ("Cross-posting from r/IndieGameDevs: my co-op game demo", "IndieGameDevs"),
        ("Shoutout to r/CoOpGaming - wishlist our game", "CoOpGaming"),
    ],
)
def test_subreddit_and_username_mentions_are_not_titles(title, junk):
    """MEDIUM: ``r/Subreddit`` and ``u/user`` are not stripped like #tags/@mentions, so the
    subreddit or a Reddit username becomes the game's title."""
    candidates = title_candidates(make_mention("reddit", "sub", title=title, author="dev"))
    assert not any(junk in cand for cand in candidates), candidates


# ============================================================================ signals


@pytest.mark.parametrize(
    "text",
    [
        "No offense but this is an asset flip",
        "No hate but this looks like AI slop",
        "Not to be rude but the art is AI generated",
    ],
)
def test_polite_preface_does_not_cancel_criticism(text):
    """MEDIUM: a negation before "but" ("No offense but ...") cancels the negative phrase after it."""
    assert negative_hits(text)


@pytest.mark.parametrize(
    "text",
    [
        "I don't usually comment on these but take my money",
        "I don't even like horror games but I need this",
        "Not usually into co-op stuff but wishlisted",
    ],
)
def test_negation_before_but_does_not_cancel_intent(text):
    """MEDIUM: "I don't usually comment but take my money" is not counted as intent."""
    assert intent_hits(text)


def test_polite_preface_does_not_cancel_roblox_joke():
    """MEDIUM: same clause rule for Roblox discourse."""
    assert is_roblox_joke("No offense but this is a Roblox game")


def test_polite_criticism_reaches_the_negativity_penalty():
    """MEDIUM: 4 of 10 commenters call it an asset flip / AI slop (> 30% -> -15 penalty), but
    every one of them opened with "No offense but" / "No hate but", so none is counted."""
    texts = [
        "No offense but this is an asset flip",
        "No hate but this looks like AI slop",
        "Not to be rude but the art is AI generated",
        "No offense but these are asset flip models",
    ] + ["looks fun"] * 6
    signals = analyze_comments(make_comments(texts), post_author="dev")
    assert signals.negative_frac > 0.3


@pytest.mark.parametrize(
    "text",
    [
        "Who would buy this garbage",
        "Who would play this?",
        "Day one refund lol",
        "How did you get people wishlisting so fast?",
    ],
)
def test_sarcasm_questions_and_dev_chatter_are_not_intent(text):
    """MEDIUM-LOW: dismissive questions / "day one refund" / dev-to-dev wishlist talk count as hype."""
    assert intent_hits(text) == []


@pytest.mark.parametrize("text", ["The boys would love this", "the boys are gonna love this"])
def test_the_boys_would_love_this_is_intent(text):
    """LOW: a common variant of the spec's "me and the boys" / "my friends would love this"."""
    assert intent_hits(text)


@pytest.mark.parametrize("name", ["Mr_Robot", "WorkingRobot", "TheAbbot"])
def test_humans_whose_name_ends_in_bot_are_not_bots(name):
    """LOW: any name ending in "bot" is dropped as a bot (Mr_Robot, ...)."""
    assert not is_bot_name(name)


# ============================================================================ comments.py


class _CommentsCollector(Collector):
    name = "reddit"

    def __init__(self, ctx, comments: dict[str, list[Comment]]):
        super().__init__(ctx)
        self.comments = comments
        self.calls: list[str] = []

    def collect(self):
        return []

    def fetch_comments(self, mention, limit):
        self.calls.append(mention.key)
        return list(self.comments.get(mention.key, []))


class _NoCommentsCollector(Collector):
    """Like the real X collector: has a reply count but no ``fetch_comments``."""

    name = "x"

    def collect(self):
        return []


def test_sources_without_comment_support_do_not_use_up_sampling_slots():
    """LOW: an X post with 50 replies takes one of the 2 sampling slots even though the X
    collector cannot fetch replies, so the second Reddit thread is never sampled."""
    config = make_config()
    ctx = CollectContext(config=config, http=make_http(), now=NOW)
    x_post = make_mention("x", "x1", comments=50, likes=900, author="clipper")
    big = make_mention("reddit", "big", comments=40, likes=300, author="dev")
    mid = make_mention("reddit", "mid", comments=30, likes=200, author="dev")
    reddit = _CommentsCollector(
        ctx, {big.key: make_comments(["Wishlisted!"]), mid.key: make_comments(["take my money"])}
    )
    game = make_game(mention_keys=[x_post.key, big.key, mid.key])
    store = {m.key: m for m in (x_post, big, mid)}
    enrich_game_comments(
        game, store, {"reddit": reddit, "x": _NoCommentsCollector(ctx)}, now=NOW, settings=config.settings
    )
    assert reddit.calls == [big.key, mid.key]


def test_game_ids_stay_deterministic_for_review_inputs():
    """Sanity (passes): resolution of the review inputs is order independent."""

    def run(order):
        mentions = [
            make_mention("reddit", "a", title="Dread Shift demo is out!", author="d1", hours_ago=5),
            make_mention(
                "bluesky", "b", text="Dread Shift demo is out! #indiedev", author="fan", hours_ago=4
            ),
            make_mention("tiktok", "c", title="Vault Rats demo out now on Steam!!", author="v", hours_ago=3),
        ]
        games: dict[str, Game] = {}
        result = resolve(games, [mentions[i] for i in order], now=NOW + timedelta(hours=1))
        return sorted(games), result.assignments

    assert run([0, 1, 2]) == run([2, 1, 0]) == run([1, 2, 0])
