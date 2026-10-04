"""Gem Score, exclusions, penalties, the Roblox bonus, prefilter and the "why" lines."""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from gembot.config import Blocklist
from gembot.enrich.entity import Resolver
from gembot.models import (
    Adjustment,
    CommentSignals,
    Evidence,
    Features,
    LLMVerdict,
    ScoreResult,
    SteamInfo,
)
from gembot.scoring.explain import (
    build_reasons,
    feed_label,
    fmt_count,
    fmt_hours,
    fmt_multiple,
    natural_join,
)
from gembot.scoring.features import ScoringContext
from gembot.scoring.score import (
    alarm_signals_for,
    blocklist_reason,
    clean_weights,
    company_matches,
    prefilter,
    score_game,
)
from tests.factories import NOW, make_config, make_game, make_mention
from tests.test_scoring_features import random_case, samples

CONFIG = make_config()
WEIGHTS = CONFIG.settings.weights
BLOCK = CONFIG.blocklist


def ctx_with(baselines=None, now=NOW) -> ScoringContext:
    return ScoringContext.from_config(CONFIG, now=now, baselines=baselines)


def score(game, mentions, signals=None, *, ctx=None, weights=None, blocklist=None):
    return score_game(game, mentions, ctx or ctx_with(), weights or WEIGHTS, blocklist or BLOCK, signals)


# ----------------------------------------------------------------------------- blocklist


@pytest.mark.parametrize(
    ("name", "company", "expected"),
    [
        ("Ubisoft Montreal", "Ubisoft", True),
        ("Ubisoft Montréal", "Ubisoft", True),
        ("Electronic Arts Inc.", "Electronic Arts", True),
        ("ELECTRONIC ARTS", "Electronic Arts", True),
        ("Take Two Interactive Software", "Take-Two Interactive", True),
        ("2K Games", "2K", True),
        ("2Kool Studio", "2K", False),
        ("Blizzardo Games", "Blizzard Entertainment", False),
        ("Blizzard", "Blizzard Entertainment", False),
        ("Red Storm (Ubisoft)", "Ubisoft", False),
        ("Tiny Ape Games", "Ubisoft", False),
        ("Anything", "", False),
    ],
)
def test_company_matching(name, company, expected):
    assert company_matches(name, company) is expected


def test_blocklist_checks_game_and_steam_companies():
    game = make_game(developer="Indie Folks")
    assert blocklist_reason(game, [], BLOCK) is None
    game.publisher = "Ubisoft Entertainment"
    assert blocklist_reason(game, [], BLOCK) == "big studio: Ubisoft (publisher 'Ubisoft Entertainment')"
    steam_game = make_game(
        steam=SteamInfo(appid=1, developers=["Small Co"], publishers=["Electronic Arts Inc."])
    )
    assert "Electronic Arts" in blocklist_reason(steam_game, [], BLOCK)
    payload = SteamInfo(appid=2, developers=["Capcom Co., Ltd."]).model_dump(mode="json")
    via_mention = make_mention("steam", "2", extra={"steam": payload})
    assert "Capcom" in blocklist_reason(make_game(), [via_mention], BLOCK)


def test_reddit_username_is_not_a_blocklisted_developer():
    """A solo dev u/Valve_Index_Fan posts "my game ...": the resolver guesses the poster is the
    developer, but a username that merely starts with "Valve" is not the studio Valve."""
    post = make_mention(
        "reddit",
        "solo1",
        title="My co-op horror game Moon Goblins just got a Steam page!",
        text="Proximity chat, 4 players, made it alone over 2 years.",
        author="Valve_Index_Fan",
        likes=150,
        comments=30,
        channel="r/IndieDev",
    )
    post.first_seen = NOW
    games: dict = {}
    stored = {post.key: post}
    Resolver(games, stored, now=NOW, settings=CONFIG.sources.resolver).resolve([post])
    (game,) = games.values()
    assert game.developer == "Valve_Index_Fan"  # the resolver's guess, from the post author
    assert blocklist_reason(game, [stored[post.key]], BLOCK) is None
    assert not score(game, [stored[post.key]]).excluded


@pytest.mark.parametrize("source", ["reddit", "bluesky", "x", "rss"])
def test_blocklist_ignores_poster_usernames_but_not_real_companies(source):
    poster = make_mention(source, "p1", author="valve_index_fan")
    fan = make_game(developer="Valve_Index_Fan")
    assert blocklist_reason(fan, [poster], BLOCK) is None
    # the same name with no post by that person is a developer name like any other
    assert blocklist_reason(fan, [], BLOCK) == "big studio: Valve (developer 'Valve_Index_Fan')"
    # Steam's developer/publisher lists and Game.publisher are always checked
    steam = make_game(developer="Valve_Index_Fan", steam=SteamInfo(appid=9, developers=["Valve Corporation"]))
    assert "Valve" in blocklist_reason(steam, [poster], BLOCK)
    publisher = make_game(developer="Valve_Index_Fan", publisher="Valve")
    assert blocklist_reason(publisher, [poster], BLOCK) == "big studio: Valve (publisher 'Valve')"
    # an account that IS the studio (exactly its name) still counts
    official = make_mention(source, "p2", author="Ubisoft")
    assert "Ubisoft" in blocklist_reason(make_game(developer="Ubisoft"), [official], BLOCK)


