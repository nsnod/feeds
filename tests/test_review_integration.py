"""Regression tests from the adversarial integration review: several consecutive runs with
state carried between them. Each docstring describes the bug the test once caught.
"""

from __future__ import annotations

import time
from datetime import timedelta

from gembot.discord.fake import FakeDiscord
from gembot.discord.setup import run_setup
from gembot.http import BudgetExceeded, HttpError
from gembot.models import LLMVerdict, Mention
from gembot.runner import run_scan
from gembot.state.store import StateStore, prune
from tests.factories import NOW, make_config, make_mention
from tests.test_pipeline import StubCollector, World, hot_collectors, wishlist_comments

HALF_HOUR = timedelta(minutes=30)


# ----------------------------------------------------------------------------- helpers


def run_like_scan(world: World, now, factory, **kw):
    """World.run, preceded by the prune that run_scan / replay do at the start of every run."""
    settings = world.config.settings
    prune(world.state, now, settings.state, settings.features.baseline_window_days)
    return world.run(now, factory, **kw)


def medium_collectors(ctx):
    """One game that lands in a roundup (score >= 45) but never alarms (copied from test_pipeline)."""
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
    bsky = make_mention(
        "bluesky",
        "did:plc:s/1",
        title="Sock Puppet Rampage demo is out",
        author="sockdev.bsky.social",
        hours_ago=4,
        likes=12,
        comments=2,
        channel="bluesky",
    )
    return {
        "reddit": StubCollector(ctx, [post], {post.key: wishlist_comments(n_intent=3, n_plain=9)}),
        "bluesky": StubCollector(ctx, [bsky], name="bluesky"),
    }


def wrap_send(fake: FakeDiscord, should_fail):
    """Make ``fake.send_message`` raise ``should_fail(channel_id, payload)`` when it returns an error."""
    original = fake.send_message

    def send(channel_id, payload):
        error = should_fail(channel_id, payload)
        if error is not None:
            fake.calls.append(("send_message_failed", channel_id))
            raise error
        return original(channel_id, payload)

    fake.send_message = send  # instance attribute: Publisher calls api.send_message


class FakeLLM:
    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = 0

    def classify(self, mention: Mention):
        self.calls += 1
        return self.verdict

    def close(self):
        pass


# ----------------------------------------------------------------------------- 1. roundup entries


def test_roundup_entry_that_failed_to_post_is_not_recorded_as_posted():
    """pipeline.py decide_and_post: ``sent_roundup = list(plan.roundup)`` marks EVERY planned
    entry as posted, although Publisher.post_roundup skips entries whose send failed. The
    game gets ``roundup_at`` and never appears in a roundup (it was never shown to anyone)."""
    world = World()
    roundup = world.channel("roundup")
    failures = {"left": 1}

    def entry_fails_once(channel_id, payload):
        is_header = "Gem Roundup" in (payload.get("content") or "")
        if channel_id == roundup and not is_header and failures["left"]:
            failures["left"] -= 1
            return HttpError("HTTP 500", 500)
        return None

    wrap_send(world.discord, entry_fails_once)
    first = world.run(NOW, medium_collectors)
    assert first.plan and len(first.plan.roundup) == 1
    gid = first.plan.roundup[0].game_id
    entries = [m for m in first.posted if m.kind == "roundup"]
    assert entries == []  # only the header went out; the game's own message failed

    # Two hours later the next roundup is due: the game was never shown, so it must be in it.
    second = world.run(NOW + timedelta(hours=2), medium_collectors)
    assert second.plan.roundup_due and world.state.games[gid].last_score >= 45  # still roundup-worthy
    assert gid in [d.game_id for d in second.plan.roundup], (
        f"posted.games[{gid}] = {world.state.posted.games.get(gid)}: recorded as posted although "
        "its roundup message was never sent"
    )


# ----------------------------------------------------------------------------- 2. status messages


def test_broken_source_alert_is_not_lost_when_the_status_post_fails():
    """pipeline.py update_health sets ``alerted_broken = True`` before anything is posted. If
    the status message then fails (HttpError, caught as a warning), the "source is broken"
    alert is never sent: later runs think the humans were already told."""
    world = World()
    status = world.channel("status")
    fail_status = {"on": False}
    wrap_send(
        world.discord,
        lambda cid, _p: HttpError("HTTP 503", 503) if fail_status["on"] and cid == status else None,
    )
    failing = lambda ctx: {"itch": StubCollector(ctx, name="itch", fail="HTTP 503")}  # noqa: E731
    now = NOW
    for i in range(6):
        fail_status["on"] = i == 5  # Discord hiccups exactly on the run that should alert
        world.run(now, failing)
        now += HALF_HOUR
    fail_status["on"] = False
    for _ in range(3):  # itch keeps failing, Discord works again
        world.run(now, failing)
        now += HALF_HOUR
    alerts = [
        p
        for cid, p in world.sent_since_setup()
        if cid == status and "itch" in p["embeds"][0].get("description", "")
    ]
    assert alerts, (
        f"itch failed {world.state.meta.source_health['itch'].consecutive_failures} runs in a row "
        "but no status alert ever reached Discord"
    )


