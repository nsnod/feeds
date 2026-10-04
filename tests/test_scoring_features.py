"""Feature extraction: every formula from BUILD_SPEC 4.2 with the default settings."""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

import pytest

from gembot.config import Sources
from gembot.models import BaselineSample, CommentSignals, LLMVerdict, Snapshot, SteamInfo
from gembot.scoring.features import (
    DEFAULT_FALLBACK_EPH,
    MIN_BASELINE_EPH,
    ScoringContext,
    baseline_eph,
    combine_strengths,
    compute_features,
    hype_from_signals,
    match_fit_keywords,
    ordered_platforms,
    steam_info_for,
)
from tests.factories import NOW, make_config, make_game, make_mention

CONFIG = make_config()


def ctx_with(baselines=None, now=NOW, config=CONFIG) -> ScoringContext:
    return ScoringContext.from_config(config, now=now, baselines=baselines)


def samples(
    channel: str, values: list[float], *, hours_ago: float = 1.0
) -> dict[str, dict[str, BaselineSample]]:
    return {
        channel: {
            f"m{i}": BaselineSample(at=NOW - timedelta(hours=hours_ago + i), eph=v)
            for i, v in enumerate(values)
        }
    }


def features_for(mentions, *, game=None, signals=None, ctx=None):
    game = game or make_game()
    return compute_features(game, mentions, ctx or ctx_with(), signals)


# ----------------------------------------------------------------------------- baselines


def test_baseline_is_median_of_recent_samples():
    ctx = ctx_with(samples("r/IndieDev", [1, 2, 3, 4, 5, 6, 7, 8, 100]))
    assert baseline_eph("r/IndieDev", "reddit", ctx) == (5.0, 9)


def test_baseline_falls_back_to_default_with_too_few_samples():
    ctx = ctx_with(samples("r/godot", [50.0] * 7))
    assert baseline_eph("r/godot", "reddit", ctx) == (3.0, 7)
    assert baseline_eph("bluesky", "bluesky", ctx) == (1.0, 0)
    assert baseline_eph("mastodon", "mastodon", ctx) == (DEFAULT_FALLBACK_EPH, 0)


def test_baseline_ignores_samples_outside_the_window():
    old = samples("r/x", [100.0] * 10, hours_ago=15 * 24)
    ctx = ctx_with(old)
    assert baseline_eph("r/x", "reddit", ctx) == (3.0, 0)


def test_baseline_never_below_floor():
    ctx = ctx_with(samples("r/quiet", [0.0] * 10))
    assert baseline_eph("r/quiet", "reddit", ctx) == (MIN_BASELINE_EPH, 10)


# ----------------------------------------------------------------------------- velocity


@pytest.mark.parametrize(
    ("multiple", "expected"),
    [(16, 1.0), (32, 1.0), (2, 0.25), (4, 0.5), (1, 0.0), (0.5, 0.0), (10, math.log2(10) / 4)],
)
def test_velocity_is_log2_of_multiple_over_4(multiple, expected):
    ctx = ctx_with(samples("r/IndieDev", [5.0] * 10))
    # 2h old -> eph = total / 2; baseline 5 eph
    m = make_mention(channel="r/IndieDev", hours_ago=2, likes=int(multiple * 10))
    features, evidence = features_for([m], ctx=ctx)
    assert features.velocity == pytest.approx(expected)
    assert evidence.velocity_multiple == pytest.approx(multiple)
    assert evidence.baseline_eph == 5.0


def test_velocity_uses_one_hour_floor_and_max_over_recent_mentions():
    ctx = ctx_with(samples("r/a", [2.0] * 10) | samples("r/b", [2.0] * 10))
    brand_new = make_mention(
        "reddit", "new", channel="r/a", hours_ago=0.05, likes=4
    )  # 4 / 1h floor = 4 eph -> 2x
    hot = make_mention("reddit", "hot", channel="r/b", hours_ago=1, likes=32)  # 32 eph -> 16x
    stale = make_mention("reddit", "old", channel="r/b", hours_ago=100, likes=100_000)  # outside 72h window
    features, evidence = features_for([brand_new, hot, stale], ctx=ctx)
    assert features.velocity == 1.0
    assert evidence.best_mention_key == "reddit:hot"
    assert evidence.best_channel == "r/b"
    assert evidence.best_likes == 32
    assert evidence.eph == pytest.approx(32)
    features, evidence = features_for([brand_new], ctx=ctx)
    assert features.velocity == pytest.approx(0.25)
    assert evidence.eph == pytest.approx(4)


