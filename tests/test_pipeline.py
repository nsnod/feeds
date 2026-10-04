"""End-to-end tests of one run (and several consecutive runs) of the pipeline.

Collectors are small stubs, Discord is the in-memory FakeDiscord, and the clock is fixed,
so these exercise the real resolver, scoring, decisions, publishing and feedback code.
"""

from __future__ import annotations

import time
from datetime import timedelta

import httpx
import pytest

from gembot.collectors.base import CollectContext, Collector
from gembot.collectors.rss import RssCollector
from gembot.config import load_config
from gembot.discord.fake import FakeDiscord
from gembot.discord.setup import run_setup
from gembot.http import HttpError
from gembot.models import Comment, GamePostState, LLMVerdict, Mention, State
from gembot.pipeline import Pipeline, _append_snapshot, _merge_mention, decision_counts
from tests.factories import (
    INCIDENT_FEEDS,
    NOW,
    config_dir_with_feeds,
    fixture_path,
    make_config,
    make_http,
    make_mention,
)

# ----------------------------------------------------------------------------- helpers


class StubCollector(Collector):
    """Returns the mentions/comments it is given; can be told to fail."""

    name = "reddit"

    def __init__(self, ctx, mentions=(), comments=None, *, name=None, fail=None, audience=None):
        if name:
            self.name = name
        super().__init__(ctx)
        self.mentions = list(mentions)
        self.comments = comments or {}
        self.fail = fail
        self.audience = audience

    def collect(self):
        if self.fail:
            with self.guard("listing"):
                raise HttpError(self.fail, 503)
            return []
        return [m.model_copy(deep=True) for m in self.mentions]

    def fetch_comments(self, mention, limit):
        return list(self.comments.get(mention.key, []))[:limit]

    def fetch_audience(self, mention):
        return self.audience


def wishlist_comments(n_intent=8, n_roblox=0, n_plain=10, n_negative=0):
    texts = (
        ["Wishlisted! me and the boys need this"] * n_intent
        + ["this looks like a roblox game lol"] * n_roblox
        + ["asset flip scam, stolen assets"] * n_negative
        + ["cool"] * n_plain
    )
    return [Comment(id=str(i), author=f"user{i}", text=t) for i, t in enumerate(texts)]


def hot_post(source_id="p1", **kw):
    defaults = dict(
        title="My co-op horror game Gorilla Pizza Panic has proximity chat - wishlist it!",
        text="Up to 4 players, ragdoll physics. https://store.steampowered.com/app/3141590/Gorilla_Pizza_Panic/",
        author="gorilla_dev",
        audience=5000,
        hours_ago=3,
        likes=310,
        comments=60,
        channel="r/IndieDev",
    )
    defaults.update(kw)
    return make_mention("reddit", source_id, **defaults)


def bsky_post(**kw):
    defaults = dict(
        title="Gorilla Pizza Panic announce trailer!",
        text="co-op chaos with friends",
        author="gorilladev.bsky.social",
        hours_ago=2,
        likes=80,
        comments=10,
        shares=12,
        channel="bluesky",
    )
    defaults.update(kw)
    return make_mention("bluesky", "did:plc:x/3k", **defaults)


class World:
    """A state + fake Discord that survive across several pipeline runs."""

    def __init__(self, config=None):
        self.config = config or make_config()
        self.state = State()
        self.discord = FakeDiscord(guilds=["1"])
        run_setup(self.discord, self.config, self.state, now=NOW - timedelta(hours=1))
        self.setup_messages = len(self.discord.sent)

    def run(self, now, collectors_factory, *, env=None, discord=True, post=True, llm=None):
        http = make_http()
        ctx = CollectContext(config=self.config, http=http, now=now, state=self.state)
        collectors = collectors_factory(ctx)
        pipeline = Pipeline(
            self.config,
            self.state,
            http=http,
            now=now,
            discord=self.discord if discord else None,
            collectors=collectors,
            sleep=lambda _s: None,
            env=env or {},
            post=post,
            llm=llm,
        )
        return pipeline.run()

    def sent_since_setup(self):
        return self.discord.sent[self.setup_messages :]

    def channel(self, role):
        return self.state.meta.discord.channels[role]


