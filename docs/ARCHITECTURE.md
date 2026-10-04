# GemBot architecture and module contracts

This is the developer map. `BUILD_SPEC.md` says *what* GemBot does; this file pins down
*how the modules talk to each other*: the exact function and class names each module
exposes. Shared data types live in `gembot/models.py`, configuration in
`gembot/config.py`, HTTP in `gembot/http.py` and the collector base class in
`gembot/collectors/base.py`. Those four files are the frozen core; read them first.

## Conventions

* Python 3.12, `from __future__ import annotations`, type hints everywhere, ruff clean
  (`ruff check` + `ruff format`, line length 110).
* All times are timezone-aware UTC `datetime`s. **Never call `datetime.now()` inside
  logic.** `now` is always passed in (the replay and tests depend on this).
* No module touches the network except through `gembot.http.HttpClient` with a
  `Budget`. Tests use `respx` (or `httpx.MockTransport` passed as `transport=`) and a
  no-op `sleep`. No test may hit the real network.
* A failure in one source/unit never crashes a run (see `Collector.guard`).
* Never log secrets. `Secrets.__repr__` already hides values.
* Coverage target is >= 85% of `gembot/` overall; aim for >= 90% in every module you own.

## Run flow (`gembot/pipeline.py`)

```
load state -> prune
  -> run collectors (each isolated, budgeted)           collectors/*
  -> update source health (status-channel messages)
  -> merge mentions into state: seen, history snapshots, baselines
  -> resolve mentions into games                         enrich/entity.py
  -> drop blocklisted / banned keyword games             scoring/score.py
  -> Stage A prefilter: cheap score, keep top N=40       scoring/score.py
  -> Stage B enrich shortlist: comments, audience,
     Steam appdetails for linked games, optional LLM     enrich/comments.py, enrich/signals.py, enrich/llm.py
  -> full score + reasons                                scoring/features.py, score.py, explain.py
  -> decide: alarms (caps), roundup if due               scoring/decide.py
  -> post to Discord                                     discord/publish.py (+ rest.py / fake.py)
  -> read 👍/👎, update weights, weekly note             discord/feedback.py, scoring/weights.py
  -> save state, commit + push bot-state branch          state/store.py
```

## Module contracts

### collectors/ (one class per platform, all subclass `Collector`)

| module | class | `name` | notes |
|---|---|---|---|
| `steam.py` | `SteamCollector` | `steam` | also exposes `fetch_appdetails(appid) -> SteamInfo \| None` (used by enrichment for Steam links found in other posts) |
| `reddit.py` | `RedditCollector` | `reddit` | OAuth if `REDDIT_CLIENT_ID/SECRET`, else public `.json`, then `.rss`; implements `fetch_comments` |
| `itch.py` | `ItchCollector` | `itch` | RSS browse feeds; `rank`/`list_size` set from item order |
| `bluesky.py` | `BlueskyCollector` | `bluesky` | session via app password, else public AppView; implements `fetch_comments` (thread replies) and `fetch_audience` (followersCount) |
| `rss.py` | `RssCollector` | `rss` | feeds.yaml; `Mention.source` = the feed's platform (`instagram`/`tiktok`/`youtube`/`rss`) |
| `x.py` | `XCollector` | `x` | only enabled when `X_BEARER_TOKEN` is set |

`gembot/collectors/__init__.py` exposes `COLLECTOR_CLASSES` (ordered list) and
`build_collectors(ctx) -> dict[str, Collector]`.

Mention conventions:

* `source_id` is stable across runs for the same post/listing (so `Mention.key` dedupes).
* `channel` is the baseline bucket: `r/<Subreddit>` (exact subreddit name), `bluesky`,
  `x`, `itch:<feed name>`, `steam:<search name>`, `<platform>:<feed name>` for RSS.
* `engagement.likes` = upvotes/score/likes, `comments` = reported comment/reply count,
  `shares` = reposts/retweets/quotes.
* `links` = every outbound URL in the post (link post URL, URLs in text, link facets,
  embeds). The resolver canonicalises them.
* Steam mentions carry `extra["steam"] = SteamInfo.model_dump(mode="json")`.
* Collectors only *read* `ctx.state` (e.g. to skip appdetails for known apps).

### enrich/entity.py

