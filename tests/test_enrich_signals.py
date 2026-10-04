"""Comment signals: intent (hype), negativity and Roblox-clone discourse."""

from __future__ import annotations

import pytest

from gembot.enrich.signals import (
    analyze_comments,
    intent_hits,
    is_bot_name,
    is_roblox_joke,
    merge_signals,
    negative_hits,
)
from gembot.models import Comment, CommentSignals
from tests.factories import make_comments


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("Wishlisted!", "wishlisted"),
        ("just wish-listed it", "wishlisted"),
        ("wishlisting this right now", "wishlisted"),
        ("Added to my wishlist", "wishlisted"),
        ("added it to wishlist", "wishlisted"),
        ("this is on my wishlist already", "wishlisted"),
        ("will wishlist when I'm home", "wishlisted"),
        ("TAKE MY MONEY", "take my money"),
        ("take all my money please", "take my money"),
        ("I need this", "need this"),
        ("need it now", "need this"),
        ("I want this so bad", "want this"),
        ("want to play this", "want this"),
        ("day one purchase for sure", "day one"),
        ("day 1 buy", "day one"),
        ("When does it come out?", "when does it come out"),
        ("when is it coming out", "when does it come out"),
        ("when is this out", "when does it come out"),
        ("When will the game release", "when does it come out"),
        ("when can I buy it", "when does it come out"),
        ("when's it out?", "when does it come out"),
        ("Release date?", "release date?"),
        ("what's the release date", "release date?"),
        ("Is there a demo?", "is there a demo"),
        ("any demo yet", "is there a demo"),
        ("where's the demo", "is there a demo"),
        ("Can I playtest?", "playtest?"),
        ("I'd love to playtest this", "playtest?"),
        ("sign me up for the playtest", "playtest?"),
        ("playtest?", "playtest?"),
        ("me and the boys", "me and the boys"),
        ("Me n the lads at 2am", "me and the boys"),
        ("my friends would love this", "my friends would love this"),
        ("My gf is gonna love it", "my friends would love this"),
        ("can't wait!!", "can't wait"),
        ("Cannot wait", "can't wait"),
        ("instant buy", "instant buy"),
        ("insta-wishlist", "instant buy"),
        ("Sign me up", "sign me up"),
        ("Where can I get this?", "where can i get it"),
        ("where do I buy it", "where can i get it"),
        ("Steam link?", "steam link?"),
        ("link to the steam page pls", "steam link?"),
        ("is this on steam", "steam link?"),
        ("I would play this", "would play"),
        ("would definitely buy", "would play"),
        ("I'd play that", "would play"),
        ("gonna buy it", "gonna buy"),
        ("will definitely grab it", "gonna buy"),
        ("I need to play this", "play with friends"),
        ("playing this with my friends tonight", "play with friends"),
        ("not gonna lie, I wishlisted it", "wishlisted"),
        ("No way, take my money", "take my money"),
    ],
)
def test_intent_phrases(text, label):
    assert label in intent_hits(text)


@pytest.mark.parametrize(
    "text",
    [
        "Looks nice",
        "Cool art style",
        "I don't need this",
        "nobody wants this",
        "I would never play this",
        "I wouldn't play that",
        "the wishlist button is broken",
        "wishlist now on steam",  # the dev's own call to action, not intent
        "",
    ],
)
def test_no_intent(text):
    assert intent_hits(text) == []


def test_intent_hits_are_unique_labels_in_pattern_order():
    assert intent_hits("Wishlisted! Take my money. Wishlisted again, can't wait") == [
        "wishlisted",
        "take my money",
        "can't wait",
    ]


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("asset flip", "asset flip"),
        ("Another asset-flipped game", "asset flip"),
        ("this is a scam", "scam"),
        ("scammy store page", "scam"),
        ("AI slop", "ai slop"),
        ("the art is ai-generated", "ai slop"),
        ("obviously made with AI", "ai slop"),
        ("generative ai garbage", "ai slop"),
        ("stolen assets", "stolen"),
        ("total rip-off of R.E.P.O.", "stolen"),
        ("they ripped off Lethal Company", "stolen"),
        ("plagiarism", "stolen"),
        ("dev abandoned it", "abandoned"),
        ("dead game", "abandoned"),
        ("cash grab", "cash grab"),
        ("cash-grab", "cash grab"),
        ("straight from the unity asset store", "unity asset store"),
        ("asset store models everywhere", "unity asset store"),
        ("so low effort", "low effort"),
        ("low-effort trash", "low effort"),
    ],
)
def test_negative_phrases(text, label):
    assert label in negative_hits(text)


@pytest.mark.parametrize(
    "text",
    [
        "friendslop at its finest",  # "slop" alone is a genre word, not negativity
        "not an asset flip at all",
        "this isn't a scam",
        "Nothing about this feels like a cash grab",
        "doesn't look like AI slop",
        "great effort",
        "",
    ],
)
def test_no_negativity(text):
    assert negative_hits(text) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("roblox", True),
        ("looks like roblox lol", True),
        ("Roblox clone", True),
        ("this is a roblox game", True),
        ("roblox game", True),
        ("robloxian vibes", True),
        ("fortnite creative map energy", True),
        ("Fortnite Creative", True),
        ("made in UEFN?", True),
        ("I make roblox games for a living", True),
        ("doesn't look like roblox at all", False),
        ("not a roblox clone, it's way better", False),
        ("not gonna lie, looks like roblox", True),
        ("looks great", False),
        ("", False),
    ],
)
def test_roblox_jokes(text, expected):
    assert is_roblox_joke(text) is expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("AutoModerator", True),
        ("RemindMeBot", True),
        ("sneakpeekbot", True),
        ("helper_bot", True),
        ("github-actions[bot]", True),
        ("u/SaveVideoBot", True),
        ("Talbot", False),
        ("robotfan", False),
        ("gorilla_dev", False),
        ("", False),
        (None, False),
    ],
)
def test_bot_names(name, expected):
    assert is_bot_name(name) is expected