def hot_collectors(ctx, *, comments=None, extra=()):
    post = hot_post()
    comments = comments if comments is not None else wishlist_comments(n_intent=8, n_roblox=6)
    return {
        "reddit": StubCollector(ctx, [post, *extra], {post.key: comments}),
        "bluesky": StubCollector(ctx, [bsky_post()], name="bluesky"),
    }


# ----------------------------------------------------------------------------- tests


def test_hot_game_alarms_once_with_reactions_and_state_updates():
    world = World()
    result = world.run(NOW, hot_collectors)

    assert result.collected == 2 and result.new_mentions == 2
    assert list(result.resolve.assignments.values()) == ["steam:3141590", "steam:3141590"]
    game = world.state.games["steam:3141590"]
    assert game.title == "Gorilla Pizza Panic"
    assert game.last_score and game.last_score >= 72 and game.last_scored_at == NOW
    assert result.plan and [d.game_id for d in result.plan.alarms] == ["steam:3141590"]
    assert decision_counts(result) == {"alarm": 1, "roundup": 0}

    sent = world.sent_since_setup()
    assert len(sent) == 1
    channel_id, payload = sent[0]
    assert channel_id == world.channel("alarm")
    embed = payload["embeds"][0]
    assert embed["title"] == "🚨 GEM ALARM: Gorilla Pizza Panic"
    assert "🧱 Roblox-clone discourse" in embed["description"]
    message = result.posted[0]
    assert message.kind == "alarm" and message.entries[0].game_id == "steam:3141590"
    reactions = world.discord.messages[message.message_id]["reactions"]
    assert set(reactions) == {"👍", "👎"}
    posted_state = world.state.posted.games["steam:3141590"]
    assert posted_state.alarmed_at == NOW
    assert world.state.meta.alarms_on(NOW.date()) == 1
    assert world.state.meta.run_count == 1 and world.state.meta.last_run_at == NOW
    # baselines learn from posts older than an hour; comment bodies are sampled, signals kept
    assert "r/IndieDev" in world.state.baselines
    assert world.state.mentions[hot_post().key].signals.roblox_commenters == 6


def test_second_run_never_double_alarms():
    world = World()
    world.run(NOW, hot_collectors)
    result = world.run(NOW + timedelta(minutes=30), hot_collectors)
    assert result.plan is not None and result.plan.alarms == []
    assert all(cid != world.channel("alarm") for cid, _ in world.sent_since_setup()[1:])


def test_medium_game_goes_to_roundup_then_waits_for_the_interval():
    world = World()

    def collectors(ctx):
        post = make_mention(
            "reddit",
            "m1",
            title="Our physics party game Sock Puppet Rampage is a chaotic co-op game for 4 players",
            author="sockdev",
            audience=40000,
            hours_ago=5,
            likes=40,
            comments=12,
            channel="r/indiegames",
        )
        return {
            "reddit": StubCollector(ctx, [post], {post.key: wishlist_comments(n_intent=3, n_plain=9)}),
            "bluesky": StubCollector(
                ctx,
                [
                    make_mention(
                        "bluesky",
                        "did:plc:s/1",
                        title="Sock Puppet Rampage demo is out",
                        author="sockdev.bsky.social",
                        hours_ago=4,
                        likes=12,
                        comments=2,
                        channel="bluesky",
                    )
                ],
                name="bluesky",
            ),
        }

    result = world.run(NOW, collectors)
    assert result.plan and result.plan.alarms == []
    roundup_ids = [d.game_id for d in result.plan.roundup]
    assert len(roundup_ids) == 1
    sent = world.sent_since_setup()
    assert [cid for cid, _ in sent] == [world.channel("roundup")] * 2  # header + one entry
    assert "Gem Roundup" in sent[0][1]["content"]
    assert world.state.meta.last_roundup_at == NOW
    # 30 minutes later: same game, roundup not due again -> nothing new posted
    result2 = world.run(NOW + timedelta(minutes=30), collectors)
    assert result2.plan.roundup == [] and not result2.plan.roundup_due
    assert len(world.sent_since_setup()) == 2