```python
def canonical_hard_id(url: str) -> str | None           # "steam:<appid>" | "itch:<dev>/<slug>" | None
def extract_urls(text: str) -> list[str]
def hard_ids_for(mention: Mention, expander: LinkExpander | None = None) -> list[str]
def normalize_title(title: str) -> str                   # lowercase, strip punctuation/emoji/"(demo)" etc.
def title_candidates(mention: Mention) -> list[str]      # best first
def developer_hint(mention: Mention) -> str | None       # steam developer / itch dev / first-person post author

class LinkExpander:                                      # follows shortened links, budgeted, cached
    def __init__(self, http: HttpClient, budget: Budget, shortener_hosts: list[str]): ...
    def expand(self, url: str) -> str

@dataclass
class ResolveResult:
    assignments: dict[str, str]      # mention key -> game_id
    new_games: list[str]
    merged: dict[str, str]           # absorbed game_id -> surviving game_id
    dropped: list[str]               # mention keys that resolved to no game

class Resolver:
    def __init__(self, games: dict[str, Game], mentions: dict[str, Mention], *, now: datetime,
                 settings: ResolverSource, expander: LinkExpander | None = None,
                 llm_titles: dict[str, str] | None = None): ...
    def resolve(self, new_mentions: list[Mention]) -> ResolveResult   # mutates `games`, sets mention.game_id
```

`game_id` is stable forever: `steam:<appid>`, `itch:<dev>/<slug>` or `t:<slug>-<6 hex>`.
A title-only game that later gains a hard id keeps its id and records the hard id in
`Game.hard_ids`.

### enrich/signals.py

```python
def analyze_comments(comments: list[Comment], *, post_author: str | None = None,
                     post_comment_count: int = 0) -> CommentSignals
def merge_signals(items: list[CommentSignals]) -> CommentSignals
def intent_hits(text: str) -> list[str]
def negative_hits(text: str) -> list[str]
def is_roblox_joke(text: str) -> bool
```

The post author's own replies and bot accounts (`is_bot`, "AutoModerator") are ignored.
Counts are per *distinct commenter* where the spec says so.

### enrich/comments.py

```python
SOURCE_TO_COLLECTOR: dict[str, str]   # mention.source -> collector name ("instagram" -> "rss", ...)
def enrich_game_comments(game: Game, mentions: dict[str, Mention], collectors: Mapping[str, Collector], *,
                         now: datetime, settings: Settings, max_posts: int = 2) -> CommentSignals
def enrich_audience(game: Game, mentions: dict[str, Mention], collectors: Mapping[str, Collector]) -> int
```

Fetches top comments for the game's most engaged mentions that support comments, stores
`mention.comments`, `mention.comments_fetched_at`, `mention.signals`, respects
`run.comment_refresh_hours`, and returns the merged signals for the game.

### enrich/llm.py (optional)

```python
class LLMClassifier:
    def __init__(self, api_key: str, http: HttpClient, settings: LLMSettings, budget: Budget | None = None): ...
    def classify(self, mention: Mention) -> LLMVerdict | None      # never raises
def build_llm(config: Config, http: HttpClient) -> LLMClassifier | None   # None without ANTHROPIC_API_KEY
```

### scoring/

```python
# features.py
@dataclass
class ScoringContext:
    settings: Settings
    sources: Sources
    now: datetime
    baselines: dict[str, dict[str, BaselineSample]]
def baseline_eph(channel: str, source: str, ctx: ScoringContext) -> tuple[float, int]   # (median eph, samples)
def compute_features(game: Game, mentions: list[Mention], ctx: ScoringContext,
                     signals: CommentSignals | None = None) -> tuple[Features, Evidence]

# score.py
def blocklist_reason(game: Game, mentions: list[Mention], blocklist: Blocklist) -> str | None
def score_game(game: Game, mentions: list[Mention], ctx: ScoringContext, weights: dict[str, float],
               blocklist: Blocklist, signals: CommentSignals | None = None) -> ScoreResult  # reasons filled
def prefilter(games: dict[str, Game], mentions: dict[str, Mention], candidate_ids: Iterable[str],
              ctx: ScoringContext, weights: dict[str, float], blocklist: Blocklist, limit: int) -> list[str]

# explain.py
def build_reasons(result: ScoreResult, game: Game, *, now: datetime, max_reasons: int = 4) -> list[str]

# weights.py
def current_weights(state_weights: WeightsState, defaults: dict[str, float]) -> dict[str, float]
def normalize(weights: dict[str, float]) -> dict[str, float]
def update_weights(weights: dict[str, float], features: Features, label: float,
                   settings: FeedbackSettings) -> dict[str, float]
def describe_change(before: dict[str, float], after: dict[str, float], labels: list[LabelExample]) -> str | None

# decide.py
@dataclass
class PostPlan:
    alarms: list[Decision]
    roundup: list[Decision]      # empty when not due or nothing qualifies
    roundup_due: bool
    carried: list[str]           # game_ids that hit the alarm cap -> next roundup
def qualifies_for_alarm(result: ScoreResult, settings: DecisionSettings) -> bool
def plan_posts(results: dict[str, ScoreResult], games: dict[str, Game], posted: PostedState, meta: Meta,
               *, now: datetime, settings: DecisionSettings) -> PostPlan
def commit_plan(plan: PostPlan, posted: PostedState, meta: Meta, *, now: datetime) -> None
```