def test_table_of_comments_to_counts():
    texts = [
        "Wishlisted! me and the boys are ready",  # u0 intent
        "take my money",  # u1 intent
        "this looks like a roblox game lol",  # u2 roblox
        "roblox clone, but I'd play it",  # u3 roblox + intent
        "asset flip",  # u4 negative
        "is this a scam?",  # u5 negative
        "cool",  # u6 nothing
        "Wishlisted too",  # u0 again: same commenter
    ]
    authors = ["u0", "u1", "u2", "u3", "u4", "u5", "u6", "u0"]
    signals = analyze_comments(make_comments(texts, authors=authors), post_comment_count=42)
    assert signals.sampled == 8
    assert signals.distinct_commenters == 7
    assert signals.intent_comments == 4
    assert signals.intent_commenters == 3
    assert signals.roblox_comments == 2
    assert signals.roblox_commenters == 2
    assert signals.negative_commenters == 2
    assert signals.negative_terms == ["asset flip", "scam"]
    assert signals.post_comment_count == 42
    assert len(signals.intent_examples) == 3
    assert signals.intent_examples[0] == "Wishlisted! me and the boys are ready"
    assert signals.roblox_examples == ["this looks like a roblox game lol", "roblox clone, but I'd play it"]
    assert signals.negative_frac == pytest.approx(2 / 7)


def test_post_author_bots_and_deleted_comments_are_ignored():
    comments = [
        Comment(id="1", author="gorilla_dev", text="Wishlist it on Steam! Take my money lol"),
        Comment(id="2", author="u/Gorilla_Dev", text="can't wait for you all to play"),
        Comment(id="3", author="AutoModerator", text="Wishlisted! Your post was removed"),
        Comment(id="4", author="helpful", text="take my money", is_bot=True),
        Comment(id="5", author="RemindMeBot", text="I need this"),
        Comment(id="6", author="someone", text="[deleted]"),
        Comment(id="7", author="someone2", text="   "),
        Comment(id="8", author="[deleted]", text="I need this"),
        Comment(id="9", author="", text="need this"),
        Comment(id="10", author="", text="roblox"),
        Comment(id="11", author="fan", text="Wishlisted"),
    ]
    signals = analyze_comments(comments, post_author="@gorilla_dev", post_comment_count=-5)
    assert signals.sampled == 4  # [deleted]-author, two anonymous, fan
    assert signals.distinct_commenters == 4  # anonymous comments count as distinct people
    assert signals.intent_commenters == 3
    assert signals.roblox_commenters == 1
    assert signals.post_comment_count == 0


def test_examples_are_short_and_one_per_commenter():
    long_text = "I need this " + "really " * 40
    comments = make_comments([long_text, "I need this too", "take my money", "can't wait", "wishlisted"])
    signals = analyze_comments(comments)
    assert len(signals.intent_examples) == 3
    assert len(signals.intent_examples[0]) <= 100 and signals.intent_examples[0].endswith("…")
    same_person = make_comments(["need this", "want this", "can't wait"], authors=["a", "a", "b"])
    assert analyze_comments(same_person).intent_examples == ["need this", "can't wait"]


def test_empty_comment_list():
    signals = analyze_comments([])
    assert signals == CommentSignals()
    assert signals.negative_frac == 0.0


def test_merge_signals_sums_counts_and_caps_examples():
    a = analyze_comments(
        make_comments(["wishlisted", "roblox", "asset flip"], authors=["a", "b", "c"]), post_comment_count=10
    )
    b = analyze_comments(
        make_comments(["take my money", "need this", "scam", "asset flip"], authors=["x", "y", "z", "w"]),
        post_comment_count=20,
    )
    merged = merge_signals([a, b, None])  # type: ignore[list-item]
    assert merged.sampled == 7
    assert merged.distinct_commenters == 7
    assert merged.intent_commenters == 3 and merged.intent_comments == 3
    assert merged.roblox_comments == 1 and merged.roblox_commenters == 1
    assert merged.negative_commenters == 3
    assert merged.negative_terms == ["asset flip", "scam"]
    assert merged.post_comment_count == 30
    assert merged.intent_examples == ["wishlisted", "take my money", "need this"]
    assert merge_signals([]) == CommentSignals()


def test_merge_counts_people_once_across_a_games_posts():
    authors = [f"fan{i}" for i in range(8)] + ["meh1", "meh2"]
    texts = ["Wishlisted! me and the boys need this"] * 8 + ["cool", "nice"]
    one = analyze_comments(make_comments(texts, authors=authors), post_comment_count=10, platform="reddit")
    two = analyze_comments(make_comments(texts, authors=authors), post_comment_count=10, platform="reddit")
    merged = merge_signals([one, two])
    assert merged.intent_commenters == 8 and merged.distinct_commenters == 10
    assert merged.intent_comments == 16 and merged.sampled == 20  # comments still add up
    elsewhere = analyze_comments(
        make_comments(texts, authors=authors), post_comment_count=10, platform="bluesky"
    )
    assert merge_signals([one, elsewhere]).intent_commenters == 16  # same names, other platform
    old = one.model_copy(update={"intent_ids": []})  # signals stored before ids existed
    assert merge_signals([old, two]).intent_commenters == 16