def test_nothing_good_enough_posts_nothing_but_advances_roundup_clock():
    world = World()

    def collectors(ctx):
        dull = make_mention(
            "reddit",
            "d1",
            title="Made a small puzzle game called Quiet Tiles",
            author="tiles",
            audience=1_900_000,
            hours_ago=20,
            likes=2,
            comments=0,
            channel="r/gamedev",
        )
        return {"reddit": StubCollector(ctx, [dull])}

    result = world.run(NOW, collectors)
    assert result.plan and result.plan.roundup == [] and result.plan.roundup_due
    assert world.sent_since_setup() == []
    assert world.state.meta.last_roundup_at == NOW


def test_unresolvable_mentions_are_dropped_and_never_posted():
    world = World()

    def collectors(ctx):
        question = make_mention("reddit", "q1", title="what engine should i use?", text="help", hours_ago=2)
        return {"reddit": StubCollector(ctx, [question])}

    result = world.run(NOW, collectors)
    assert result.resolve.dropped == ["reddit:q1"]
    assert "reddit:q1" not in world.state.mentions
    assert "reddit:q1" in world.state.seen
    assert world.state.games == {}


def test_blocklisted_publisher_is_excluded():
    world = World()

    def collectors(ctx):
        post = hot_post()
        steam = make_mention(
            "steam",
            "3141590",
            title="Gorilla Pizza Panic",
            author="Ubisoft Montreal",
            extra={"steam": {"appid": 3141590, "name": "Gorilla Pizza Panic", "publishers": ["Ubisoft"]}},
        )
        return {"reddit": StubCollector(ctx, [post, steam], {post.key: wishlist_comments(n_intent=10)})}

    result = world.run(NOW, collectors)
    assert "steam:3141590" not in result.shortlist  # never enriched or scored
    assert result.plan.alarms == [] and result.plan.roundup == []
    assert "Ubisoft" in (world.state.games["steam:3141590"].excluded_reason or "")


def test_source_breaks_after_six_failures_then_recovers_once():
    world = World()
    failing = lambda ctx: {"itch": StubCollector(ctx, name="itch", fail="HTTP 503")}  # noqa: E731
    now = NOW
    for _ in range(5):
        result = world.run(now, failing)
        assert result.status_lines == []
        now += timedelta(minutes=30)
    result = world.run(now, failing)
    assert len(result.status_lines) == 1 and "itch" in result.status_lines[0]
    status = [p for cid, p in world.sent_since_setup() if cid == world.channel("status")]
    assert len(status) == 1 and "failed 6 runs" in status[0]["embeds"][0]["description"]
    # still failing: no new message (no noise)
    result = world.run(now + timedelta(minutes=30), failing)
    assert result.status_lines == []
    # recovery is announced once
    ok = lambda ctx: {"itch": StubCollector(ctx, name="itch")}  # noqa: E731
    result = world.run(now + timedelta(minutes=60), ok)
    assert len(result.status_lines) == 1 and "working again" in result.status_lines[0]
    assert world.state.meta.source_health["itch"].consecutive_failures == 0