def test_keepalive_reminder_is_not_lost_when_the_status_post_fails():
    """Same root cause: check_keepalive stamps ``keepalive_reminded_at`` before posting, so a
    failed status post silences the 60-day-inactivity reminder for ``remind_every_days``."""
    world = World()
    status = world.channel("status")
    fail_status = {"on": True}
    wrap_send(
        world.discord,
        lambda cid, _p: HttpError("HTTP 503", 503) if fail_status["on"] and cid == status else None,
    )
    env = {"GEMBOT_DEFAULT_BRANCH_COMMIT_TS": str(int((NOW - timedelta(days=50)).timestamp()))}
    quiet = lambda ctx: {}  # noqa: E731
    first = world.run(NOW, quiet, env=env)
    assert any("not posted" in w for w in first.warnings)
    fail_status["on"] = False
    world.run(NOW + HALF_HOUR, quiet, env=env)
    reminders = [p for cid, p in world.sent_since_setup() if cid == status]
    assert reminders, "the keep-alive reminder was never delivered (next try only in 7 days)"


# ----------------------------------------------------------------------------- 3. LLM exclusion


def test_llm_not_a_game_exclusion_is_lifted_once_the_game_gets_a_steam_page():
    """score() only applies the LLM "not a specific game" exclusion when the game has no
    Steam app, but the exclusion is sticky: mark_excluded keeps any "(LLM)" reason and
    prefilter skips every game with ``excluded_reason`` set. So once excluded, the game is
    never scored again, even after a Steam page is linked to it."""
    world = World()
    nope = FakeLLM(LLMVerdict(game_title=None, is_a_specific_game=False, friendslop_fit=0.0))

    def teaser(ctx):
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

    first = world.run(NOW, teaser, llm=nope)
    (gid,) = [g for g, r in first.results.items() if r.excluded]
    assert "LLM" in (world.state.games[gid].excluded_reason or "")

    def steam_page(ctx):
        post = make_mention(
            "reddit",
            "t2",
            title="Moon Soup Simulator is a co-op game for 4 friends, wishlist it",
            text="https://store.steampowered.com/app/5550555/Moon_Soup_Simulator/",
            hours_ago=1,
            likes=80,
            comments=6,
            channel="r/IndieGaming",
        )
        return {"reddit": StubCollector(ctx, [post])}

    second = world.run(NOW + HALF_HOUR, steam_page, llm=nope)
    game = world.state.games[gid]
    assert game.steam_appid == 5550555  # the Steam page was attached to the same game
    assert gid in second.results and not second.results[gid].excluded, (
        f"still excluded: {game.excluded_reason!r}; shortlist={second.shortlist}"
    )


# ----------------------------------------------------------------------------- 4. crash after posting


def test_discord_budget_running_out_does_not_crash_the_run_and_re_alarm(tmp_path, monkeypatch):
    """DiscordClient charges every call to a per-run Budget and raises BudgetExceeded, which is
    NOT an HttpError. Pipeline.decide_and_post / feedback only catch HttpError, so the
    exception escapes run_scan *after* the alarm was sent and before state is saved: the next
    run alarms the same game again."""
    config = make_config()
    state_dir = tmp_path / "state"
    store = StateStore(state_dir)
    state = store.load()
    fake = FakeDiscord(guilds=["1"])
    run_setup(fake, config, state, now=NOW - timedelta(hours=1))
    store.save(state)
    channels = state.meta.discord.channels
    monkeypatch.setattr("gembot.pipeline.build_collectors", lambda ctx: hot_collectors(ctx))
    # a status line this run (keep-alive reminder) -> one more Discord call after the alarm
    monkeypatch.setenv("GEMBOT_DEFAULT_BRANCH_COMMIT_TS", str(int((NOW - timedelta(days=50)).timestamp())))
    out_of_budget = {"left": 1}

    def budget_runs_out_on_status(cid, _payload):
        if cid == channels["status"] and out_of_budget["left"]:
            out_of_budget["left"] -= 1
            return BudgetExceeded("discord: request budget of 150 used up for this run")
        return None

    wrap_send(fake, budget_runs_out_on_status)
    crashed = None
    try:
        run_scan(config, state_dir=state_dir, now=NOW, push=False, discord=fake, sleep=lambda s: None)
    except BudgetExceeded as exc:
        crashed = exc
    run_scan(config, state_dir=state_dir, now=NOW + HALF_HOUR, push=False, discord=fake, sleep=lambda s: None)
    alarms = [
        p
        for cid, p in fake.sent
        if cid == channels["alarm"] and p["embeds"][0]["title"] == "🚨 GEM ALARM: Gorilla Pizza Panic"
    ]
    assert crashed is None and len(alarms) == 1, f"run crashed with {crashed!r}; {len(alarms)} alarms sent"