def test_blocklist_store_listing_authors_are_not_usernames():
    """itch.io / Steam listings are not posts by a person: their developer still matches."""
    listing = make_mention("itch", "valvestuff/thing", author="Valve_Index_Fan", channel="itch:new")
    game = make_game(developer="Valve_Index_Fan")
    assert blocklist_reason(game, [listing], BLOCK) == "big studio: Valve (developer 'Valve_Index_Fan')"


def test_blocklist_banned_keywords_are_whole_words():
    assert blocklist_reason(make_game(title="Crypto Raiders"), [], BLOCK) == "banned keyword: 'crypto'"
    m = make_mention(text="Earn tokens: a play-to-earn adventure")
    assert blocklist_reason(make_game(), [m], BLOCK) == "banned keyword: 'play-to-earn'"
    assert blocklist_reason(make_game(title="Cryptography Club"), [], BLOCK) is None
    assert blocklist_reason(make_game(title="No NFTs here"), [], BLOCK) is None
    assert blocklist_reason(make_game(title="Casino"), [], Blocklist(keywords=["  "])) is None


def test_excluded_game_scores_zero_without_reasons():
    m = make_mention(likes=100_000, comments=9000, hours_ago=1, audience=100)
    result = score(make_game(publisher="Square Enix"), [m])
    assert result.excluded and result.score == 0.0
    assert result.exclude_reason.startswith("big studio: Square Enix")
    assert result.reasons == []
    assert result.base > 0  # features are still computed for the logs


# ----------------------------------------------------------------------------- base score


def test_base_is_weighted_mean_of_features():
    game = make_game(first_seen=NOW - timedelta(hours=1))  # fresh = 1
    result = score(game, [])
    assert result.features.fresh == 1.0
    assert result.base == pytest.approx(10.0)  # fresh weight .10
    assert result.score == 10.0
    doubled = score(game, [], weights={k: 2 * v for k, v in WEIGHTS.items()})
    assert doubled.score == result.score
    assert sum(result.weights.values()) == pytest.approx(1.0)


def test_clean_weights_handles_bad_input():
    assert clean_weights({}) == {k: pytest.approx(1 / 7) for k in WEIGHTS}
    w = clean_weights({"velocity": 2, "hype": float("nan"), "fresh": -1, "bogus": 5})
    assert w["velocity"] == 1.0 and w["hype"] == 0.0 and "bogus" not in w


def test_alarm_signals_follow_thresholds():
    f = Features(velocity=0.5, hype=0.49, cross=0.6, meme=0.9, fit=1.0)
    assert alarm_signals_for(f, CONFIG.settings.decisions.alarm_signal_thresholds) == [
        "velocity",
        "cross",
        "meme",
    ]


# ----------------------------------------------------------------------------- penalties


def negativity(neg: int, distinct: int) -> CommentSignals:
    return CommentSignals(
        sampled=distinct,
        distinct_commenters=distinct,
        negative_commenters=neg,
        negative_terms=["asset flip", "scam"],
    )


def test_negativity_penalty():
    game = make_game()
    result = score(game, [], negativity(4, 10))
    assert [p.code for p in result.penalties] == ["negativity"]
    assert result.penalties[0].points == 15
    assert result.penalties[0].detail == "4 of 10 commenters negative (asset flip, scam)"
    assert score(game, [], negativity(3, 10)).penalties == []  # exactly 30% is not above 30%
    assert score(game, [], negativity(2, 2)).penalties == []  # too few commenters (minimum 3)
    assert score(game, [], negativity(1, 2)).penalties == []  # 1 grumpy reply of 2 is a fluke
    assert score(game, []).penalties == []


def test_negativity_minimum_is_three_commenters():
    assert CONFIG.settings.penalties.negativity_min_commenters == 3
    small = score(make_game(), [], negativity(3, 4))  # 75% of 4 people
    assert [p.code for p in small.penalties] == ["negativity"]
    assert small.penalties[0].detail == "3 of 4 commenters negative (asset flip, scam)"
    assert [p.code for p in score(make_game(), [], negativity(2, 3)).penalties] == ["negativity"]
    assert score(make_game(), [], negativity(2, 2)).penalties == []  # below the minimum


def test_negativity_penalty_detail_without_terms():
    s = CommentSignals(sampled=10, distinct_commenters=10, negative_commenters=9)
    assert score(make_game(), [], s).penalties[0].detail == "9 of 10 commenters negative"