def test_mistakes_in_feeds_yaml_never_stop_a_run_and_reach_the_status_channel(tmp_path):
    """The 2026-10-04 feeds.yaml: every other source keeps working, the two valid feeds are read,
    and after six runs #gembot-status quotes the mistake (it never fixes itself)."""
    config = load_config(config_dir_with_feeds(tmp_path, INCIDENT_FEEDS), env={})
    world = World(config)
    instagram = fixture_path("rss", "rssapp_instagram.xml").read_bytes()
    requested: list[str] = []

    def feeds_online(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=instagram, headers={"Content-Type": "application/rss+xml"})

    def collectors(ctx):
        rss_ctx = CollectContext(
            config=ctx.config, http=make_http(httpx.MockTransport(feeds_online)), now=ctx.now, state=ctx.state
        )
        return {**hot_collectors(ctx), "rss": RssCollector(rss_ctx)}

    now = NOW
    for run in range(6):
        result = world.run(now, collectors)
        assert result.reports["reddit"].mentions == 1 and result.reports["bluesky"].mentions == 1
        rss = result.reports["rss"]
        assert rss.mentions == 3 and rss.ok_units == 2 and not rss.ok
        assert rss.config_errors == 3  # the stray key, the second "feeds:" and KreekCraft's channel_id
        if run == 0:
            assert [d.game_id for d in result.plan.alarms] == ["steam:3141590"]  # the scan went on
        now += timedelta(minutes=30)
    # KreekCraft's channel id can never work: it is not requested at all
    assert sorted(set(requested)) == [
        "https://rss.app/feeds/5KcRbde1HFqAzPdx.xml",
        "https://rss.app/feeds/K1vwmXudAkt1exqO.xml",
    ]
    assert len(result.status_lines) == 1
    assert "**rss** has failed 6 runs in a row" in result.status_lines[0]
    assert "config/feeds.yaml: line 38: 'feeds' appears again (first on line 22)" in result.status_lines[0]
    status = [p for cid, p in world.sent_since_setup() if cid == world.channel("status")]
    assert len(status) == 1 and "feeds.yaml" in status[0]["embeds"][0]["description"]


def test_skipped_sources_do_not_count_as_failures():
    world = World()

    class Off(StubCollector):
        def enabled(self):
            return False, "no key"

    for i in range(7):
        world.run(NOW + timedelta(minutes=30 * i), lambda ctx: {"x": Off(ctx, name="x")})
    assert "x" not in world.state.meta.source_health


def test_thumbs_up_from_humans_nudges_weights_and_records_labels():
    world = World()
    result = world.run(NOW, hot_collectors)
    alarm = result.posted[0]
    for user in ("u1", "u2"):
        world.discord.react(alarm.channel_id, alarm.message_id, "👍", user)
    world.discord.react(alarm.channel_id, alarm.message_id, "👍", "otherbot", bot=True)
    before = dict(world.config.settings.weights)
    later = NOW + timedelta(minutes=40)
    result2 = world.run(later, hot_collectors)
    assert len(result2.labels) == 1 and result2.labels[0].label == 1.0 and result2.labels[0].up == 2
    after = world.state.weights.current
    assert after and abs(sum(after.values()) - 1) < 1e-9
    # the rule rewards features that were above this game's average (meme/fit/velocity were 1.0)
    assert after["meme"] > before["meme"] and after["fit"] > before["fit"]
    # reading the same reactions again does not apply the label twice
    weights_once = dict(after)
    world.run(later + timedelta(minutes=40), hot_collectors)
    assert world.state.weights.current == weights_once
    assert len(world.state.labels) == 1


def test_keepalive_reminder_after_45_idle_days_once_a_week():
    world = World()
    old = int((NOW - timedelta(days=50)).timestamp())
    env = {"GEMBOT_DEFAULT_BRANCH_COMMIT_TS": str(old)}
    quiet = lambda ctx: {}  # noqa: E731
    result = world.run(NOW, quiet, env=env)
    assert len(result.status_lines) == 1 and "50 days" in result.status_lines[0]
    assert world.state.meta.keepalive_reminded_at == NOW
    assert world.run(NOW + timedelta(days=2), quiet, env=env).status_lines == []
    assert len(world.run(NOW + timedelta(days=8), quiet, env=env).status_lines) == 1
    recent = {"GEMBOT_DEFAULT_BRANCH_COMMIT_TS": str(int(NOW.timestamp()))}
    assert world.run(NOW + timedelta(days=20), quiet, env=recent).status_lines == []
    assert world.run(NOW, quiet, env={"GEMBOT_DEFAULT_BRANCH_COMMIT_TS": "garbage"}).status_lines == []