def test_stale_mention_gives_no_velocity_but_still_evidence():
    m = make_mention(channel="r/x", hours_ago=200, likes=5000)
    features, evidence = features_for([m])
    assert features.velocity == 0.0
    assert evidence.best_mention_key == m.key


def test_channel_defaults_to_source_for_baseline():
    ctx = ctx_with(samples("bluesky", [0.5] * 10))
    m = make_mention("bluesky", "p", channel=None, hours_ago=1, likes=8)  # 8 eph vs 0.5 -> 16x
    features, _ = features_for([m], ctx=ctx)
    assert features.velocity == 1.0


def itch_mention(rank, *, list_size=30, history_hours=None, channel="itch:new-and-popular", **kw):
    history = []
    if history_hours is not None:
        history = [
            Snapshot(at=NOW - timedelta(hours=history_hours), rank=rank + 5),
            Snapshot(at=NOW, rank=rank),
        ]
    kw.setdefault("hours_ago", 30)
    return make_mention(
        "itch", f"dev/game-{rank}", channel=channel, rank=rank, list_size=list_size, history=history, **kw
    )


def test_itch_velocity_from_rank_and_time_on_list():
    features, evidence = features_for([itch_mention(1, history_hours=12)])
    assert features.velocity == pytest.approx(0.6 * 1.0 + 0.4 * 0.5)
    assert evidence.best_rank == 1
    assert evidence.rank_channel == "itch:new-and-popular"
    assert evidence.hours_on_list == pytest.approx(12)
    assert evidence.best_source == "itch"
    # rank 16 of 30 for a full day
    features, _ = features_for([itch_mention(16, history_hours=30)])
    assert features.velocity == pytest.approx(0.6 * (1 - 15 / 30) + 0.4)


def test_itch_velocity_falls_back_to_first_seen_and_default_list_size():
    m = itch_mention(4, list_size=None, first_seen=NOW - timedelta(hours=6))
    features, evidence = features_for([m])
    assert features.velocity == pytest.approx(0.6 * (1 - 3 / 30) + 0.4 * 0.25)
    assert evidence.hours_on_list == pytest.approx(6)
    no_time = itch_mention(1)
    assert features_for([no_time])[0].velocity == pytest.approx(0.6)


def test_itch_rank_from_history_when_missing_on_mention():
    m = itch_mention(2, history_hours=24)
    m.rank = None
    features, evidence = features_for([m])
    assert evidence.best_rank == 2
    assert features.velocity == pytest.approx(0.6 * (1 - 1 / 30) + 0.4)


def test_itch_other_feeds_and_stale_listings_have_no_velocity():
    tag_feed = itch_mention(1, history_hours=24, channel="itch:tag-co-op")
    assert features_for([tag_feed])[0].velocity == 0.0
    fell_off = itch_mention(1, history_hours=24, observed_at=NOW - timedelta(hours=10))
    features, evidence = features_for([fell_off])
    assert features.velocity == 0.0 and evidence.best_rank is None


def steam_listing(followers: list[tuple[float, int]], **kw):
    history = [Snapshot(at=NOW - timedelta(hours=h), followers=f) for h, f in followers]
    return make_mention("steam", "123", channel="steam:comingsoon", history=history, **kw)


def test_steam_velocity_from_follower_growth():
    features, evidence = features_for([steam_listing([(24, 200), (0, 300)])])  # +50% in a day -> 1.0
    assert features.velocity == pytest.approx(1.0)
    assert evidence.follower_growth == 100
    assert evidence.best_source == "steam"
    features, _ = features_for([steam_listing([(48, 400), (0, 480)])])  # +10%/day -> 0.2
    assert features.velocity == pytest.approx(0.2)
    # tiny pages are measured against at least 100 followers; short gaps count as 12h
    features, _ = features_for([steam_listing([(1, 2), (0, 8)])])
    assert features.velocity == pytest.approx((6 / 100 / 0.5) / 0.5)


def test_steam_without_follower_history_has_no_velocity():
    assert features_for([steam_listing([(0, 300)])])[0].velocity == 0.0
    assert features_for([steam_listing([])])[0].velocity == 0.0
    shrinking = features_for([steam_listing([(24, 300), (0, 200)])])[0]
    assert shrinking.velocity == 0.0