@pytest.mark.parametrize(
    ("steam", "penalised"),
    [
        (SteamInfo(appid=1, release_date=date(2026, 8, 1)), True),
        (SteamInfo(appid=1, release_date=date(2026, 9, 20)), False),
        (SteamInfo(appid=1, release_date=date(2026, 9, 20), early_access=True), False),
        (SteamInfo(appid=1, release_date=date(2025, 3, 1), early_access=True), True),
        (SteamInfo(appid=1, release_date=date(2027, 1, 1)), False),
        (SteamInfo(appid=1, release_date_text="2024"), False),
        (SteamInfo(appid=1, release_date=date(2020, 1, 1), coming_soon=True), False),
        (None, False),
    ],
)
def test_old_release_penalty(steam, penalised):
    result = score(make_game(steam=steam), [])
    assert ("old_release" in [p.code for p in result.penalties]) is penalised
    if penalised:
        assert result.penalties[0].points == 20


def test_old_release_detail_mentions_early_access():
    steam = SteamInfo(appid=1, release_date=date(2025, 3, 1), early_access=True)
    detail = score(make_game(steam=steam), []).penalties[0].detail
    assert detail.startswith("Early Access launch 2025-03-01")


def test_spammer_penalty_counts_distinct_posts_by_same_author_in_window():
    posts = [make_mention("reddit", f"p{i}", author="PromoGuy", hours_ago=24 * i + 1) for i in range(4)]
    result = score(make_game(), posts)
    assert [p.code for p in result.penalties] == ["spammer"]
    assert result.penalties[0].points == 10
    assert "promoguy posted it 4 times on reddit" in result.penalties[0].detail
    assert score(make_game(), posts[:3]).penalties == []
    old = [*posts[:3], make_mention("reddit", "old", author="PromoGuy", hours_ago=24 * 8)]
    assert score(make_game(), old).penalties == []
    listings = [make_mention("steam", f"s{i}", author="Dev") for i in range(5)]
    assert score(make_game(), listings).penalties == []
    mixed = posts[:2] + [make_mention("bluesky", f"b{i}", author="PromoGuy") for i in range(2)]
    assert score(make_game(), mixed).penalties == []


def test_penalties_stack_and_score_never_below_zero():
    steam = SteamInfo(appid=1, release_date=date(2024, 1, 1))
    game = make_game(steam=steam, first_seen=NOW - timedelta(days=60))
    result = score(game, [], negativity(9, 10))
    assert {p.code for p in result.penalties} == {"negativity", "old_release"}
    assert result.score == 0.0


# ----------------------------------------------------------------------------- roblox bonus


def roblox(jokers: int, comments: int) -> CommentSignals:
    return CommentSignals(
        sampled=comments,
        distinct_commenters=comments,
        roblox_commenters=jokers,
        roblox_comments=jokers,
        post_comment_count=comments,
    )


def test_roblox_bonus_with_many_comments():
    m = make_mention(comments=40)
    result = score(make_game(), [m], roblox(12, 40))
    assert result.features.meme == 1.0
    assert [b.code for b in result.bonuses] == ["roblox_bonus"]
    assert result.score == pytest.approx(result.base + 8, abs=0.1)
    assert any(r.startswith("🧱 Roblox-clone discourse: 12 commenters") for r in result.reasons)


def test_roblox_bonus_with_velocity_instead_of_comments():
    ctx = ctx_with(samples("r/IndieDev", [2.0] * 10))
    m = make_mention(channel="r/IndieDev", comments=20, likes=20, hours_ago=2)  # 20 eph = 10x -> .83
    assert score(make_game(), [m], roblox(3, 20), ctx=ctx).bonuses[0].points == 8
    slow = make_mention(channel="r/IndieDev", comments=20, hours_ago=20)
    assert score(make_game(), [slow], roblox(3, 20), ctx=ctx).bonuses == []


def test_no_roblox_bonus_below_meme_threshold_or_on_tiny_posts():
    assert score(make_game(), [make_mention(comments=40)], roblox(2, 40)).bonuses == []
    assert score(make_game(), [make_mention(comments=6)], roblox(5, 6)).bonuses == []


def test_roblox_bonus_per_post_gate_and_game_level_comment_total():
    """Jokers only count under posts with 10+ comments; the bonus's "30+ comments" is the
    game's whole discussion (two 20-comment posts = 40), and the 🧱 line quotes the jokers
    that counted."""
    a = make_mention("reddit", "a", channel="r/IndieDev", comments=20, hours_ago=40)
    b = make_mention("bluesky", "b", comments=20, hours_ago=40)
    tiny = make_mention("reddit", "tiny", channel="r/IndieDev", comments=6, hours_ago=40)
    a.signals, b.signals, tiny.signals = roblox(2, 20), roblox(1, 20), roblox(5, 6)
    merged = roblox(8, 46)
    result = score(make_game(), [a, b, tiny], merged)
    assert result.evidence.meme_jokers == 3 and result.evidence.total_comments == 46
    assert result.features.meme == pytest.approx(0.6) and result.features.velocity < 0.4
    assert [(b.code, b.detail) for b in result.bonuses] == [
        ("roblox_bonus", "3 commenters joking it's a Roblox game")
    ]
    assert "🧱 Roblox-clone discourse: 3 commenters joking it's a Roblox game" in result.reasons
    # only the 6-comment post jokes: nothing counts, no bonus, no 🧱 line
    a.signals, b.signals = roblox(0, 20), roblox(0, 20)
    quiet = score(make_game(), [a, b, tiny], merged.model_copy(update={"roblox_commenters": 5}))
    assert quiet.features.meme == 0.0 and quiet.bonuses == []
    assert not any(r.startswith("🧱") for r in quiet.reasons)