def test_without_discord_nothing_is_posted_and_plan_is_not_committed():
    world = World()
    result = world.run(NOW, hot_collectors, discord=False)
    assert result.plan and len(result.plan.alarms) == 1
    assert any("not set up" in w for w in result.warnings)
    assert world.sent_since_setup() == []
    assert "steam:3141590" not in world.state.posted.games  # will alarm once Discord works
    # dry runs (post=False) don't warn
    world2 = World()
    assert world2.run(NOW, hot_collectors, post=False).warnings == []


def test_missing_channels_means_setup_needed():
    world = World()
    world.state.meta.discord.channels = {}
    result = world.run(NOW, hot_collectors)
    assert any("Setup" in w for w in result.warnings)


def test_failed_alarm_post_is_retried_next_run():
    world = World()
    world.discord.fail("send_message", HttpError("HTTP 500", 500), times=1)
    result = world.run(NOW, hot_collectors)
    assert result.plan and len(result.plan.alarms) == 1
    assert any("not posted" in w for w in result.warnings)
    assert world.state.posted.games.get("steam:3141590") is None
    result2 = world.run(NOW + timedelta(minutes=30), hot_collectors)
    assert [d.game_id for d in result2.plan.alarms] == ["steam:3141590"]
    assert world.state.posted.games["steam:3141590"].alarmed_at is not None


def test_failed_roundup_keeps_it_due():
    world = World()

    def collectors(ctx):
        post = make_mention(
            "reddit",
            "m1",
            title="Our physics party game Sock Puppet Rampage is a chaotic co-op game for 4 players",
            author="sockdev",
            audience=40000,
            hours_ago=5,
            likes=40,
            comments=12,
            channel="r/indiegames",
        )
        return {
            "reddit": StubCollector(ctx, [post], {post.key: wishlist_comments(n_intent=3, n_plain=9)}),
            "bluesky": StubCollector(
                ctx,
                [
                    make_mention(
                        "bluesky", "b", title="Sock Puppet Rampage demo is out", hours_ago=4, likes=12
                    )
                ],
                name="bluesky",
            ),
        }

    world.discord.fail("send_message", HttpError("HTTP 500", 500), times=1)
    result = world.run(NOW, collectors)
    assert result.plan.roundup and any("roundup not posted" in w for w in result.warnings)
    assert world.state.meta.last_roundup_at is None


def test_ingest_merges_numbers_and_keeps_identity():
    world = World()
    world.run(NOW, hot_collectors)
    key = hot_post().key
    stored = world.state.mentions[key]
    assert stored.game_id == "steam:3141590" and stored.first_seen == NOW
    bigger = lambda ctx: hot_collectors(ctx, extra=())  # noqa: E731
    world.run(NOW + timedelta(minutes=30), bigger)
    again = world.state.mentions[key]
    assert again.first_seen == NOW and again.game_id == "steam:3141590"
    assert len(again.history) == 1  # unchanged numbers are not re-recorded


def test_merge_mention_and_snapshot_helpers():
    old = make_mention("reddit", "a", likes=1, audience=500)
    old.first_seen = NOW - timedelta(hours=5)
    old.game_id = "g1"
    old.extra = {"keep": 1}
    old.media_thumb = "thumb"
    fresh = make_mention("reddit", "a", likes=9, audience=None, created_at=NOW - timedelta(hours=1))
    fresh.extra = {"new": 2, "none": None}
    merged = _merge_mention(old, fresh)
    assert merged.game_id == "g1" and merged.first_seen == old.first_seen
    assert merged.author_audience == 500 and merged.media_thumb == "thumb"
    assert merged.extra == {"keep": 1, "new": 2}
    assert merged.created_at == min(old.created_at, fresh.created_at)
    m = make_mention("steam", "1", extra={"steam": {"followers": 10}})
    _append_snapshot(m, NOW)
    _append_snapshot(m, NOW + timedelta(minutes=30))
    assert len(m.history) == 1 and m.history[0].followers == 10
    m.rank = 3
    _append_snapshot(m, NOW + timedelta(hours=1))
    assert [s.rank for s in m.history] == [None, 3]
    many = make_mention("reddit", "z")
    for i in range(60):
        many.engagement.likes = i
        _append_snapshot(many, NOW + timedelta(minutes=i))
    assert len(many.history) == 48