# ----------------------------------------------------------------------------- 5. seen vs mentions


def steam_listing(appid: int = 4242424, title: str = "Moon Soup Simulator", **kw) -> Mention:
    info = {"appid": appid, "name": title, "coming_soon": True, "categories": ["Online Co-op"]}
    return make_mention(
        "steam",
        str(appid),
        title=title,
        url=f"https://store.steampowered.com/app/{appid}/",
        author=None,
        hours_ago=0,
        channel="steam:comingsoon-indie-coop",
        extra={"steam": info},
        **kw,
    )


def test_listing_older_than_the_seen_window_is_not_new_again_every_run():
    """seen.json keeps 14 days, games.json 30. After day 14 prune() drops the seen entry
    while the mention is still stored, ingest() then treats it as new (``is_new``),
    re-adds ``seen`` with the *old* first_seen, the next prune drops it again, and so on:
    every run from day 14 to 30 counts every still-listed Steam game as a new mention
    and re-resolves it."""
    world = World()
    listing = lambda ctx: {"steam": StubCollector(ctx, [steam_listing()], name="steam")}  # noqa: E731
    assert run_like_scan(world, NOW, listing).new_mentions == 1
    new_later = {}
    for day in range(1, 18):
        now = NOW + timedelta(days=day)
        new_later[day] = run_like_scan(world, now, listing).new_mentions
    assert "steam:4242424" in world.state.mentions
    assert sum(new_later.values()) == 0, f"counted as a new mention again on days {new_later}"


def test_game_whose_listing_is_still_observed_is_not_pruned_as_stale():
    """Game.last_seen is only refreshed for *touched* mentions (new / numbers changed), not for a
    mention that is re-observed unchanged (ingest sets ``mention.observed_at`` only). prune()
    drops games by ``last_seen`` (and their mentions with them), so a Steam listing collected
    every single run is pruned after ``games_days`` and comes back as a brand-new game.
    With the defaults (seen 14 d < games 30 d) the seen-window churn above happens to touch
    the mention again from day 14 on and masks this; any ``games_days <= seen_days`` shows it,
    and so will fixing the churn."""
    settings = make_config().settings
    short = settings.model_copy(update={"state": settings.state.model_copy(update={"games_days": 10})})
    world = World(make_config(settings=short))

    def listing(ctx):  # like SteamCollector: created_at is the collection time
        return {"steam": StubCollector(ctx, [steam_listing(created_at=ctx.now)], name="steam")}

    world.run(NOW, listing)
    for day in range(1, 13):
        now = NOW + timedelta(days=day)
        last_seen = world.state.games["steam:4242424"].last_seen
        observed = world.state.mentions["steam:4242424"].observed_at
        prune(world.state, now, short.state, short.features.baseline_window_days)
        assert "steam:4242424" in world.state.games, (
            f"day {day}: game pruned although its listing was observed at {observed} "
            f"(Game.last_seen stuck at {last_seen})"
        )
        world.run(now, listing)
    game = world.state.games["steam:4242424"]
    assert game.first_seen == NOW and game.last_seen == NOW + timedelta(days=12)