def test_score_clamped_to_100():
    ctx = ctx_with(samples("r/IndieDev", [1.0] * 10))
    mentions = [
        make_mention(
            p, p, channel="r/IndieDev" if p == "reddit" else None, likes=5000, comments=400, audience=200
        )
        for p in ("reddit", "bluesky", "x", "youtube")
    ]
    signals = CommentSignals(sampled=100, distinct_commenters=60, intent_commenters=30, roblox_commenters=12)
    game = make_game(
        title="Proximity chat friendslop", steam=SteamInfo(appid=1, category_ids=[38], coming_soon=True)
    )
    result = score(game, mentions, signals, ctx=ctx)
    assert result.base == pytest.approx(100.0)
    assert result.score == 100.0


def test_score_and_features_bounded_for_random_games():
    rng = random.Random(99)
    ctx = ctx_with(samples("r/IndieDev", [rng.uniform(0, 20) for _ in range(10)]))
    for _ in range(300):
        game, mentions, signals = random_case(rng)
        weights = {k: rng.uniform(0, 1) for k in WEIGHTS}
        result = score(game, mentions, signals, ctx=ctx, weights=weights)
        assert 0.0 <= result.score <= 100.0
        assert all(0.0 <= v <= 1.0 for v in result.features.as_dict().values())
        assert result.excluded or 1 <= len(result.reasons) <= 4


# ----------------------------------------------------------------------------- prefilter


def test_prefilter_keeps_top_by_cheap_score_and_skips_excluded():
    ctx = ctx_with(samples("r/IndieDev", [2.0] * 10))
    games, mentions = {}, {}
    for i, likes in enumerate([5, 400, 80, 2000, 0]):
        m = make_mention("reddit", f"p{i}", channel="r/IndieDev", likes=likes, hours_ago=2)
        g = make_game(f"t:g{i}", f"Game {i}", mention_keys=[m.key], first_seen=NOW - timedelta(hours=i + 1))
        games[g.game_id], mentions[m.key] = g, m
    games["t:g3"].publisher = "Valve Corporation"
    games["t:g2"].excluded_reason = "banned keyword"
    ids = [*list(games), "t:missing", "t:g1"]
    assert prefilter(games, mentions, ids, ctx, WEIGHTS, BLOCK, limit=2) == ["t:g1", "t:g0"]
    assert prefilter(games, mentions, ids, ctx, WEIGHTS, BLOCK, limit=10) == ["t:g1", "t:g0", "t:g4"]
    assert prefilter(games, mentions, ids, ctx, WEIGHTS, BLOCK, limit=0) == []


def test_prefilter_ties_prefer_newest():
    games = {
        "t:old": make_game("t:old", "Old", first_seen=NOW - timedelta(hours=10)),
        "t:new": make_game("t:new", "New", first_seen=NOW - timedelta(hours=1)),
    }
    assert prefilter(games, {}, ["t:old", "t:new"], ctx_with(), WEIGHTS, BLOCK, limit=5) == ["t:new", "t:old"]


# ----------------------------------------------------------------------------- explain: helpers


def test_formatting_helpers():
    assert natural_join([]) == ""
    assert natural_join(["Reddit"]) == "Reddit"
    assert natural_join(["Reddit", "Steam"]) == "Reddit and Steam"
    assert natural_join(["Reddit", "Bluesky", "Steam"]) == "Reddit, Bluesky and Steam"
    assert [fmt_count(n) for n in (312, 2100, 48_250, 120_000, 1_250_000)] == [
        "312",
        "2,100",
        "48.2k",
        "120k",
        "1.2M",
    ]
    assert [fmt_hours(h) for h in (0.0, 0.4, 3.2, 47.4, 50, 100)] == [
        "1m",
        "24m",
        "3h",
        "47h",
        "2 days",
        "4 days",
    ]
    assert [fmt_multiple(m) for m in (9.04, 2.46, 2.0, 16.7)] == ["9", "2.5", "2", "17"]
    assert feed_label("itch:new-and-popular") == "New & Popular"
    assert feed_label(None) == "New & Popular"
    assert feed_label("itch:tag-horror") == "Tag Horror"


# ----------------------------------------------------------------------------- explain: lines


def result_with(**kw) -> ScoreResult:
    evidence = kw.pop("evidence", Evidence())
    features = kw.pop("features", Features())
    return ScoreResult(game_id="t:x", weights=dict(WEIGHTS), features=features, evidence=evidence, **kw)