def test_game_merge_moves_posted_state_and_aliases():
    world = World()
    pipeline = Pipeline(
        world.config, world.state, http=make_http(), now=NOW, discord=None, collectors={}, env={}
    )
    world.state.posted.games["t:old-000000"] = GamePostState(
        alarmed_at=NOW - timedelta(days=1), alarm_score=80
    )
    world.state.posted.games["steam:1"] = GamePostState(roundup_at=NOW - timedelta(days=2), roundup_score=50)
    world.state.meta.game_aliases["t:older-111111"] = "t:old-000000"
    from gembot.models import PostedEntry, PostedMessage

    world.state.posted.messages["m"] = PostedMessage(
        message_id="m",
        channel_id="c",
        kind="alarm",
        posted_at=NOW,
        entries=[PostedEntry(game_id="t:old-000000", score=80)],
    )
    pipeline._apply_merge("t:old-000000", "steam:1")
    merged = world.state.posted.games["steam:1"]
    assert merged.alarmed_at == NOW - timedelta(days=1) and merged.alarm_score == 80
    assert merged.roundup_at == NOW - timedelta(days=2)
    assert "t:old-000000" not in world.state.posted.games
    assert world.state.meta.game_aliases == {"t:old-000000": "steam:1", "t:older-111111": "steam:1"}
    assert world.state.posted.messages["m"].entries[0].game_id == "steam:1"


def test_mentions_pointing_at_aliased_games_follow_the_alias():
    world = World()
    world.run(NOW, hot_collectors)
    key = hot_post().key
    world.state.mentions[key].game_id = "t:gone-000000"
    world.state.meta.game_aliases["t:gone-000000"] = "steam:3141590"
    world.state.mentions[key].engagement.likes += 100  # changed -> touched
    pipeline = Pipeline(
        world.config, world.state, http=make_http(), now=NOW, discord=None, collectors={}, env={}
    )
    games = pipeline.resolve([key])
    assert games == {"steam:3141590"}
    assert world.state.mentions[key].game_id == "steam:3141590"


def test_llm_verdict_is_used_once_and_can_exclude_non_games():
    class FakeLLM:
        def __init__(self, verdict):
            self.verdict = verdict
            self.calls = 0
            self.closed = False

        def classify(self, mention: Mention):
            self.calls += 1
            return self.verdict

        def close(self):
            self.closed = True

    world = World()
    llm = FakeLLM(
        LLMVerdict(
            game_title="Gorilla Pizza Panic",
            is_a_specific_game=True,
            friendslop_fit=0.9,
            one_line_pitch="Pizza delivery, but your friends are gorillas.",
        )
    )
    world.run(NOW, hot_collectors, llm=llm)
    game = world.state.games["steam:3141590"]
    assert game.llm and game.pitch == "Pizza delivery, but your friends are gorillas."
    assert llm.calls == 1 and llm.closed
    world.run(NOW + timedelta(minutes=30), hot_collectors, llm=llm)
    assert llm.calls == 1  # never re-asked for the same game

    world2 = World()

    def title_only(ctx):
        post = make_mention(
            "reddit",
            "t1",
            title='Check out "Moon Soup Simulator" my new game',
            hours_ago=2,
            likes=50,
            comments=3,
            channel="r/IndieGaming",
        )
        return {"reddit": StubCollector(ctx, [post])}

    nope = FakeLLM(LLMVerdict(game_title=None, is_a_specific_game=False, friendslop_fit=0.0))
    result = world2.run(NOW, title_only, llm=nope)
    excluded = [r for r in result.results.values() if r.excluded]
    assert excluded and "LLM" in (excluded[0].exclude_reason or "")