# ----------------------------------------------------------------------------- underdog


def test_underdog_formula_uses_most_engaged_mention_with_audience():
    small = make_mention("bluesky", "a", audience=2100, likes=480)
    bigger_no_audience = make_mention("reddit", "b", audience=None, likes=5000)
    weak = make_mention("bluesky", "c", audience=50, likes=1)
    features, _ = features_for([small, bigger_no_audience, weak])
    assert features.underdog == pytest.approx(math.log10(1 + 1000 * 480 / 2100) / 3)


def test_underdog_min_audience_and_no_audience():
    tiny = make_mention("bluesky", "a", audience=3, likes=10)
    assert features_for([tiny])[0].underdog == pytest.approx(math.log10(1 + 1000 * 10 / 100) / 3)
    assert features_for([make_mention(likes=999)])[0].underdog == 0.0
    huge = make_mention("bluesky", "z", audience=50, likes=10_000_000)
    assert features_for([huge])[0].underdog == 1.0


# ----------------------------------------------------------------------------- cross


@pytest.mark.parametrize(
    ("platforms", "expected"),
    [
        (["reddit"], 0.0),
        (["reddit", "reddit"], 0.0),
        (["reddit", "bluesky"], 0.6),
        (["reddit", "bluesky", "steam"], 0.85),
        (["reddit", "bluesky", "steam", "itch"], 1.0),
        (["reddit", "bluesky", "steam", "itch", "youtube"], 1.0),
    ],
)
def test_cross_platform_map(platforms, expected):
    mentions = [make_mention(p, f"id{i}", hours_ago=5) for i, p in enumerate(platforms)]
    features, evidence = features_for(mentions)
    assert features.cross == pytest.approx(expected)
    assert set(evidence.sources_72h) == set(platforms)


def test_cross_window_uses_created_or_first_seen():
    old_but_new_to_us = make_mention("youtube", "v", hours_ago=200, first_seen=NOW - timedelta(hours=2))
    too_old = make_mention("bluesky", "b", hours_ago=100)
    recent = make_mention("reddit", "r", hours_ago=30)
    features, evidence = features_for([old_but_new_to_us, too_old, recent])
    assert evidence.sources_72h == ["reddit", "youtube"]
    assert evidence.sources_24h == ["youtube"]
    assert features.cross == 0.6


def test_cross_map_with_gaps_uses_largest_key_below():
    settings = CONFIG.settings.model_copy(deep=True)
    settings.features.cross_map = {2: 0.5, 5: 1.0}
    ctx = ScoringContext(settings=settings, sources=CONFIG.sources, now=NOW)
    mentions = [make_mention(p, p) for p in ("reddit", "bluesky", "x")]
    assert features_for(mentions, ctx=ctx)[0].cross == 0.5
    assert features_for(mentions[:1], ctx=ctx)[0].cross == 0.0
    settings.features.cross_map = {}
    assert features_for(mentions, ctx=ctx)[0].cross == 0.0


def test_platforms_are_listed_social_first_steam_last():
    assert ordered_platforms(["steam", "bluesky", "reddit", "mastodon", "itch"]) == [
        "reddit",
        "itch",
        "bluesky",
        "steam",
        "mastodon",
    ]


# ----------------------------------------------------------------------------- fit


def steam_info(**kw) -> SteamInfo:
    kw.setdefault("appid", 42)
    return SteamInfo(**kw)


@pytest.mark.parametrize(
    ("category_ids", "expected"),
    [([38], 0.8), ([9], 0.7), ([39], 0.6), ([1], 0.5), ([1, 9, 38], 0.8), ([2, 22], 0.0)],
)
def test_fit_from_steam_categories(category_ids, expected):
    game = make_game(title="Zzz", steam=steam_info(category_ids=category_ids))
    assert features_for([], game=game)[0].fit == pytest.approx(expected)


def test_fit_unknown_coop_category_from_sources_list():
    sources = CONFIG.sources.model_copy(deep=True)
    sources.steam.coop_category_ids = [9, 38, 99]
    ctx = ScoringContext(settings=CONFIG.settings, sources=sources, now=NOW)
    game = make_game(title="Zzz", steam=steam_info(category_ids=[99]))
    assert features_for([], game=game, ctx=ctx)[0].fit == pytest.approx(0.7)
    bare = ScoringContext(settings=CONFIG.settings, sources=Sources(), now=NOW)
    game = make_game(title="Zzz", steam=steam_info(category_ids=[38, 1]))
    assert features_for([], game=game, ctx=bare)[0].fit == pytest.approx(0.8)