`plan_posts` is pure (no mutation). `commit_plan` records alarm/roundup timestamps,
daily alarm counts, `pending_roundup` carry-overs and `meta.last_roundup_at` once the
messages were actually sent. Roundup candidates come from the stored
`Game.last_score / last_scored_at / last_reasons` so games found between roundups are
not forgotten.

### discord/

```python
# rest.py
class DiscordAPI(Protocol):  # implemented by DiscordClient (real) and FakeDiscord (tests/replay)
    def me(self) -> dict
    def list_guilds(self) -> list[dict]
    def list_channels(self, guild_id: str) -> list[dict]
    def create_channel(self, guild_id: str, name: str, type: int, parent_id: str | None = None,
                       topic: str | None = None) -> dict
    def send_message(self, channel_id: str, payload: dict) -> dict
    def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> None
    def get_message(self, channel_id: str, message_id: str) -> dict
    def get_reaction_users(self, channel_id: str, message_id: str, emoji: str, limit: int = 100) -> list[dict]
class DiscordClient:  # REST v10, "Bot <token>", 429-aware via HttpClient + retry_after
    def __init__(self, token: str, http: HttpClient, budget: Budget, api_base: str = ...): ...

# fake.py
class FakeDiscord:  # in-memory DiscordAPI; records every payload in `.sent` as (channel_id, payload)

# embeds.py
def score_bar(score: float) -> str                     # "▰▰▰▰▰▰▰▱▱▱ 74"
def alarm_payload(card: GemCard, *, ping_role_id: str | None = None) -> dict
def roundup_header_payload(now: datetime, count: int) -> dict
def roundup_entry_payload(card: GemCard) -> dict
def status_payload(lines: list[str]) -> dict
def text_payload(text: str) -> dict
def welcome_payload() -> dict
def enforce_limits(payload: dict) -> dict              # truncates cleanly to Discord limits

# publish.py
def build_card(game: Game, mentions: dict[str, Mention], *, score: float, reasons: list[str],
               decision: Decision | None = None, max_links: int = 5) -> GemCard
class Publisher:
    def __init__(self, api: DiscordAPI, channels: dict[str, str], settings: Settings, *, ping_role_id=None): ...
    def post_alarm(self, card: GemCard, features: Features, now: datetime) -> PostedMessage
    def post_roundup(self, cards: list[tuple[GemCard, Features]], now: datetime) -> list[PostedMessage]
    def post_status(self, lines: list[str], now: datetime) -> PostedMessage | None

# feedback.py
def collect_reactions(api: DiscordAPI, posted: PostedState, *, now: datetime, settings: FeedbackSettings,
                      budget: Budget, bot_user_id: str | None) -> dict[str, tuple[int, int]]  # msg id -> (up, down)
def apply_feedback(posted: PostedState, weights: WeightsState, reactions: dict[str, tuple[int, int]], *,
                   now: datetime, defaults: dict[str, float], settings: FeedbackSettings) -> list[LabelExample]
def maybe_weekly_note(weights: WeightsState, labels: list[LabelExample], *, now: datetime,
                      settings: FeedbackSettings, defaults: dict[str, float]) -> str | None

# setup.py
@dataclass
class SetupResult: guild_id: str; category_id: str; channels: dict[str, str]; created: list[str]; posted: list[str]
def run_setup(api: DiscordAPI, config: Config, state: State, *, now: datetime, force_welcome: bool = False) -> SetupResult
def connect_gateway_once(token: str, timeout_s: float = 30.0) -> bool   # discord.py, intents none
```

Roundups are posted as **one header message plus one short message per game**, so each
game gets its own 👍/👎 (see `docs/SCORING.md`).

### state/store.py

```python
STATE_FILES = ("games.json", "seen.json", "posted.json", "baselines.json", "weights.json",
               "labels.jsonl", "meta.json")
class StateStore:
    def __init__(self, root: Path): ...
    def load(self) -> State            # missing/corrupt files -> defaults (corrupt file logged, not fatal)
    def save(self, state: State) -> None   # atomic writes, sorted keys, compact JSON
    def size_bytes(self) -> int
def prune(state: State, now: datetime, settings: StateSettings, baseline_days: int = 14) -> dict[str, int]
class GitStateRepo:
    def __init__(self, workdir: Path, branch: str = "bot-state", remote: str = "origin", run=subprocess.run): ...
    def commit_and_push(self, message: str, reapply: Callable[[], None], retries: int = 3) -> bool
def ensure_state_branch(repo_dir: Path, branch: str = "bot-state", remote: str = "origin") -> bool
```

`Mention.comments` bodies are never persisted (only `Mention.signals`), to keep the state
under `state.max_bytes`.