def test_recollected_mention_keeps_the_first_seen_time_recorded_in_seen():
    """ingest(): for a mention whose body is not stored (dropped as unresolvable, or dropped by
    the size cap) but whose key IS in ``seen``, ``first_seen`` is reset to *now* instead of
    ``seen[key]``. A 4-day-old post then counts as "recent" again (velocity and cross)."""
    world = World()
    question = make_mention(
        "reddit", "q9", title="what engine should i use?", text="help", created_at=NOW - timedelta(hours=1)
    )
    first = world.run(NOW, lambda ctx: {"reddit": StubCollector(ctx, [question])})
    assert first.resolve.dropped == ["reddit:q9"] and world.state.seen["reddit:q9"] == NOW

    later = NOW + timedelta(days=4)
    edited = question.model_copy(
        update={
            "title": "EDIT: my co-op game Gravy Train Panic is on Steam now",
            "text": "https://store.steampowered.com/app/7777777/Gravy_Train_Panic/",
        }
    )
    second = world.run(later, lambda ctx: {"reddit": StubCollector(ctx, [edited])})
    stored = world.state.mentions["reddit:q9"]
    assert stored.game_id == "steam:7777777"
    result = second.results["steam:7777777"]
    assert stored.first_seen == world.state.seen["reddit:q9"] == NOW, f"first_seen={stored.first_seen}"
    assert result.evidence.sources_72h == []  # created and first seen 4 days ago: not recent


# ----------------------------------------------------------------------------- 6. huge input


def test_a_huge_feed_title_does_not_stall_the_run():
    """Resolver title extraction runs its regexes over the *whole* title (only the body is cut
    to MAX_TEXT_CHARS, and the RSS collector does not cap titles). A long Title Case run is
    matched in O(n^2): a 12 KB title costs ~6 s, a 64 KB one minutes, and it is redone every
    run because an unresolvable mention is dropped and re-ingested each time."""
    world = World()
    title = " ".join(["Foo"] * 3000)  # 12 KB, e.g. a broken feed that puts the whole page in <title>
    item = make_mention("youtube", "vid1", title=title, text="", channel="youtube:Devlogs", hours_ago=2)
    started = time.monotonic()
    world.run(NOW, lambda ctx: {"rss": StubCollector(ctx, [item], name="rss")})
    elapsed = time.monotonic() - started
    assert elapsed < 1.5, f"one 12 KB title took {elapsed:.1f}s"


# ----------------------------------------------------------------------------- 7. size cap


def test_saved_state_respects_max_bytes(tmp_path, monkeypatch):
    """run_scan prunes (and enforces ``state.max_bytes``) only BEFORE the pipeline; what the run
    adds is saved and pushed unchecked. With thousands of Steam listings the cap only shrinks
    the in-memory copy at load: mentions dropped for size are re-collected and saved again,
    so the branch never gets under the cap (simulated at 1/10 scale: 300 listings, 400 KB cap)."""
    settings = make_config().settings
    small = settings.model_copy(update={"state": settings.state.model_copy(update={"max_bytes": 400_000})})
    config = make_config(settings=small)
    listings = [
        steam_listing(
            100000 + i,
            f"Zorbo Quest {i} Deluxe",
            text="A chaotic co-op horror game for 1-4 friends with proximity voice chat and ragdoll physics. "
            * 3,
        )
        for i in range(300)
    ]
    monkeypatch.setattr(
        "gembot.pipeline.build_collectors", lambda ctx: {"steam": StubCollector(ctx, listings, name="steam")}
    )
    state_dir = tmp_path / "state"
    for i in range(3):  # steady state: every run prunes to the cap, then re-adds what it collected
        result = run_scan(
            config, state_dir=state_dir, now=NOW + i * HALF_HOUR, push=False, sleep=lambda s: None
        )
        assert result.collected == 300
    size = StateStore(state_dir).size_bytes()
    assert size <= 400_000, f"saved state is {size} bytes after 3 runs, cap is 400000"


# ----------------------------------------------------------------------------- 8. HTTP cache in state

YT = "https://www.youtube.com/feeds/videos.xml?channel_id=UC"


def conditional_feed_server(feeds: dict[str, tuple[str, str]]):
    """MockTransport serving ``url -> (body, Last-Modified)`` that honours If-Modified-Since /
    If-None-Match like a real server; everything else answers 404."""
    from email.utils import parsedate_to_datetime

    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        found = feeds.get(str(request.url))
        if found is None:
            return httpx.Response(404, request=request)
        body, last_modified = found
        etag = f'"{len(body)}-{body.count("<entry>")}-{body[-40:].__hash__() & 0xFFFF}"'
        since = request.headers.get("if-modified-since")
        if request.headers.get("if-none-match") == etag or (
            since and parsedate_to_datetime(last_modified) <= parsedate_to_datetime(since)
        ):
            return httpx.Response(304, request=request)
        headers = {"content-type": "application/xml", "last-modified": last_modified, "etag": etag}
        return httpx.Response(200, text=body, headers=headers, request=request)

    return httpx.MockTransport(handler)