def test_fit_keywords_combine_and_are_recorded():
    m = make_mention(title="Our ragdoll party game", text="Proximity chat chaos for 2–4 players")
    features, evidence = features_for([m])
    assert evidence.fit_hits[:2] == ["proximity chat", "2-4 players"]
    assert set(evidence.fit_hits) == {"proximity chat", "2-4 players", "ragdoll", "party game", "chaos"}
    assert features.fit == 1.0
    moderate = make_mention(title="A silly multiplayer game", text="it is funny")
    features, evidence = features_for([moderate])
    assert features.fit == pytest.approx(1 - 0.7 * 0.6 * 0.7)  # silly .3, multiplayer .4, funny .3


def test_fit_longest_phrase_wins_and_word_boundaries():
    hits = match_fit_keywords(["co-op horror with friends, also co-op"], CONFIG.sources.fit_keywords)
    assert [h for h, _ in hits] == ["co-op horror", "with friends", "co-op"]
    assert match_fit_keywords(["proximity voice chat"], CONFIG.sources.fit_keywords) == [
        ("proximity voice chat", 1.0)
    ]
    assert match_fit_keywords(["Cooper peaked at chaotically"], CONFIG.sources.fit_keywords) == []
    assert match_fit_keywords(
        ["Like Lethal Company but R.E.P.O.-style"], {"like lethal company": 1, "r.e.p.o": 0.8}
    ) == [
        ("like lethal company", 1.0),
        ("r.e.p.o", 0.8),
    ]
    assert match_fit_keywords(["anything"], {"": 1.0, "anything": 0.0}) == []


def test_fit_reads_steam_description_tags_and_raw_tags():
    game = make_game(title="Zzz", steam=steam_info(short_description="A friendslop game", tags=["Physics"]))
    assert set(features_for([], game=game)[1].fit_hits) == {"friendslop", "physics"}
    m = make_mention(raw_tags=["ragdoll"])
    assert features_for([m])[1].fit_hits == ["ragdoll"]


def test_fit_with_llm_verdict_blends():
    game = make_game(title="Zzz", llm=LLMVerdict(friendslop_fit=1.0, is_a_specific_game=True))
    features, evidence = features_for([], game=game)
    assert features.fit == pytest.approx(0.8)  # max(0.5*0 + 0.5*1, 0.8*1)
    assert evidence.llm_fit == 1.0
    game = make_game(title="Proximity chat chaos", llm=LLMVerdict(friendslop_fit=0.0))
    assert features_for([], game=game)[0].fit == pytest.approx(0.5)  # the LLM tempers keywords
    game = make_game(title="Proximity chat", llm=LLMVerdict(friendslop_fit=0.9))
    assert features_for([], game=game)[0].fit == pytest.approx(0.95)


def test_combine_strengths_caps_at_one():
    assert combine_strengths([]) == 0.0
    assert combine_strengths([0.5, 0.5]) == pytest.approx(0.75)
    assert combine_strengths([1.0, 0.2, 7.0]) == 1.0


def test_steam_info_from_mention_extra():
    payload = steam_info(appid=7, category_ids=[38]).model_dump(mode="json")
    m = make_mention("steam", "7", extra={"steam": payload})
    broken = make_mention("steam", "8", extra={"steam": {"appid": "not-a-number"}})
    game = make_game(title="Zzz")
    info = steam_info_for(game, [broken, m])
    assert info is not None and info.appid == 7
    assert features_for([broken, m], game=game)[0].fit == pytest.approx(0.8)
    assert steam_info_for(game, [broken]) is None


# ----------------------------------------------------------------------------- hype / meme


