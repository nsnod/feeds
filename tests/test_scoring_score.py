"""Gem Score, exclusions, penalties, the Roblox bonus, prefilter and the "why" lines."""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from gembot.config import Blocklist
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
    assert score(game, [], negativity(4, 4)).penalties == []  # too few commenters
    assert score(game, []).penalties == []


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


def test_underdog_lines():
    ev = Evidence(best_source="bluesky", best_likes=480, audience=2100)
    lines = build_reasons(result_with(evidence=ev, features=Features(underdog=0.8)), make_game(), now=NOW)
    assert "Small creator: 2,100 followers, 480 likes" in lines
    ev = Evidence(best_source="x", best_comments=30, best_shares=5, audience=150)
    lines = build_reasons(result_with(evidence=ev, features=Features(underdog=0.8)), make_game(), now=NOW)
    assert "Small creator: 150 followers, 35 reactions" in lines
    ev = Evidence(best_source="reddit", best_channel="r/CoOpGaming", best_likes=250, audience=21_000)
    lines = build_reasons(result_with(evidence=ev, features=Features(underdog=0.4)), make_game(), now=NOW)
    assert "Big reaction for a small sub: 250 upvotes in r/CoOpGaming (21k members)" in lines
    ev = Evidence(best_source="bluesky", best_likes=30_000, audience=48_000)
    lines = build_reasons(result_with(evidence=ev, features=Features(underdog=0.9)), make_game(), now=NOW)
    assert "Big reaction for the account's size: 30k likes with 48k followers" in lines
    for ev, underdog in (
        (Evidence(best_source="reddit", best_channel="r/CoOpGaming", audience=21_000), 0.4),
        (Evidence(best_source="reddit", best_channel="r/gamedev", best_likes=9, audience=1_900_000), 0.4),
        (Evidence(best_source="bluesky", best_likes=3, audience=5_000), 0.29),
        (Evidence(best_source="bluesky", audience=10), 0.4),
        (Evidence(best_source=None, audience=10), 0.4),
    ):
        lines = build_reasons(
            result_with(evidence=ev, features=Features(underdog=underdog)), make_game(), now=NOW
        )
        assert not any("small" in line.lower() or "size" in line for line in lines)


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
    assert build_reasons(result_with(evidence=ev), make_game(), now=NOW) == [
        "New indie game spotted on Reddit and itch.io"
    ]
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


def test_llm_present_game_still_scores():
    game = make_game(
        llm=LLMVerdict(friendslop_fit=0.9, is_a_specific_game=True, one_line_pitch="Co-op chaos")
    )
    result = score(game, [])
    assert result.features.fit == pytest.approx(0.72)
    assert any(line.startswith("AI check") for line in result.reasons)