def test_reasons_spec_examples_from_a_real_score():
    ctx = ctx_with(samples("r/IndieDev", [11.5] * 10))
    reddit = make_mention("reddit", "r1", channel="r/IndieDev", likes=312, hours_ago=3, audience=400_000)
    sky = make_mention("bluesky", "b1", likes=40, hours_ago=5, audience=900)
    steam = make_mention("steam", "77", hours_ago=10)
    signals = CommentSignals(sampled=60, distinct_commenters=40, intent_commenters=17)
    game = make_game("steam:77", "Gorilla Pizza Panic", first_seen=NOW - timedelta(hours=10))
    result = score(game, [reddit, sky, steam], signals, ctx=ctx)
    assert result.reasons[0] == "312 upvotes in 3h on r/IndieDev (9× normal for that sub)"
    assert "Seen on Reddit, Bluesky and Steam in the last 24h" in result.reasons
    assert "17 different people said they wishlisted or want to play with friends" in result.reasons
    assert 2 <= len(result.reasons) <= 4


def test_velocity_line_units_per_platform():
    ev = Evidence(
        best_source="bluesky",
        best_channel="bluesky",
        best_likes=480,
        best_comments=35,
        best_shares=60,
        best_age_hours=0.4,
        velocity_multiple=12.4,
    )
    r = result_with(evidence=ev, features=Features(velocity=0.9))
    assert build_reasons(r, make_game(), now=NOW)[0] == (
        "480 likes, 35 replies and 60 reposts in 24m on Bluesky (12× normal for Bluesky)"
    )
    ev = Evidence(
        best_source="youtube", best_likes=0, best_comments=12, best_age_hours=5, velocity_multiple=1.2
    )
    lines = build_reasons(result_with(evidence=ev, features=Features(velocity=0.0)), make_game(), now=NOW)
    assert "12 comments in 5h on YouTube" in lines


def test_itch_line_is_the_velocity_line_when_itch_drove_it():
    ev = Evidence(best_source="itch", best_rank=3, rank_channel="itch:new-and-popular", hours_on_list=9.2)
    lines = build_reasons(result_with(evidence=ev, features=Features(velocity=0.7)), make_game(), now=NOW)
    assert lines[0] == "#3 on itch.io New & Popular for 9h"
    ev.hours_on_list = 0.2
    lines = build_reasons(result_with(evidence=ev, features=Features(velocity=0.7)), make_game(), now=NOW)
    assert lines[0] == "#3 on itch.io New & Popular (just arrived)"


def test_itch_line_is_secondary_when_a_post_drove_velocity():
    ev = Evidence(
        best_source="reddit",
        best_channel="r/godot",
        best_likes=90,
        best_age_hours=2,
        velocity_multiple=4,
        best_rank=12,
        rank_channel="itch:new-and-popular",
        hours_on_list=30,
    )
    lines = build_reasons(result_with(evidence=ev, features=Features(velocity=0.5)), make_game(), now=NOW)
    assert lines[:2] == [
        "90 upvotes in 2h on r/godot (4× normal for that sub)",
        "#12 on itch.io New & Popular for 30h",
    ]


def test_steam_follower_line():
    ev = Evidence(best_source="steam", follower_growth=340)
    lines = build_reasons(result_with(evidence=ev, features=Features(velocity=0.6)), make_game(), now=NOW)
    assert lines[0] == "+340 Steam followers in the last few days"


def underdog_ev(source, *, likes=0, comments=0, shares=0, audience=None, channel=None) -> Evidence:
    return Evidence(
        underdog_source=source,
        underdog_channel=channel,
        underdog_likes=likes,
        underdog_comments=comments,
        underdog_shares=shares,
        underdog_audience=audience,
    )


def test_underdog_lines():
    ev = underdog_ev("bluesky", likes=480, audience=2100)
    lines = build_reasons(result_with(evidence=ev, features=Features(underdog=0.8)), make_game(), now=NOW)
    assert "Small creator: 2,100 followers, 480 likes" in lines
    ev = underdog_ev("x", comments=30, shares=5, audience=150)
    lines = build_reasons(result_with(evidence=ev, features=Features(underdog=0.8)), make_game(), now=NOW)
    assert "Small creator: 150 followers, 35 reactions" in lines
    ev = underdog_ev("reddit", channel="r/CoOpGaming", likes=250, audience=21_000)
    lines = build_reasons(result_with(evidence=ev, features=Features(underdog=0.4)), make_game(), now=NOW)
    assert "Big reaction for a small sub: 250 upvotes in r/CoOpGaming (21k members)" in lines
    ev = underdog_ev("bluesky", likes=30_000, audience=48_000)
    lines = build_reasons(result_with(evidence=ev, features=Features(underdog=0.9)), make_game(), now=NOW)
    assert "Big reaction for the account's size: 30k likes with 48k followers" in lines
    for ev, underdog in (
        (underdog_ev("reddit", channel="r/CoOpGaming", audience=21_000), 0.4),
        (underdog_ev("reddit", channel="r/gamedev", likes=9, audience=1_900_000), 0.4),
        (underdog_ev("bluesky", likes=3, audience=5_000), 0.29),
        (underdog_ev("bluesky", audience=10), 0.4),
        (underdog_ev(None, audience=10), 0.4),
        # the velocity post's numbers are not the underdog post's numbers
        (Evidence(best_source="bluesky", best_likes=480, audience=2100), 0.8),
    ):
        lines = build_reasons(
            result_with(evidence=ev, features=Features(underdog=underdog)), make_game(), now=NOW
        )
        claims = ("small", "followers", "members")
        assert not any(word in line.lower() for line in lines for word in claims), lines
    # a strong underdog value without quotable numbers only gets the generic filler line
    ev = Evidence(best_source="bluesky", best_likes=480, best_age_hours=3, audience=2100)
    lines = build_reasons(result_with(evidence=ev, features=Features(underdog=0.8)), make_game(), now=NOW)
    assert lines == ["480 likes in 3h on Bluesky", "Big reaction for the size of its audience"]