def test_two_feeds_on_the_same_path_do_not_share_one_http_cache_entry(tmp_path):
    """http.py:151 builds the conditional-GET cache key with ``httpx.URL(url, params=None)``,
    which DROPS the URL's own query string. Every YouTube channel feed
    (videos.xml?channel_id=...) therefore shares ONE ``meta.http_cache`` entry: channel B is
    requested with channel A's If-Modified-Since / If-None-Match, answers 304, and its videos
    are never collected (in this run or, while A keeps refreshing the entry, any later one)."""
    from gembot.config import FeedConfig, Feeds
    from tests.factories import read_fixture

    body_a = read_fixture("rss", "youtube_channel.xml")
    body_b = body_a
    for old, new in (
        ("pReMiErE001", "bBbBbBbB001"),
        ("dQw4w9WgXcQ", "bBbBbBbB002"),
        ("aZXQk7zkdy0", "bBbBbBbB003"),
        ("OlDvIdEo123", "bBbBbBbB004"),
    ):
        body_b = body_b.replace(old, new)
    url_a, url_b = YT + "a" * 22, YT + "b" * 22
    transport = conditional_feed_server(
        {
            url_a: (body_a, "Sat, 03 Oct 2026 11:00:00 GMT"),
            url_b: (body_b, "Sat, 03 Oct 2026 09:30:00 GMT"),  # B is older than A, but we never read B
        }
    )
    feeds = Feeds(
        feeds=[
            FeedConfig(name="A", url=url_a, source="youtube"),
            FeedConfig(name="B", url=url_b, source="youtube"),
        ]
    )
    state_dir = tmp_path / "state"
    run_scan(
        make_config(feeds=feeds),
        state_dir=state_dir,
        now=NOW,
        push=False,
        transport=transport,
        sleep=lambda s: None,
    )
    state = StateStore(state_dir).load()
    assert any(k.startswith("youtube:pReMiErE") for k in state.seen)  # channel A was read
    assert any(k.startswith("youtube:bBbBbBbB") for k in state.seen), (
        f"channel B never collected; http_cache keys: {list(state.meta.http_cache)}"
    )


def test_smoke_reads_feeds_live_instead_of_replaying_the_state_validators(tmp_path):
    """smoke.py builds its HttpClient from the copied state, so the scan's ``meta.http_cache``
    validators are sent: an unchanged feed answers 304 and the smoke table says "ok, 0
    mentions" for a feed it never parsed (the smoke test is meant to hit every source live)."""
    from gembot.config import FeedConfig, Feeds
    from gembot.smoke import run_smoke
    from tests.factories import read_fixture

    url = "https://rss.app/feeds/AbCdEfGhIjKlMnOp.xml"
    config = make_config(feeds=Feeds(feeds=[FeedConfig(name="IG", url=url, source="instagram")]))
    transport = conditional_feed_server(
        {url: (read_fixture("rss", "rssapp_instagram.xml"), "Sat, 03 Oct 2026 10:10:41 GMT")}
    )
    state_dir = tmp_path / "state"
    run_scan(config, state_dir=state_dir, now=NOW, push=False, transport=transport, sleep=lambda s: None)
    assert StateStore(state_dir).load().meta.http_cache  # the scan remembered the feed's validators

    summary = tmp_path / "s.md"
    run_smoke(
        config,
        now=NOW + HALF_HOUR,
        state_dir=state_dir,
        summary_path=summary,
        transport=transport,
        sleep=lambda s: None,
    )
    row = next(line for line in summary.read_text().splitlines() if line.startswith("| rss |"))
    assert int(row.split("|")[4]) > 0, f"smoke parsed nothing from the live feed: {row}"


# ----------------------------------------------------------------------------- 9. hardening


def test_a_malformed_post_url_does_not_crash_the_run():
    """Hardening (no current collector emits such a URL): build_card -> publish._url_key calls
    urlsplit() unguarded, so one mention whose ``url`` is e.g. ``http://[::1/...`` raises
    ValueError inside decide_and_post - after earlier alarms of the run were sent and before
    state is saved. The resolver already guards the same call (entity._split_url)."""
    world = World()

    def collectors(ctx):
        made = hot_collectors(ctx)
        made["reddit"].mentions[0].url = "http://[::1/r/IndieDev/comments/p1"
        return made

    result = world.run(NOW, collectors)
    assert result.plan and [d.game_id for d in result.plan.alarms] == ["steam:3141590"]
    assert world.state.posted.games["steam:3141590"].alarmed_at == NOW
