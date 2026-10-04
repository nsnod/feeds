"""The 10 scoring scenarios from BUILD_SPEC section 8, end to end through the scorer.

Every scenario is built from synthetic mentions / comment signals / channel baselines and
run through ``score_game`` -> ``plan_posts`` (-> ``commit_plan``) with the DEFAULT
``config/*.yaml``. Nothing is special-cased: the outcomes follow from the formulas.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pytest

from gembot.models import (
    BaselineSample,
    CommentSignals,
    DecisionKind,
    Game,
    Mention,
    Meta,
    PostedState,
    ScoreResult,
    SteamInfo,
)
from gembot.scoring import PostPlan, ScoringContext, commit_plan, plan_posts, score_game
from tests.factories import NOW, make_config, make_game, make_mention

CONFIG = make_config()
SETTINGS = CONFIG.settings
DECISIONS = SETTINGS.decisions


def baseline(channel: str, typical_eph: float, n: int = 14) -> dict[str, dict[str, BaselineSample]]:
    """``n`` past posts in ``channel`` whose median engagement/hour is ``typical_eph``."""
    spread = [0.4, 0.6, 0.8, 0.9, 1.0, 1.0, 1.0, 1.0, 1.1, 1.3, 1.6, 2.0, 3.0, 5.0][:n]
    return {
        channel: {
            f"{channel}-past-{i}": BaselineSample(at=NOW - timedelta(hours=10 * i + 2), eph=typical_eph * s)
            for i, s in enumerate(spread)
        }
    }


BASELINES = (
    baseline("r/SoloDevelopment", 6.0)
    | baseline("r/IndieGaming", 8.0)
    | baseline("r/CoOpGaming", 4.0)
    | baseline("r/gamedev", 10.0)
    | baseline("bluesky", 1.2)
)


@dataclass
class Case:
    game: Game
    mentions: list[Mention]
    signals: CommentSignals | None = None
    results: list[ScoreResult] = field(default_factory=list)


def score(case: Case, *, now: datetime = NOW) -> ScoreResult:
    ctx = ScoringContext.from_config(CONFIG, now=now, baselines=BASELINES)
    result = score_game(case.game, case.mentions, ctx, SETTINGS.weights, CONFIG.blocklist, case.signals)
    # what the pipeline does after scoring
    case.game.last_score = result.score
    case.game.last_scored_at = now
    case.game.last_reasons = result.reasons
    case.results.append(result)
    return result


def decide(
    cases: list[Case], *, posted: PostedState | None = None, meta: Meta | None = None, now: datetime = NOW
) -> PostPlan:
    results = {c.game.game_id: score(c, now=now) for c in cases}
    games = {c.game.game_id: c.game for c in cases}
    return plan_posts(results, games, posted or PostedState(), meta or Meta(), now=now, settings=DECISIONS)


def kind_of(plan: PostPlan, game_id: str) -> str:
    if any(d.game_id == game_id for d in plan.alarms):
        return "alarm"
    if any(d.game_id == game_id for d in plan.roundup):
        return "roundup"
    return "nothing"


def coop_steam(appid: int, name: str, **kw) -> SteamInfo:
    kw.setdefault("categories", ["Single-player", "Multi-player", "Co-op", "Online Co-op"])
    kw.setdefault("category_ids", [2, 1, 9, 38])
    kw.setdefault("coming_soon", True)
    kw.setdefault("release_date_text", "Coming soon")
    return SteamInfo(
        appid=appid, name=name, developers=["Tiny Ape Games"], publishers=["Tiny Ape Games"], **kw
    )


def tiny_dev_game(
    appid: int = 1001,
    title: str = "Gorilla Pizza Panic",
    *,
    likes: int = 140,
    comments: int = 40,
    hours_ago: float = 3,
    signals: CommentSignals | None = None,
    first_seen_hours: float = 5,
) -> Case:
    """A solo dev's first Steam-page post on r/SoloDevelopment (18k members), plus the new
    Steam listing GemBot found by itself."""
    game = make_game(
        f"steam:{appid}",
        title,
        steam=coop_steam(appid, title, short_description=f"{title}: chaotic pizza delivery for 1-4 players."),
        steam_appid=appid,
        first_seen=NOW - timedelta(hours=first_seen_hours),
    )
    post = make_mention(
        "reddit",
        f"t3_{appid}",
        title=f"After two years solo, my co-op game {title} finally has a Steam page!",
        text="Proximity chat, ragdoll physics, up to 4 players. Wishlists mean the world to me <3",
        channel="r/SoloDevelopment",
        audience=18_000,
        author=f"dev_{appid}",
        hours_ago=hours_ago,
        likes=likes,
        comments=comments,
        links=[f"https://store.steampowered.com/app/{appid}/"],
    )
    listing = make_mention(
        "steam",
        str(appid),
        title=title,
        channel="steam:comingsoon-indie-online-coop",
        author="Tiny Ape Games",
        hours_ago=first_seen_hours,
    )
    game.mention_keys = [post.key, listing.key]
    wishlists = CommentSignals(
        sampled=40,
        distinct_commenters=30,
        intent_comments=17,
        intent_commenters=15,
        negative_commenters=1,
        post_comment_count=comments,
        intent_examples=["wishlisted!", "me and the boys need this"],
    )
    return Case(game, [post, listing], wishlists if signals is None else signals)


# ----------------------------------------------------------------------------- 1


def test_scenario_1_tiny_dev_at_10x_normal_with_wishlist_comments_alarms():
    case = tiny_dev_game()
    plan = decide([case])
    result = case.results[-1]
    assert result.evidence.velocity_multiple == pytest.approx(10.0)  # 180 in 3h = 60/h vs 6/h
    assert result.features.velocity > 0.8
    assert result.features.hype > 0.9
    assert result.score >= DECISIONS.alarm_score
    assert set(result.alarm_signals) >= {"velocity", "hype"}
    assert kind_of(plan, case.game.game_id) == "alarm"
    assert not plan.alarms[0].escalated
    assert (
        result.reasons[0]
        == "140 upvotes and 40 comments in 3h on r/SoloDevelopment (10× normal for that sub)"
    )
    assert "15 different people said they wishlisted or want to play with friends" in result.reasons
    assert 2 <= len(result.reasons) <= 4


# ----------------------------------------------------------------------------- 2


def test_scenario_2_big_studio_with_huge_numbers_is_excluded():
    game = make_game(
        "steam:2002",
        "Tom Clancy's Squad Chaos",
        steam=SteamInfo(
            appid=2002, developers=["Ubisoft Montreal"], publishers=["Ubisoft"], category_ids=[38]
        ),
        steam_appid=2002,
    )
    mentions = [
        make_mention("reddit", "t3_big", channel="r/IndieGaming", likes=25_000, comments=3_000, hours_ago=2),
        make_mention("bluesky", "big", likes=9_000, shares=2_000, hours_ago=3, audience=150),
        make_mention("x", "big", likes=40_000, hours_ago=1),
        make_mention("steam", "2002", hours_ago=4),
    ]
    signals = CommentSignals(sampled=100, distinct_commenters=90, intent_commenters=60, roblox_commenters=20)
    case = Case(game, mentions, signals)
    plan = decide([case])
    result = case.results[-1]
    assert result.excluded and result.score == 0.0
    assert result.exclude_reason == "big studio: Ubisoft (developer 'Ubisoft Montreal')"
    assert plan.roundup_due
    assert kind_of(plan, game.game_id) == "nothing"


# ----------------------------------------------------------------------------- 3


@pytest.mark.parametrize("perfect_rest", [False, True])
def test_scenario_3_single_source_average_engagement_is_roundup_at_most(perfect_rest):
    game = make_game("t:cellar-crew-1a2b3c", "Cellar Crew", first_seen=NOW - timedelta(hours=5))
    # 22 upvotes + 8 comments in 5h = 6/h; r/CoOpGaming's normal is 4/h -> 1.5x
    post = make_mention(
        "reddit",
        "t3_avg",
        title="Cellar Crew - a co-op horror game you play with friends, proximity chat included",
        channel="r/CoOpGaming",
        audience=2_500 if perfect_rest else 60_000,
        likes=22,
        comments=8,
        hours_ago=5,
    )
    if perfect_rest:  # every non-velocity feature as good as it gets for one post
        signals = CommentSignals(sampled=8, distinct_commenters=7, intent_commenters=7, post_comment_count=8)
    else:
        signals = CommentSignals(sampled=8, distinct_commenters=7, intent_commenters=3, post_comment_count=8)
    case = Case(game, [post], signals)
    plan = decide([case])
    result = case.results[-1]
    assert result.evidence.velocity_multiple == pytest.approx(1.5)
    assert result.features.cross == 0.0
    assert result.score < DECISIONS.alarm_score
    assert plan.alarms == []
    if perfect_rest:
        assert result.score >= DECISIONS.roundup_score
        assert kind_of(plan, game.game_id) == "roundup"
    else:
        assert result.score < DECISIONS.roundup_score
        assert kind_of(plan, game.game_id) == "nothing"


# ----------------------------------------------------------------------------- 4


def test_scenario_4_roblox_clone_discourse_with_decent_velocity_alarms():
    game = make_game(
        "steam:4004",
        "Blocky Brawl Buddies",
        steam=coop_steam(4004, "Blocky Brawl Buddies"),
        steam_appid=4004,
        first_seen=NOW - timedelta(hours=6),
    )
    # 180 upvotes + 40 comments in 4h = 55/h vs r/IndieGaming's 8/h -> ~6.9x
    post = make_mention(
        "reddit",
        "t3_blocky",
        title="Our chaotic physics co-op game you play with friends",
        channel="r/IndieGaming",
        audience=450_000,
        likes=180,
        comments=40,
        hours_ago=4,
    )
    listing = make_mention("steam", "4004", channel="steam:comingsoon-indie-coop", hours_ago=6)
    signals = CommentSignals(
        sampled=40,
        distinct_commenters=30,
        intent_commenters=8,
        roblox_comments=14,
        roblox_commenters=12,
        post_comment_count=40,
        roblox_examples=["this is a roblox game", "roblox clone lol"],
    )
    case = Case(game, [post, listing], signals)
    plan = decide([case])
    result = case.results[-1]
    assert result.features.meme >= 0.6
    assert [b.code for b in result.bonuses] == ["roblox_bonus"]
    assert result.score == pytest.approx(result.base + 8, abs=0.1)
    assert result.base < DECISIONS.alarm_score <= result.score  # the bonus is what tips it over
    assert set(result.alarm_signals) >= {"velocity", "meme"}
    assert kind_of(plan, game.game_id) == "alarm"
    assert "🧱 Roblox-clone discourse: 12 commenters joking it's a Roblox game" in result.reasons


# ----------------------------------------------------------------------------- 5


def test_scenario_5_roblox_flood_on_a_six_comment_post_is_not_counted():
    game = make_game("t:roblocks-5e5e5e", "Roblocks Party", first_seen=NOW - timedelta(hours=3))
    post = make_mention(
        "reddit", "t3_tiny", title="My party game", channel="r/gamedev", likes=15, comments=6, hours_ago=3
    )
    signals = CommentSignals(
        sampled=6, distinct_commenters=6, roblox_comments=6, roblox_commenters=5, post_comment_count=6
    )
    case = Case(game, [post], signals)
    plan = decide([case])
    result = case.results[-1]
    assert result.evidence.total_comments == 6
    assert result.features.meme == 0.0
    assert result.bonuses == []
    assert not any(r.startswith("🧱") for r in result.reasons)
    assert kind_of(plan, game.game_id) == "nothing"


# ----------------------------------------------------------------------------- 6


def test_scenario_6_three_platforms_low_engagement_reaches_roundup_not_alarm():
    game = make_game(
        "steam:6006",
        "Night Shift Janitors",
        steam=coop_steam(
            6006, "Night Shift Janitors", short_description="Co-op horror cleaning with proximity chat."
        ),
        steam_appid=6006,
        first_seen=NOW - timedelta(hours=12),
    )
    mentions = [
        # 6 upvotes + 2 comments in 6h = 1.3/h; r/CoOpGaming's normal is 4/h -> below normal
        make_mention(
            "reddit", "t3_nsj", channel="r/CoOpGaming", likes=6, comments=2, hours_ago=6, audience=60_000
        ),
        # 9 likes + 2 replies + 1 repost in 5h = 2.4/h; Bluesky's normal is 1.2/h -> 2x
        make_mention("bluesky", "nsj", likes=9, comments=2, shares=1, hours_ago=5, audience=400),
        make_mention("steam", "6006", channel="steam:comingsoon-indie-coop", hours_ago=12),
    ]
    case = Case(game, mentions, None)
    plan = decide([case])
    result = case.results[-1]
    f = result.features
    assert f.velocity < 0.5 and f.hype == 0.0 and f.meme == 0.0
    assert f.cross == pytest.approx(0.85)
    assert result.alarm_signals == ["cross"]
    cross_points = 100 * result.weights["cross"] * f.cross
    assert result.score >= DECISIONS.roundup_score > result.score - cross_points  # cross lifts it in
    assert kind_of(plan, game.game_id) == "roundup"
    assert "Seen on Reddit, Bluesky and Steam in the last 24h" in result.reasons


# ----------------------------------------------------------------------------- 7


def test_scenario_7_strong_negativity_is_penalised_and_never_alarms():
    hated = CommentSignals(
        sampled=40,
        distinct_commenters=30,
        intent_commenters=6,
        negative_commenters=12,
        post_comment_count=40,
        negative_terms=["asset flip", "scam"],
    )
    case = tiny_dev_game(7007, "Pizza Panic Ultimate", signals=hated)
    clean = tiny_dev_game(7008, "Pizza Panic Ultimate Clone")
    plan = decide([case, clean])
    result = case.results[-1]
    assert [p.code for p in result.penalties] == ["negativity"]
    assert result.penalties[0].points == 15
    assert result.score == pytest.approx(result.base - 15, abs=0.1)
    assert "velocity" in result.alarm_signals  # still fast, but...
    assert result.score < DECISIONS.alarm_score
    assert kind_of(plan, case.game.game_id) != "alarm"
    assert kind_of(plan, clean.game.game_id) == "alarm"  # same numbers without the hate do alarm


# ----------------------------------------------------------------------------- 8


def test_scenario_8_five_qualifying_games_three_alarms_two_carried_to_next_roundup():
    cases = [
        tiny_dev_game(8000 + i, f"Friendslop Game {i}", likes=140 + 15 * i, comments=40 + i) for i in range(5)
    ]
    posted = PostedState()
    meta = Meta(last_roundup_at=NOW - timedelta(minutes=30))  # roundup not due this run
    plan = decide(cases, posted=posted, meta=meta)
    assert all(c.results[-1].score >= DECISIONS.alarm_score for c in cases)
    ranked = sorted(cases, key=lambda c: -c.results[-1].score)
    alarmed = [c.game.game_id for c in ranked[:3]]
    carried = [c.game.game_id for c in ranked[3:]]
    assert [d.game_id for d in plan.alarms] == alarmed
    assert plan.carried == carried
    assert plan.roundup == []
    commit_plan(plan, posted, meta, now=NOW)
    assert all(posted.games[g].pending_roundup and posted.games[g].would_have_alarmed for g in carried)

    # next run, 30 min later: still hot, but they wait for the roundup instead of alarming
    later = NOW + timedelta(minutes=30)
    plan = decide(cases, posted=posted, meta=meta, now=later)
    assert plan.alarms == [] and not plan.roundup_due
    commit_plan(plan, posted, meta, now=later)

    # the roundup run: the two carried games lead it, marked "would have alarmed"
    roundup_time = NOW + timedelta(minutes=120)
    plan = decide(cases, posted=posted, meta=meta, now=roundup_time)
    assert plan.roundup_due and plan.alarms == []
    assert [d.game_id for d in plan.roundup[:2]] == carried
    assert all(d.would_have_alarmed and d.kind == DecisionKind.ROUNDUP for d in plan.roundup[:2])
    assert not {d.game_id for d in plan.roundup} & set(alarmed)
    commit_plan(plan, posted, meta, now=roundup_time)
    assert all(
        not posted.games[g].pending_roundup and posted.games[g].roundup_at == roundup_time for g in carried
    )
    assert meta.alarms_on(NOW.date()) == 3


# ----------------------------------------------------------------------------- 9


def test_scenario_9_roundup_game_that_later_crosses_alarm_threshold_escalates():
    posted, meta = PostedState(), Meta()
    early = NOW - timedelta(hours=6)
    # six hours ago: a 1h-old post with 12 upvotes + 3 comments, a couple of wishlist replies
    quiet = tiny_dev_game(
        9009,
        "Goose Heist Co-op",
        likes=12,
        comments=3,
        hours_ago=1,
        first_seen_hours=2,
        signals=CommentSignals(sampled=3, distinct_commenters=3, intent_commenters=2, post_comment_count=3),
    )
    for m in quiet.mentions:  # shift the snapshot back in time
        m.created_at -= timedelta(hours=6)
    quiet.game.first_seen -= timedelta(hours=6)
    plan = decide([quiet], posted=posted, meta=meta, now=early)
    first = quiet.results[-1]
    assert DECISIONS.roundup_score <= first.score < DECISIONS.alarm_score
    assert kind_of(plan, quiet.game.game_id) == "roundup"
    commit_plan(plan, posted, meta, now=early)
    assert posted.games[quiet.game.game_id].roundup_at == early

    # now: the same post took off (10x normal) and the comments are full of wishlists
    hot = tiny_dev_game(9009, "Goose Heist Co-op", first_seen_hours=8)
    plan = decide([hot], posted=posted, meta=meta)
    assert hot.results[-1].score >= DECISIONS.alarm_score
    assert [(d.game_id, d.escalated) for d in plan.alarms] == [(hot.game.game_id, True)]
    commit_plan(plan, posted, meta, now=NOW)
    assert posted.games[hot.game.game_id].alarmed_at == NOW


# ----------------------------------------------------------------------------- 10


def test_scenario_10_roundup_with_nothing_above_45_posts_nothing():
    cases = []
    for i, (likes, comments) in enumerate([(3, 0), (9, 2), (20, 4)]):
        game = make_game(f"t:meh-{i}", f"Spreadsheet Simulator {i}", first_seen=NOW - timedelta(days=4))
        post = make_mention(
            "reddit",
            f"t3_meh{i}",
            title="Feedback on my solo puzzle game?",
            channel="r/gamedev",
            likes=likes,
            comments=comments,
            hours_ago=20,
            audience=1_900_000,
        )
        cases.append(Case(game, [post], CommentSignals(sampled=comments, distinct_commenters=comments)))
    posted, meta = PostedState(), Meta(last_roundup_at=NOW - timedelta(hours=2))
    plan = decide(cases, posted=posted, meta=meta)
    assert all(c.results[-1].score < DECISIONS.roundup_score for c in cases)
    assert plan.roundup_due
    assert plan.roundup == [] and plan.alarms == []
    commit_plan(plan, posted, meta, now=NOW)
    assert meta.last_roundup_at == NOW  # cadence holds: next check in 2 hours
    assert posted.games == {}