def test_underdog_line_quotes_the_underdog_post_not_the_velocity_post():
    """A hot Bluesky post drives velocity; a 500-upvote post in a 200-member sub drives underdog."""
    bsky = make_mention("bluesky", "b1", title="Moon Goblins", likes=50, hours_ago=2, audience=5000)
    reddit = make_mention(
        "reddit", "r1", title="Moon Goblins", likes=500, hours_ago=70, audience=200, channel="r/tinysub"
    )
    result = score(make_game(), [bsky, reddit])
    ev = result.evidence
    assert result.features.underdog == 1.0
    assert (ev.best_source, ev.best_likes, ev.audience) == ("bluesky", 50, 5000)  # velocity post
    assert (ev.underdog_mention_key, ev.underdog_source, ev.underdog_channel) == (
        "reddit:r1",
        "reddit",
        "r/tinysub",
    )
    assert (ev.underdog_likes, ev.underdog_audience) == (500, 200)
    assert "Big reaction for a small sub: 500 upvotes in r/tinysub (200 members)" in result.reasons
    assert not any("5,000 followers" in line for line in result.reasons)
    # no audience known anywhere: no underdog evidence, no underdog line
    plain = score(make_game(), [make_mention(likes=500)])
    assert plain.evidence.underdog_source is None and plain.features.underdog == 0.0


def test_cross_line_falls_back_to_three_days():
    ev = Evidence(sources_72h=["reddit", "itch"], sources_24h=["reddit"])
    lines = build_reasons(result_with(evidence=ev, features=Features(cross=0.6)), make_game(), now=NOW)
    assert lines[0] == "Seen on Reddit and itch.io in the last 3 days"
    ev = Evidence(sources_72h=["reddit"], sources_24h=["reddit"])
    lines = build_reasons(result_with(evidence=ev, features=Features(cross=0.6)), make_game(), now=NOW)
    assert not any(line.startswith("Seen on") for line in lines)


def test_hype_line_singular():
    ev = Evidence(signals=CommentSignals(sampled=3, distinct_commenters=3, intent_commenters=1))
    lines = build_reasons(result_with(evidence=ev, features=Features(hype=0.4)), make_game(), now=NOW)
    assert lines[0] == "1 person said they wishlisted it or want to play it with friends"


def test_roblox_line_is_pinned_when_bonus_applied():
    ev = Evidence(
        best_source="reddit",
        best_channel="r/IndieGaming",
        best_likes=900,
        best_age_hours=2,
        velocity_multiple=14,
        fit_hits=["proximity chat"],
        sources_24h=["reddit", "bluesky"],
        sources_72h=["reddit", "bluesky"],
        signals=CommentSignals(roblox_commenters=11, intent_commenters=9),
        meme_jokers=11,
    )
    features = Features(velocity=1, cross=0.6, fit=1, hype=1, meme=0.6, fresh=1, underdog=1)
    bonus = Adjustment(code="roblox_bonus", points=8)
    lines = build_reasons(
        result_with(evidence=ev, features=features, bonuses=[bonus]), make_game(), now=NOW, max_reasons=2
    )
    assert len(lines) == 2
    assert "🧱 Roblox-clone discourse: 11 commenters joking it's a Roblox game" in lines
    no_bonus = build_reasons(result_with(evidence=ev, features=features), make_game(), now=NOW, max_reasons=2)
    assert not any(line.startswith("🧱") for line in no_bonus)
    ev.signals = CommentSignals(roblox_commenters=1)
    ev.meme_jokers = 1
    lines = build_reasons(result_with(evidence=ev, features=Features(meme=0.2)), make_game(), now=NOW)
    assert lines[0] == "🧱 Roblox-clone discourse: 1 commenter joking it's a Roblox game"