def test_hype_formula():
    s = CommentSignals(sampled=40, distinct_commenters=25, intent_commenters=10, negative_commenters=1)
    rate_part = 0.5 * min(1, 2 * 10 / 25)
    volume = math.log(11) / math.log(16)
    assert hype_from_signals(s, 15) == pytest.approx(rate_part + 0.5 * volume - 1 / 25)
    full = CommentSignals(sampled=40, distinct_commenters=30, intent_commenters=15)
    assert hype_from_signals(full, 15) == pytest.approx(1.0)
    assert hype_from_signals(None, 15) == 0.0
    assert hype_from_signals(CommentSignals(sampled=0, intent_commenters=5), 15) == 0.0
    hated = CommentSignals(sampled=20, distinct_commenters=20, intent_commenters=2, negative_commenters=15)
    assert hype_from_signals(hated, 15) == 0.0


def test_hype_feature_needs_signals():
    m = make_mention()
    assert features_for([m])[0].hype == 0.0
    s = CommentSignals(sampled=30, distinct_commenters=30, intent_commenters=15)
    features, evidence = features_for([m], signals=s)
    assert features.hype == pytest.approx(1.0)
    assert evidence.signals.intent_commenters == 15


@pytest.mark.parametrize(
    ("jokers", "post_comments", "expected"),
    [(12, 40, 1.0), (3, 40, 0.6), (1, 10, 0.2), (5, 6, 0.0), (0, 100, 0.0)],
)
def test_meme_counts_distinct_jokers_only_on_busy_posts(jokers, post_comments, expected):
    m = make_mention(comments=post_comments)
    s = CommentSignals(
        sampled=min(post_comments, 100),
        distinct_commenters=min(post_comments, 100),
        roblox_comments=jokers * 3,
        roblox_commenters=jokers,
        post_comment_count=post_comments,
    )
    features, evidence = features_for([m], signals=s)
    assert features.meme == pytest.approx(expected)
    assert evidence.total_comments == post_comments


def test_meme_total_comments_from_signals_when_mention_count_missing():
    m = make_mention(comments=0)
    s = CommentSignals(sampled=12, distinct_commenters=12, roblox_commenters=2)
    features, evidence = features_for([m], signals=s)
    assert evidence.total_comments == 12
    assert features.meme == pytest.approx(0.4)
    assert features_for([m])[0].meme == 0.0


# ----------------------------------------------------------------------------- fresh


@pytest.mark.parametrize(
    ("hours", "expected"),
    [(0, 1.0), (47, 1.0), (48, 1.0), (192, 0.5), (14 * 24, 0.0), (40 * 24, 0.0)],
)
def test_fresh_decay(hours, expected):
    game = make_game(first_seen=NOW - timedelta(hours=hours))
    assert features_for([], game=game)[0].fresh == pytest.approx(expected)


def test_fresh_coming_soon_bonus_caps_at_one():
    soon = steam_info(coming_soon=True)
    assert features_for([], game=make_game(first_seen=NOW - timedelta(days=8), steam=soon))[
        0
    ].fresh == pytest.approx(0.7)
    assert features_for([], game=make_game(first_seen=NOW - timedelta(days=30), steam=soon))[
        0
    ].fresh == pytest.approx(0.2)
    assert features_for([], game=make_game(steam=soon))[0].fresh == 1.0
    dated = steam_info(release_date=date(2026, 12, 1))
    game = make_game(first_seen=NOW - timedelta(days=30), steam=dated)
    features, evidence = features_for([], game=game)
    assert features.fresh == pytest.approx(0.2)
    released = steam_info(release_date=date(2026, 9, 1), release_date_text="1 Sep, 2026")
    game = make_game(first_seen=NOW - timedelta(days=30), steam=released)
    features, evidence = features_for([], game=game)
    assert features.fresh == 0.0
    assert evidence.release_date_text == "1 Sep, 2026"


# ----------------------------------------------------------------------------- properties


