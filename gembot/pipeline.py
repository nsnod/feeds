"""One GemBot run: collect -> resolve -> prefilter -> enrich -> score -> decide -> post -> learn.

The pipeline works on an in-memory :class:`~gembot.models.State`; loading, pruning,
saving and pushing that state is done by :func:`run_scan` (and the CLI). Every external
dependency (HTTP transport, Discord API, clock, collectors) is injectable, which is what
makes the offline replay (``python -m gembot replay``) and the tests possible.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from gembot.collectors import build_collectors
from gembot.collectors.base import CollectContext, Collector, SourceReport
from gembot.config import Config
from gembot.discord.feedback import apply_feedback, collect_reactions, maybe_weekly_note
from gembot.discord.publish import Publisher, build_card
from gembot.discord.rest import DiscordAPI
from gembot.enrich.comments import enrich_audience, enrich_game_comments
from gembot.enrich.entity import LinkExpander, Resolver, ResolveResult
from gembot.enrich.llm import LLMClassifier, build_llm
from gembot.http import Budget, HttpClient, HttpError
from gembot.models import (
    BaselineSample,
    CommentSignals,
    DecisionKind,
    Features,
    GamePostState,
    LabelExample,
    Mention,
    PostedMessage,
    ScoreResult,
    Snapshot,
    SourceHealth,
    State,
    SteamInfo,
)
from gembot.scoring.decide import PostPlan, commit_plan, plan_posts
from gembot.scoring.features import ScoringContext
from gembot.scoring.score import prefilter, score_game
from gembot.scoring.weights import current_weights

log = logging.getLogger("gembot.pipeline")

MAX_HISTORY = 48  # snapshots kept per mention (one per run ~= 24h)
STEAM_REFRESH = timedelta(hours=24)


@dataclass
class RunResult:
    now: datetime
    reports: dict[str, SourceReport] = field(default_factory=dict)
    collected: int = 0
    new_mentions: int = 0
    resolve: ResolveResult | None = None
    shortlist: list[str] = field(default_factory=list)
    results: dict[str, ScoreResult] = field(default_factory=dict)
    plan: PostPlan | None = None
    posted: list[PostedMessage] = field(default_factory=list)
    status_lines: list[str] = field(default_factory=list)
    labels: list[LabelExample] = field(default_factory=list)
    weekly_note: str | None = None
    warnings: list[str] = field(default_factory=list)

    def top(self, n: int = 10) -> list[ScoreResult]:
        ranked = [r for r in self.results.values() if not r.excluded]
        return sorted(ranked, key=lambda r: r.score, reverse=True)[:n]


class Pipeline:
    def __init__(
        self,
        config: Config,
        state: State,
        *,
        http: HttpClient,
        now: datetime,
        discord: DiscordAPI | None = None,
        collectors: Mapping[str, Collector] | None = None,
        llm: LLMClassifier | None = None,
        post: bool = True,
        sleep: Callable[[float], None] | None = None,
    ):
        self.config = config
        self.settings = config.settings
        self.state = state
        self.http = http
        self.now = now
        self.discord = discord
        self.post = post
        self.sleep = sleep
        self.result = RunResult(now=now)
        ctx = CollectContext(config=config, http=http, now=now, state=state)
        self.collectors: dict[str, Collector] = (
            dict(collectors) if collectors is not None else build_collectors(ctx)
        )
        self.llm = llm
        self.scoring = ScoringContext(
            settings=config.settings, sources=config.sources, now=now, baselines=state.baselines
        )

    # ------------------------------------------------------------------ run
    def run(self) -> RunResult:
        mentions = self.collect()
        self.update_health()
        touched = self.ingest(mentions)
        touched_games = self.resolve(touched)
        weights = current_weights(self.state.weights, self.settings.weights)
        self.result.shortlist = prefilter(
            self.state.games,
            self.state.mentions,
            sorted(touched_games),
            self.scoring,
            weights,
            self.config.blocklist,
            self.settings.run.shortlist_size,
        )
        self.enrich(self.result.shortlist)
        self.score(self.result.shortlist, weights)
        self.decide_and_post()
        self.feedback()
        meta = self.state.meta
        meta.last_run_at = self.now
        meta.run_count += 1
        return self.result

    # ------------------------------------------------------------------ 1 collect
    def collect(self) -> list[Mention]:
        found: list[Mention] = []
        for name, collector in self.collectors.items():
            mentions, report = collector.run()
            self.result.reports[name] = report
            log.info(report.summary())
            for error in report.errors[:5]:
                log.warning("%s: %s", name, error)
            found.extend(mentions)
        self.result.collected = len(found)
        return found

    def update_health(self) -> None:
        threshold = self.settings.discord.status_failure_threshold
        health_map = self.state.meta.source_health
        for name, report in self.result.reports.items():
            if report.skipped:
                continue
            health = health_map.setdefault(name, SourceHealth())
            if report.ok:
                if health.alerted_broken:
                    self.result.status_lines.append(
                        f"✅ **{name}** is working again (after {health.consecutive_failures} failed runs)."
                    )
                health.consecutive_failures = 0
                health.alerted_broken = False
                health.last_ok_at = self.now
            else:
                health.consecutive_failures += 1
                health.last_error_at = self.now
                health.last_error = (report.errors[-1] if report.errors else "unknown error")[:300]
                if health.consecutive_failures >= threshold and not health.alerted_broken:
                    health.alerted_broken = True
                    self.result.status_lines.append(
                        f"⚠️ **{name}** has failed {health.consecutive_failures} runs in a row. "
                        f"Last error: `{health.last_error}`"
                    )

    # ------------------------------------------------------------------ 2 ingest
    def ingest(self, mentions: list[Mention]) -> list[str]:
        """Merge freshly collected mentions into state; return keys of new/changed mentions."""
        state = self.state
        touched: list[str] = []
        for mention in mentions:
            key = mention.key
            first_seen = state.seen.get(key)
            is_new = first_seen is None
            stored = state.mentions.get(key)
            if stored is not None:
                merged = _merge_mention(stored, mention)
            else:
                merged = mention
                merged.first_seen = mention.first_seen or self.now
            if is_new:
                self.result.new_mentions += 1
                state.seen[key] = merged.first_seen or self.now
            merged.first_seen = merged.first_seen or state.seen[key]
            merged.observed_at = self.now
            changed = is_new or _numbers_changed(stored, merged)
            _append_snapshot(merged, self.now)
            state.mentions[key] = merged
            self._record_baseline(merged)
            if changed or merged.game_id is None or merged.game_id not in state.games:
                touched.append(key)
        return touched

    def _record_baseline(self, mention: Mention) -> None:
        if mention.source in {"steam", "itch"}:
            return  # ranked listings, no engagement numbers
        age = mention.age_hours(self.now)
        if age < 1.0:
            return  # too young to say anything about its hourly pace
        channel = mention.channel or mention.source
        eph = mention.engagement.total / max(age, 0.5)
        self.state.baselines.setdefault(channel, {})[mention.key] = BaselineSample(at=self.now, eph=eph)

    # ------------------------------------------------------------------ 3 resolve
    def resolve(self, touched: list[str]) -> set[str]:
        state = self.state
        unresolved: list[Mention] = []
        games: set[str] = set()
        for key in touched:
            mention = state.mentions[key]
            game = state.games.get(mention.game_id) if mention.game_id else None
            if game is None and mention.game_id in state.meta.game_aliases:
                game = state.games.get(state.meta.game_aliases[mention.game_id])
                if game is not None:
                    mention.game_id = game.game_id
            if game is None:
                unresolved.append(mention)
                continue
            game.last_seen = self.now
            if key not in game.mention_keys:
                game.mention_keys.append(key)
            games.add(game.game_id)
        expander = LinkExpander(
            self.http,
            Budget("resolver", self.settings.budgets.resolver),
            self.config.sources.resolver.shortener_hosts,
        )
        resolver = Resolver(
            state.games,
            state.mentions,
            now=self.now,
            settings=self.config.sources.resolver,
            expander=expander,
        )
        result = resolver.resolve(unresolved)
        self.result.resolve = result
        for absorbed, survivor in result.merged.items():
            self._apply_merge(absorbed, survivor)
            games.discard(absorbed)
        for key in result.dropped:
            # unresolvable mentions never become games; forget the body, keep "seen"
            state.mentions.pop(key, None)
        for game_id in result.assignments.values():
            games.add(state.meta.game_aliases.get(game_id, game_id))
        return {g for g in games if g in state.games}

    def _apply_merge(self, absorbed: str, survivor: str) -> None:
        meta, posted = self.state.meta, self.state.posted
        meta.game_aliases[absorbed] = survivor
        for old, new in list(meta.game_aliases.items()):
            if new == absorbed:
                meta.game_aliases[old] = survivor
        if absorbed in posted.games:
            old = posted.games.pop(absorbed)
            new = posted.games.setdefault(survivor, GamePostState())
            new.alarmed_at = _earliest(new.alarmed_at, old.alarmed_at)
            new.alarm_score = new.alarm_score if new.alarm_score is not None else old.alarm_score
            new.roundup_at = _earliest(new.roundup_at, old.roundup_at)
            new.roundup_score = new.roundup_score if new.roundup_score is not None else old.roundup_score
            new.pending_roundup = new.pending_roundup or old.pending_roundup
            new.would_have_alarmed = new.would_have_alarmed or old.would_have_alarmed
        for message in posted.messages.values():
            for entry in message.entries:
                if entry.game_id == absorbed:
                    entry.game_id = survivor

    # ------------------------------------------------------------------ 4 enrich
    def enrich(self, shortlist: list[str]) -> None:
        if self.llm is None:
            self.llm = build_llm(self.config, self.http)
        steam = self.collectors.get("steam")
        self._signals: dict[str, CommentSignals] = {}
        for game_id in shortlist:
            game = self.state.games.get(game_id)
            if game is None:
                continue
            if game.steam_appid and steam is not None and hasattr(steam, "fetch_appdetails"):
                fresh = (
                    game.steam and game.steam.fetched_at and self.now - game.steam.fetched_at < STEAM_REFRESH
                )
                if not fresh:
                    info = _safe_appdetails(steam, game.steam_appid)
                    if info is not None:
                        game.steam = info
                        game.developer = game.developer or (info.developers[0] if info.developers else None)
                        game.publisher = game.publisher or (info.publishers[0] if info.publishers else None)
                        game.thumb = game.thumb or info.header_image
            enrich_audience(game, self.state.mentions, self.collectors)
            self._signals[game_id] = enrich_game_comments(
                game, self.state.mentions, self.collectors, now=self.now, settings=self.settings
            )
            if self.llm is not None and game.llm is None:
                best = _best_mention(game.mention_keys, self.state.mentions)
                if best is not None:
                    verdict = self.llm.classify(best)
                    if verdict is not None:
                        game.llm = verdict
                        game.pitch = game.pitch or verdict.one_line_pitch

    # ------------------------------------------------------------------ 5 score
    def score(self, shortlist: list[str], weights: dict[str, float]) -> None:
        signals = getattr(self, "_signals", {})
        for game_id in shortlist:
            game = self.state.games.get(game_id)
            if game is None:
                continue
            mentions = [self.state.mentions[k] for k in game.mention_keys if k in self.state.mentions]
            if game.llm is not None and not game.llm.is_a_specific_game and not game.steam_appid:
                game.excluded_reason = "not a specific game (LLM)"
            result = score_game(
                game, mentions, self.scoring, weights, self.config.blocklist, signals.get(game_id)
            )
            if game.excluded_reason and game.excluded_reason.endswith("(LLM)") and not result.excluded:
                result.excluded = True
                result.exclude_reason = game.excluded_reason
                result.score = 0.0
            game.last_score = result.score
            game.last_scored_at = self.now
            game.last_features = result.features
            game.last_reasons = result.reasons
            game.excluded_reason = result.exclude_reason if result.excluded else None
            self.result.results[game_id] = result

    # ------------------------------------------------------------------ 6+7 decide & post
    def decide_and_post(self) -> None:
        state = self.state
        plan = plan_posts(
            self.result.results,
            state.games,
            state.posted,
            state.meta,
            now=self.now,
            settings=self.settings.decisions,
        )
        self.result.plan = plan
        publisher = self._publisher()
        if publisher is None:
            if self.post:
                self.result.warnings.append(
                    "Discord is not set up: nothing was posted (run the Setup workflow)."
                )
            return
        sent_alarms, sent_roundup = [], []
        for decision in plan.alarms:
            game = state.games[decision.game_id]
            card = build_card(
                game,
                state.mentions,
                score=decision.score,
                reasons=self._reasons(decision.game_id),
                decision=decision,
                max_links=self.settings.discord.max_links_in_alarm,
            )
            try:
                message = publisher.post_alarm(card, self._features(decision.game_id), self.now)
            except HttpError as exc:
                self.result.warnings.append(f"alarm for {game.title} not posted: {exc}")
                continue
            self._remember(message)
            sent_alarms.append(decision)
        roundup_ok = True
        if plan.roundup:
            cards = []
            for decision in plan.roundup:
                game = state.games[decision.game_id]
                card = build_card(
                    game,
                    state.mentions,
                    score=decision.score,
                    reasons=self._reasons(decision.game_id),
                    decision=decision,
                    max_links=3,
                )
                cards.append((card, self._features(decision.game_id)))
            try:
                for message in publisher.post_roundup(cards, self.now):
                    self._remember(message)
                sent_roundup = list(plan.roundup)
            except HttpError as exc:
                roundup_ok = False
                self.result.warnings.append(f"roundup not posted: {exc}")
        committed = PostPlan(
            alarms=sent_alarms,
            roundup=sent_roundup,
            roundup_due=plan.roundup_due and roundup_ok,
            carried=plan.carried,
        )
        commit_plan(committed, state.posted, state.meta, now=self.now)
        if self.result.status_lines:
            try:
                message = publisher.post_status(self.result.status_lines, self.now)
                if message is not None:
                    self._remember(message)
            except HttpError as exc:
                self.result.warnings.append(f"status message not posted: {exc}")

    def _publisher(self) -> Publisher | None:
        if not self.post or self.discord is None:
            return None
        channels = self.state.meta.discord.channels
        if not {"alarm", "roundup", "status"} <= set(channels):
            return None
        kwargs = {"sleep": self.sleep} if self.sleep is not None else {}
        return Publisher(
            self.discord,
            channels,
            self.settings,
            ping_role_id=self.config.secrets.alarm_ping_role_id,
            **kwargs,
        )

    def _remember(self, message: PostedMessage) -> None:
        self.state.posted.messages[message.message_id] = message
        self.result.posted.append(message)

    def _reasons(self, game_id: str) -> list[str]:
        if game_id in self.result.results:
            return self.result.results[game_id].reasons
        return self.state.games[game_id].last_reasons

    def _features(self, game_id: str) -> Features:
        if game_id in self.result.results:
            return self.result.results[game_id].features
        return self.state.games[game_id].last_features or Features()

    # ------------------------------------------------------------------ 9 learn
    def feedback(self) -> None:
        fb = self.settings.feedback
        if not fb.enabled or self.discord is None or not self.post:
            return
        try:
            reactions = collect_reactions(
                self.discord,
                self.state.posted,
                now=self.now,
                settings=fb,
                budget=Budget("discord_feedback", self.settings.budgets.discord_feedback),
                bot_user_id=self.state.meta.discord.bot_user_id,
            )
        except HttpError as exc:
            self.result.warnings.append(f"could not read reactions: {exc}")
            return
        labels = apply_feedback(
            self.state.posted,
            self.state.weights,
            reactions,
            now=self.now,
            defaults=self.settings.weights,
            settings=fb,
        )
        self.state.labels.extend(labels)
        self.result.labels = labels
        note = maybe_weekly_note(
            self.state.weights, self.state.labels, now=self.now, settings=fb, defaults=self.settings.weights
        )
        self.result.weekly_note = note
        publisher = self._publisher()
        if note and publisher is not None:
            try:
                message = publisher.post_status([note], self.now)
                if message is not None:
                    self._remember(message)
            except HttpError as exc:
                self.result.warnings.append(f"weekly note not posted: {exc}")


# ---------------------------------------------------------------------- helpers


def _earliest(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _merge_mention(stored: Mention, fresh: Mention) -> Mention:
    """Fresh numbers/text win; identity, history and enrichment results are kept."""
    merged = fresh.model_copy(deep=True)
    merged.first_seen = stored.first_seen or fresh.first_seen
    merged.game_id = stored.game_id
    merged.history = list(stored.history)
    merged.comments_fetched_at = stored.comments_fetched_at
    merged.signals = stored.signals
    merged.created_at = min(stored.created_at, fresh.created_at)
    if merged.author_audience is None:
        merged.author_audience = stored.author_audience
    extra = dict(stored.extra)
    extra.update({k: v for k, v in fresh.extra.items() if v is not None})
    merged.extra = extra
    if not merged.media_thumb:
        merged.media_thumb = stored.media_thumb
    return merged


def _numbers_changed(stored: Mention | None, fresh: Mention) -> bool:
    if stored is None:
        return True
    a, b = stored.engagement, fresh.engagement
    return (a.likes, a.comments, a.shares, stored.rank) != (b.likes, b.comments, b.shares, fresh.rank)


def _append_snapshot(mention: Mention, now: datetime) -> None:
    followers = None
    steam = mention.extra.get("steam") if mention.source == "steam" else None
    if isinstance(steam, dict):
        followers = steam.get("followers")
    snap = Snapshot(
        at=now,
        likes=mention.engagement.likes,
        comments=mention.engagement.comments,
        shares=mention.engagement.shares,
        rank=mention.rank,
        followers=followers,
    )
    mention.history.append(snap)
    if len(mention.history) > MAX_HISTORY:
        mention.history = mention.history[-MAX_HISTORY:]


def _best_mention(keys: list[str], mentions: dict[str, Mention]) -> Mention | None:
    candidates = [mentions[k] for k in keys if k in mentions]
    if not candidates:
        return None
    return max(candidates, key=lambda m: (m.engagement.total, len(m.text) + len(m.title)))


def _safe_appdetails(steam: Collector, appid: int) -> SteamInfo | None:
    try:
        return steam.fetch_appdetails(appid)  # type: ignore[attr-defined]
    except Exception as exc:  # budget exhausted, HTTP errors, parse errors
        log.info("appdetails %s skipped: %s", appid, exc)
        return None


def decision_counts(result: RunResult) -> dict[str, int]:
    plan = result.plan
    if plan is None:
        return {"alarm": 0, "roundup": 0}
    return {
        DecisionKind.ALARM.value: len(plan.alarms),
        DecisionKind.ROUNDUP.value: len(plan.roundup),
    }


def default_state_dir() -> Path:
    return Path("state")