def test_fit_lines_keywords_or_llm():
    ev = Evidence(fit_hits=["proximity chat", "co-op horror", "ragdoll", "chaos"])
    lines = build_reasons(result_with(evidence=ev, features=Features(fit=1)), make_game(), now=NOW)
    assert lines[0] == "Friendslop fit: proximity chat, co-op horror, ragdoll"
    ev = Evidence(llm_fit=0.82)
    lines = build_reasons(result_with(evidence=ev, features=Features(fit=0.7)), make_game(), now=NOW)
    assert lines[0] == "AI check says it's a friends-co-op game (0.8 out of 1)"
    ev = Evidence(llm_fit=0.2)
    lines = build_reasons(result_with(evidence=ev, features=Features(fit=0.2)), make_game(), now=NOW)
    assert not any(line.startswith("AI check") for line in lines)


@pytest.mark.parametrize(
    ("steam", "expected"),
    [
        (
            SteamInfo(
                appid=1,
                categories=["Single-player", "Multi-player", "Online Co-op", "Co-op"],
                coming_soon=True,
            ),
            "Steam page: Online Co-op, Co-op, coming soon",
        ),
        (
            SteamInfo(appid=1, categories=["Co-op"], coming_soon=True, release_date_text="Q1 2027"),
            "Steam page: Co-op, coming Q1 2027",
        ),
        (
            SteamInfo(appid=1, categories=["Online PvP"], release_date=date(2026, 11, 14)),
            "Steam page: Online PvP, out 14 Nov 2026",
        ),
        (SteamInfo(appid=1, early_access=True, release_date=date(2026, 9, 30)), "Steam page: Early Access"),
    ],
)
def test_steam_line(steam, expected):
    game = make_game(steam=steam)
    lines = build_reasons(result_with(features=Features(fit=0.8, fresh=1)), game, now=NOW)
    assert expected in lines


def test_no_steam_line_without_interesting_details():
    game = make_game(steam=SteamInfo(appid=1, categories=["Single-player"]))
    lines = build_reasons(result_with(features=Features(fit=0.8)), game, now=NOW)
    assert not any(line.startswith("Steam page") for line in lines)


def test_fresh_lines():
    ev = Evidence(first_seen=NOW - timedelta(hours=5), sources_72h=["steam"])
    lines = build_reasons(result_with(evidence=ev, features=Features(fresh=1)), make_game(), now=NOW)
    assert lines[0] == "New Steam page, first seen 5h ago"
    ev = Evidence(first_seen=NOW - timedelta(hours=30), sources_72h=["reddit"])
    lines = build_reasons(result_with(evidence=ev, features=Features(fresh=1)), make_game(), now=NOW)
    assert lines[0] == "New find: first seen 30h ago"
    ev = Evidence(first_seen=NOW - timedelta(days=9), sources_72h=["reddit"])
    lines = build_reasons(result_with(evidence=ev, features=Features(fresh=0.4)), make_game(), now=NOW)
    assert not any("first seen" in line for line in lines)


def test_reasons_never_empty_and_respect_max():
    lines = build_reasons(ScoreResult(game_id="t:x"), make_game(title="Quiet Game"), now=NOW)
    assert lines == ["Quiet Game turned up in GemBot's scan (Gem Score 0)"]
    ev = Evidence(sources_72h=["reddit", "itch"])
    assert build_reasons(result_with(evidence=ev), make_game(), now=NOW) == ["Spotted on Reddit and itch.io"]
    ev = Evidence(
        best_source="reddit",
        best_channel="r/x",
        best_likes=10,
        best_age_hours=1,
        fit_hits=["co-op"],
        first_seen=NOW,
        sources_72h=["reddit"],
    )
    weak = result_with(evidence=ev, features=Features(fit=0.04, fresh=0.05))
    lines = build_reasons(weak, make_game(), now=NOW)
    assert len(lines) == 2  # one strong line padded with the best filler
    full = result_with(
        evidence=Evidence(
            best_source="reddit",
            best_channel="r/x",
            best_likes=500,
            best_age_hours=1,
            velocity_multiple=16,
            fit_hits=["co-op"],
            first_seen=NOW,
            sources_24h=["reddit", "x"],
            sources_72h=["reddit", "x"],
            signals=CommentSignals(intent_commenters=5),
        ),
        features=Features(velocity=1, fit=1, fresh=1, cross=0.6, hype=0.6),
    )
    assert len(build_reasons(full, make_game(), now=NOW)) == 4
    assert len(build_reasons(full, make_game(), now=NOW, max_reasons=3)) == 3
    assert len(build_reasons(full, make_game(), now=NOW, max_reasons=0)) == 1
    unweighted = full.model_copy(update={"weights": {}})
    assert len(build_reasons(unweighted, make_game(), now=NOW)) == 4


def test_roundup_worthy_game_gets_at_least_two_reasons():
    """BUILD_SPEC 4.6 "2-4 reasons": one hot post in a huge sub (velocity 1.0), one older post
    with a big reaction in a tiny sub (underdog 1.0), first seen 8 days ago (fresh 0.5)."""
    hot = make_mention(
        "reddit", "hot", title="Moon Goblins", likes=200, hours_ago=1, audience=1_500_000, channel="r/gamedev"
    )
    small = make_mention(
        "reddit", "small", title="Moon Goblins", likes=300, hours_ago=70, audience=50, channel="r/tinysub"
    )
    old = make_mention("reddit", "old", title="Moon Goblins", likes=1, hours_ago=8 * 24, channel="r/tinysub")
    game = make_game(first_seen=NOW - timedelta(days=8))
    result = score(game, [hot, small, old])
    assert result.score >= CONFIG.settings.decisions.roundup_score
    assert result.reasons == [
        "200 upvotes in 1h on r/gamedev (67× normal for that sub)",
        "Big reaction for a small sub: 300 upvotes in r/tinysub (50 members)",
    ]