def random_case(rng: random.Random):
    sources = ["reddit", "bluesky", "steam", "itch", "x", "youtube", "rss"]
    mentions = []
    for i in range(rng.randint(0, 6)):
        source = rng.choice(sources)
        history = [
            Snapshot(
                at=NOW - timedelta(hours=rng.uniform(0, 200)),
                rank=rng.choice([None, rng.randint(1, 40)]),
                followers=rng.choice([None, rng.randint(0, 10_000)]),
            )
            for _ in range(rng.randint(0, 3))
        ]
        mentions.append(
            make_mention(
                source,
                f"m{i}",
                hours_ago=rng.uniform(-2, 400),
                likes=rng.randint(-5, 100_000),
                comments=rng.randint(0, 5000),
                shares=rng.randint(0, 3000),
                audience=rng.choice([None, 0, rng.randint(1, 1_000_000)]),
                channel=rng.choice([None, "r/IndieDev", "itch:new-and-popular", "bluesky"]),
                rank=rng.choice([None, rng.randint(0, 50)]),
                list_size=rng.choice([None, 0, 30]),
                history=history,
                title=rng.choice(
                    ["", "co-op horror", "proximity chat ragdoll friendslop", "spreadsheet sim"]
                ),
                first_seen=rng.choice([None, NOW - timedelta(hours=rng.uniform(0, 300))]),
            )
        )
    signals = rng.choice(
        [
            None,
            CommentSignals(
                sampled=rng.randint(0, 100),
                distinct_commenters=rng.randint(0, 100),
                intent_commenters=rng.randint(0, 100),
                negative_commenters=rng.randint(0, 100),
                roblox_commenters=rng.randint(0, 100),
                post_comment_count=rng.randint(0, 500),
            ),
        ]
    )
    steam = rng.choice(
        [
            None,
            steam_info(
                category_ids=rng.sample([1, 9, 38, 39, 48, 2, 22], 3),
                coming_soon=rng.random() < 0.5,
                release_date=rng.choice([None, date(2026, 1, 1), date(2027, 1, 1)]),
            ),
        ]
    )
    llm = rng.choice([None, LLMVerdict(friendslop_fit=rng.uniform(-1, 2))])
    game = make_game(first_seen=NOW - timedelta(hours=rng.uniform(-5, 1000)), steam=steam, llm=llm)
    return game, mentions, signals


def test_every_feature_stays_in_unit_interval():
    rng = random.Random(1234)
    ctx = ctx_with(samples("r/IndieDev", [rng.uniform(0, 50) for _ in range(12)]))
    for _ in range(400):
        game, mentions, signals = random_case(rng)
        features, _ = compute_features(game, mentions, ctx, signals)
        for name, value in features.as_dict().items():
            assert 0.0 <= value <= 1.0, (name, value)


def test_steam_popular_upcoming_rank_counts_as_velocity():
    from datetime import timedelta

    from gembot.models import Snapshot
    from gembot.scoring.explain import build_reasons
    from gembot.scoring.features import ScoringContext, compute_features, popular_channels
    from gembot.scoring.score import score_game
    from tests.factories import NOW, make_config, make_game, make_mention

    config = make_config()
    ctx = ScoringContext.from_config(config, now=NOW)
    channel = next(c for c in popular_channels(ctx) if c.startswith("steam:"))
    m = make_mention("steam", "123", title="Gorilla Pizza Panic", channel=channel, rank=2, list_size=50)
    m.first_seen = NOW - timedelta(hours=30)
    m.observed_at = NOW
    m.history = [Snapshot(at=NOW - timedelta(hours=30), rank=6), Snapshot(at=NOW, rank=2)]
    game = make_game("steam:123", "Gorilla Pizza Panic", steam_appid=123, mention_keys=[m.key])
    features, evidence = compute_features(game, [m], ctx)
    assert features.velocity > 0.9
    assert evidence.best_rank == 2 and evidence.rank_channel == channel
    result = score_game(game, [m], ctx, config.settings.weights, config.blocklist)
    lines = build_reasons(result, game, now=NOW)
    assert any("Steam's popular upcoming list" in line for line in lines)
    # a plain coming-soon listing (not the popular list) gives no rank velocity
    plain = make_mention("steam", "124", channel="steam:comingsoon-indie-coop", rank=1, list_size=50)
    g2 = make_game("steam:124", "Other", steam_appid=124, mention_keys=[plain.key])
    assert compute_features(g2, [plain], ctx)[0].velocity == 0.0


def test_hype_rate_needs_enough_commenters_to_count_fully():
    from gembot.models import CommentSignals
    from gembot.scoring.features import hype_from_signals

    tiny = CommentSignals(sampled=2, distinct_commenters=2, intent_commenters=1)
    solid = CommentSignals(sampled=20, distinct_commenters=20, intent_commenters=10)
    assert hype_from_signals(tiny, 15, 10) < 0.3  # 1 "wishlisted!" out of 2 replies
    assert hype_from_signals(solid, 15, 10) > 0.9
    assert hype_from_signals(tiny, 15, 1) > 0.6  # without the confidence term it would count fully