def test_audience_is_filled_by_collectors_that_know_it():
    world = World()

    def collectors(ctx):
        post = bsky_post(audience=None)
        return {"bluesky": StubCollector(ctx, [post], name="bluesky", audience=1234)}

    world.run(NOW, collectors)
    assert world.state.mentions[bsky_post().key].author_audience == 1234


@pytest.mark.parametrize("count", [0, 1])
def test_decision_counts_without_plan(count):
    from gembot.pipeline import RunResult

    assert decision_counts(RunResult(now=NOW)) == {"alarm": 0, "roundup": 0}


def test_llm_title_replaces_a_heuristic_title_for_games_without_a_store_page():
    class TitleLLM:
        def classify(self, mention):
            return LLMVerdict(game_title="Moon Soup Simulator", is_a_specific_game=True, friendslop_fit=0.9)

        def close(self):
            pass

    world = World()
    post = make_mention(
        "reddit",
        "ms1",
        title='Finally showing "Moon Soup Simulator Teaser" - a co-op cooking game for 4 friends',
        hours_ago=2,
        likes=30,
        comments=4,
        channel="r/IndieGaming",
    )
    result = world.run(NOW, lambda ctx: {"reddit": StubCollector(ctx, [post])}, llm=TitleLLM())
    game = world.state.games[world.state.mentions["reddit:ms1"].game_id]
    assert game.title == "Moon Soup Simulator"
    assert game.game_id in result.results


def test_sources_stop_at_the_collect_deadline_and_enrichment_gets_its_own():
    class Slow(StubCollector):
        def collect(self):
            self.budget.take()  # one "request"
            return super().collect()

        def fetch_comments(self, mention, limit):
            self.budget.take()
            return super().fetch_comments(mention, limit)

    settings = make_config().settings
    run = settings.run.model_copy(update={"collect_seconds": 0.0, "network_seconds": 3600.0})
    world = World(make_config(settings=settings.model_copy(update={"run": run})))
    post = make_mention("reddit", "d1", title="Moon Soup Simulator co-op", hours_ago=2, comments=5)
    seen = {}

    def factory(ctx):
        seen["c"] = Slow(ctx, [post], {post.key: wishlist_comments(n_intent=1, n_plain=1)})
        return {"reddit": seen["c"]}

    result = world.run(NOW, factory)
    report = result.reports["reddit"]
    assert report.mentions == 0 and any("out of time" in w for w in report.warnings)
    # after collecting, the collector's budget runs on the (later) enrichment deadline
    assert seen["c"].budget.deadline - time.monotonic() > 3000


def test_still_there_timestamps_move_only_when_due_so_state_diffs_stay_small():
    from gembot.pipeline import LAST_SEEN_REFRESH, OBSERVED_REFRESH
    from gembot.scoring.features import ITCH_STALE_HOURS

    # a listing seen every run must never look like it fell off the list
    assert OBSERVED_REFRESH + timedelta(minutes=30) < timedelta(hours=ITCH_STALE_HOURS)
    world = World()
    post = make_mention("reddit", "ts1", title="Moon Soup Simulator co-op", hours_ago=2, likes=5)
    factory = lambda ctx: {"reddit": StubCollector(ctx, [post])}  # noqa: E731
    world.run(NOW, factory)
    mention = world.state.mentions["reddit:ts1"]
    game = world.state.games[mention.game_id]
    assert mention.observed_at == NOW and game.last_seen == NOW

    world.run(NOW + timedelta(minutes=30), factory)  # unchanged, re-observed soon after
    assert world.state.mentions["reddit:ts1"].observed_at == NOW and game.last_seen == NOW

    later = NOW + OBSERVED_REFRESH
    world.run(later, factory)
    assert world.state.mentions["reddit:ts1"].observed_at == later
    assert game.last_seen == NOW

    latest = NOW + LAST_SEEN_REFRESH
    world.run(latest, factory)
    assert world.state.games[mention.game_id].last_seen == latest