def one_line_result(**evidence) -> ScoreResult:
    """A game whose only strong feature is a velocity line on r/IndieDev."""
    ev = Evidence(
        best_source="reddit",
        best_channel="r/IndieDev",
        best_likes=300,
        best_age_hours=2,
        velocity_multiple=12,
        sources_72h=["reddit"],
        sources_24h=["reddit"],
    )
    return result_with(evidence=ev.model_copy(update=evidence), features=Features(velocity=0.9))


VELOCITY_LINE = "300 upvotes in 2h on r/IndieDev (12× normal for that sub)"


def test_fillers_pad_a_single_reason_to_two_in_order():
    old = NOW - timedelta(days=9)
    steam_game = make_game(steam=SteamInfo(appid=5, genres=["Action", "Indie", "Casual"], price="$4.99"))
    # 1. the strongest feature without a line of its own, in general words
    generic = one_line_result(first_seen=old)
    generic.features.underdog = 0.7
    assert build_reasons(generic, steam_game, now=NOW) == [
        VELOCITY_LINE,
        "Big reaction for the size of its audience",
    ]
    # 2. facts from the Steam page
    assert build_reasons(one_line_result(first_seen=old), steam_game, now=NOW) == [
        VELOCITY_LINE,
        "Steam page: Action, Indie, $4.99",
    ]
    free = make_game(steam=SteamInfo(appid=6, is_free=True))
    assert build_reasons(one_line_result(), free, now=NOW)[1] == "Steam page: free to play"
    # 3. when GemBot first spotted it (the "New find" line stops after 7 days)
    assert build_reasons(one_line_result(first_seen=old), make_game(), now=NOW) == [
        VELOCITY_LINE,
        "First spotted 9 days ago",
    ]
    # 4. where it was seen
    assert build_reasons(one_line_result(), make_game(), now=NOW) == [VELOCITY_LINE, "Spotted on r/IndieDev"]
    two_places = one_line_result(sources_72h=["reddit", "itch"])
    assert build_reasons(two_places, make_game(), now=NOW)[1] == "Spotted on r/IndieDev and itch.io"


def test_fillers_never_repeat_a_topic_or_exceed_two():
    # a weak (but real) fresh line already covers "first seen": no "First spotted" filler
    recent = one_line_result(first_seen=NOW - timedelta(hours=30))
    recent.features.fresh = 0.05
    assert build_reasons(recent, make_game(), now=NOW) == [VELOCITY_LINE, "New find: first seen 30h ago"]
    # the co-op Steam line covers the Steam page: no genre/price filler
    coop = make_game(steam=SteamInfo(appid=7, categories=["Online Co-op"], genres=["Action"], price="$1"))
    assert build_reasons(one_line_result(), coop, now=NOW)[1] == "Steam page: Online Co-op"
    # weak features below GENERIC_LINE_MIN get no generic line
    weak = one_line_result()
    weak.features.underdog = 0.3
    assert "Big reaction for the size of its audience" not in build_reasons(weak, make_game(), now=NOW)
    # two strong lines: no filler at all
    strong = one_line_result(fit_hits=["proximity chat"])
    strong.features.fit = 1.0
    assert build_reasons(strong, make_game(), now=NOW) == [VELOCITY_LINE, "Friendslop fit: proximity chat"]
    # a cross line makes "Spotted on" redundant
    cross = one_line_result(sources_72h=["reddit", "itch"], sources_24h=["reddit", "itch"])
    cross.features.cross = 0.6
    lines = build_reasons(cross, make_game(), now=NOW)
    assert lines == [VELOCITY_LINE, "Seen on Reddit and itch.io in the last 24h"]


def test_every_scored_game_with_a_mention_gets_two_to_four_reasons():
    rng = random.Random(77)
    ctx = ctx_with(samples("r/IndieDev", [rng.uniform(0, 50) for _ in range(12)]))
    for _ in range(300):
        game, mentions, signals = random_case(rng)
        result = score_game(game, mentions, ctx, WEIGHTS, Blocklist(), signals)
        if mentions:
            assert 2 <= len(result.reasons) <= 4, result.reasons
        assert len(set(result.reasons)) == len(result.reasons)


def test_llm_present_game_still_scores():
    game = make_game(
        llm=LLMVerdict(friendslop_fit=0.9, is_a_specific_game=True, one_line_pitch="Co-op chaos")
    )
    result = score(game, [])
    assert result.features.fit == pytest.approx(0.72)
    assert any(line.startswith("AI check") for line in result.reasons)
