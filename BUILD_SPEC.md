# GemBot — Build Spec for Claude Code

> **How to use this file (for the human):**
> 1. Create a new GitHub repo (a **public** repo is recommended; see "Cost" below). Add this file to it as `BUILD_SPEC.md`.
> 2. Open the repo in Claude Code on the web and send: **"Read BUILD_SPEC.md and build it phase by phase. Don't skip the tests or the verification steps."**
> 3. When it's done, follow the `README.md` it writes. Setup is: add the bot to your server, paste one secret, then click "Run" on the Setup workflow.
>
> Everything below this line is the prompt for Claude Code.

---

## 0. Your role and the goal

You are building **GemBot**, a Discord bot for a small indie game news channel run by two friends. It finds **upcoming small indie games**, especially **"friendslop"** games: cheap, chaotic co-op games you play with friends, often with proximity voice chat and physics or ragdoll humor, that do well with streamers (think Lethal Company, Content Warning, R.E.P.O., PEAK). It also catches early **breakout** indie games and games that **just started development** (first devlogs, first Steam pages, first trailers).

The users want to **be first to hear about a game, without spam**. The core of this project is the **hidden-gem scoring algorithm**, not just collecting posts.

It runs **entirely on GitHub Actions** (scheduled workflows). There is no always-on server. It talks to Discord over the REST API with a bot token.

**Rules for you:**
- Build in the phases below. Each phase ends with passing tests before you start the next.
- **Don't trust any endpoint, ID or limit in this spec without checking it.** Section 9 lists claims you must verify live and fix if they're wrong. Record what you found in `docs/VERIFICATION.md`.
- If one source fails, the rest of the run must still work. A broken source must never crash a run.
- Never commit secrets. All credentials come from GitHub Actions secrets or environment variables.
- Be polite to every API: send a descriptive User-Agent, cap requests per source per run, back off on 429 responses, and use ETag / If-Modified-Since where it's supported.
- Language: Python 3.12. Dependencies: `httpx`, `pydantic`, `feedparser`, `rapidfuzz`, `pyyaml`, `selectolax` (or `beautifulsoup4`), `discord.py` (only for the one-time gateway connect), and `pytest`, `respx`, `pytest-cov`, `ruff` for development.

---

## 1. Architecture

```
            ┌─────────── GitHub Actions: scan.yml (every 30 min) ───────────┐
            │                                                                │
 Sources ──▶│ 1 Collect ─▶ 2 Resolve games ─▶ 3 Prefilter ─▶ 4 Enrich ─▶     │
 Steam      │   (mentions)  (cluster mentions   (cheap score)  (comments,    │
 Reddit     │                into Game records)                sentiment)   │
 itch.io    │ ─▶ 5 Score ─▶ 6 Decide ─▶ 7 Post to Discord ─▶ 8 Save state     │
 Bluesky    │                (alarm / roundup / skip)                        │
 RSS slot   │ ─▶ 9 Read 👍/👎 reactions ─▶ adjust weights                     │
 (IG/TikTok │                                                                │
 /YouTube)  └────────────────────────────────────────────────────────────────┘
 X (off)                         state lives on the `bot-state` git branch
```

### Repo layout
```
gembot/
  __main__.py          # CLI: run | setup | smoke | connect-once | replay
  config.py            # loads config/*.yaml + env vars, validated with pydantic
  models.py            # Mention, Game, Features, ScoreResult, Decision
  pipeline.py          # orchestrates one run
  collectors/
    base.py            # Collector protocol, per-run request budget, error isolation
    steam.py  reddit.py  itch.py  bluesky.py  rss.py  x.py (disabled unless key)
  enrich/
    entity.py          # link canonicalization + fuzzy title clustering
    comments.py        # fetch top comments/replies for shortlisted posts
    signals.py         # hype / negativity / roblox-meme detection
    llm.py             # OPTIONAL Claude classifier (only if ANTHROPIC_API_KEY set)
  scoring/
    features.py  score.py  weights.py  explain.py
  discord/
    rest.py            # minimal REST client, rate-limit aware
    embeds.py          # alarm + roundup embed builders (enforce Discord limits)
    setup.py           # creates channels, posts welcome message
    feedback.py        # reads reactions, emits labels
  state/
    store.py           # JSON state on the bot-state branch, pruning
config/
  settings.yaml        # thresholds, caps, schedules, weights (defaults)
  sources.yaml         # subreddits, search terms, Steam tags, itch feeds
  feeds.yaml           # user-added RSS feeds (Instagram/TikTok via RSS.app, YouTube)
  blocklist.yaml       # big publishers/studios, banned keywords
tests/
  fixtures/            # recorded real responses (JSON/HTML/XML), scrubbed
  golden/              # expected Discord payloads
  test_*.py
.github/workflows/
  scan.yml  setup.yml  ci.yml  smoke.yml
docs/
  VERIFICATION.md  SCORING.md
README.md
```

---

## 2. Sources (collectors)

Every collector returns a list of `Mention`:
```
Mention: source, source_id, url, title, text, author, author_audience (followers /
subreddit subscribers / null), created_at, engagement {likes, comments, shares,
ratio}, links[] (outbound URLs), media_thumb, raw_tags[], channel (e.g. "r/IndieDev")
```

### 2.1 Steam (free, no key needed)
- Pull **coming soon** and newly listed games filtered toward indie + co-op/multiplayer, using the store search results JSON (`store.steampowered.com/search/results/?...&json=1` or `infinite=1`) and `api/featuredcategories`. Steam tag IDs to start from (**verify them**): Indie 492, Co-op 1685, Online Co-Op 3843, Multiplayer 3859.
- For each new app, call `api/appdetails?appids=` to get the release date, developer, publisher, categories (Online Co-op, Co-op, Multi-player…), price, header image and short description.
- **Freshness signal:** the first time we see an app counts as "page discovered." Store it.
- **Best effort:** follower/member count from the game's community hub. Track the growth between runs if it's available. If it's not reliable, drop it and write that down in VERIFICATION.md.
- A Steam store link inside any other mention is the **strongest** way to identify a game.

### 2.2 Reddit
- Subreddits (in `sources.yaml`): r/IndieGaming, r/indiegames, r/IndieDev, r/playmygame, r/CoOpGaming, r/SoloDevelopment, r/DestroyMyGame, r/godot, r/Unity3D, r/unrealengine, r/gamedev. Engine subreddits are where games appear **before** announcements.
- Read `/new` and `/rising` for each.
- **Access:** use OAuth (script app, `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET`) if those secrets exist. Otherwise fall back to the public `.json` endpoints, then `.rss`. Reddit's API access policy has been changing, and GitHub runner IPs are often blocked when unauthenticated. **Verify this**, document the result, and if Reddit is unavailable, log a clear warning (don't crash).
- Keep a rolling baseline per subreddit (median engagement per hour over the last 14 days) so a hot post in a small subreddit is judged against **that** subreddit's normal.

### 2.3 itch.io (free)
- RSS feeds for the browse pages (add `.xml` to the browse URL; **verify**): new-and-popular, newest, and tag feeds for multiplayer, co-op, local-multiplayer, horror, physics.
- itch has no engagement counts in RSS. Use **rank position and time spent on new-and-popular** as the velocity signal.

### 2.4 Bluesky
- `app.bsky.feed.searchPosts` with search terms from `sources.yaml` (for example `"proximity chat"`, `#indiedev co-op`, `#screenshotsaturday multiplayer`, `"wishlist" co-op`, `"announce trailer" indie`, `friendslop`).
- **Auth:** search may require a session. Use `BLUESKY_HANDLE` + `BLUESKY_APP_PASSWORD` → `com.atproto.server.createSession`. If the secrets are missing, try the public AppView, and skip with a warning if it refuses.
- For shortlisted posts, pull replies with `app.bsky.feed.getPostThread`, and the author's follower count with `app.bsky.actor.getProfile`.

### 2.5 RSS slot (Instagram, TikTok, YouTube and anything else)
- `config/feeds.yaml`:
  ```yaml
  feeds:
    - name: "Some indie curator (Instagram)"
      url: "https://rss.app/feeds/XXXX.xml"
      source: instagram
      audience: 25000        # optional, follower count for the underdog math
    - name: "Devlog channel (YouTube)"
      url: "https://www.youtube.com/feeds/videos.xml?channel_id=UCxxxx"
      source: youtube
  ```
- These carry little or no engagement data. They mostly count as **cross-platform mentions** and **freshness**, plus keyword fit.
- The README must explain, in plain words, how to make an RSS.app feed for an Instagram or TikTok account and paste it here.

### 2.6 X (off by default)
- Write `collectors/x.py` against the X API v2 recent search, enabled **only** if `X_BEARER_TOKEN` is set. Include tests with fixtures. In the README, note that this is a paid API.

---

## 3. Game resolution (turning mentions into games)

A `Game` collects every mention of one game across all sources.
1. **Hard IDs first:** canonicalize Steam app URLs (`/app/<id>`), itch URLs (`<dev>.itch.io/<slug>`), and follow redirects for shortened links (budgeted). Mentions with the same hard ID belong to the same game.
2. **Title matching:** extract candidate titles from the post title or text. Use patterns like quoted names, "my game X", "X is a co-op…", "X — announce trailer", and Title Case runs near words like game, demo, trailer, Steam or wishlist. Fuzzy-match (`rapidfuzz` token_set_ratio ≥ 90, plus the developer name if known) against games seen in the last 30 days.
3. **Optional LLM pass (`enrich/llm.py`):** only if `ANTHROPIC_API_KEY` is set, and only for shortlisted mentions. Use a small model (`claude-haiku-4-5-20251001`) and ask for strict JSON: `{game_title, is_a_specific_game, friendslop_fit_0_1, one_line_pitch}`. Hard cap: 40 calls per run. Without a key, everything still works using the heuristics.
4. Mentions that don't resolve to any game are dropped. They never get posted.

---

## 4. The hidden-gem algorithm

### 4.1 Two-stage pipeline (keeps the bot fast and polite)
- **Stage A, prefilter (cheap):** score every new mention using only the data already collected (velocity, fit keywords, freshness). Keep the top 40 games for this run.
- **Stage B, enrich (expensive):** for the shortlist only, fetch the top comments/replies (Reddit up to 100 top comments; Bluesky the thread), the author's audience, and Steam details. Then do the full scoring.

### 4.2 Features (each normalized to 0–1)
| Feature | What it measures | Default formula |
|---|---|---|
| `velocity` | engagement per hour vs that channel's normal | `v = eph / baseline_median`; `clamp(log2(v)/4)`, so 16× normal = 1.0. itch: rank-based. Steam: follower growth if available |
| `underdog` | big reaction for a small creator | `clamp(log10(1 + 1000·engagement / max(audience,100)) / 3)` |
| `cross` | showing up on several platforms | distinct sources in the last 72h: 1→0, 2→0.6, 3→0.85, 4+→1 |
| `fit` | friendslop / co-op fit | Steam categories (Online Co-op etc.), tags, keywords: proximity chat, "with friends", 2–4 players, co-op horror, physics, ragdoll, chaotic, party, "like Lethal Company/R.E.P.O./PEAK", "me and the boys". LLM score if available |
| `hype` | do people actually like the idea | from sampled comments: rate of **intent** phrases (wishlisted, take my money, need this, day one, when does it come out, playtest?, me and the boys, my friends would love this) and **distinct commenters**, with negativity subtracted |
| `meme` | **"Roblox clone" discourse** | count of comments joking that it's a Roblox game or clone ("roblox", "looks like roblox", "roblox clone", "this is a roblox game", "fortnite creative map"). `clamp(count/5)`, counted only if the post has ≥ 10 comments |
| `fresh` | newness | 1.0 if first seen <48h ago, linear decay to 0 at 14 days. +0.2 (capped at 1) if the Steam page has a future release date or is "coming soon" |

**Penalties (subtracted after weighting):**
- Publisher or developer in `blocklist.yaml` (big studios): the game is excluded entirely.
- Negativity (asset flip, scam, AI slop, stolen, abandoned) above 30% of sampled commenters: −15.
- Released more than 30 days ago and not a new Early Access launch: −20.
- A known self-promo spammer (the same author posting the same game more than 3 times in 7 days): −10.

### 4.3 Score
```
score = 100 · Σ(wᵢ·fᵢ) / Σwᵢ  − penalties
default weights: velocity .25, underdog .15, cross .15, fit .15, hype .15, meme .05, fresh .10
Roblox discourse bonus: +8 if meme ≥ 0.6 AND (velocity ≥ 0.4 OR comments ≥ 30)
```
The Roblox jokes count as **attention**, not as a negative. Lots of people joking that it's a Roblox clone means lots of people are watching it.

### 4.4 Decisions
- **🚨 GEM ALARM:** `score ≥ 72` **and** at least 2 of {velocity ≥ 0.5, hype ≥ 0.5, cross ≥ 0.6, meme ≥ 0.6}, **and** the game has never been alarmed before. Max 3 per run and 12 per day. Over the cap, the extras go to the next roundup, marked "would have alarmed."
- **Escalation:** a game that appeared in a roundup and later crosses the alarm threshold gets an alarm labeled "📈 Escalated."
- **📋 Roundup (every 2 hours):** the top 8 games with `score ≥ 45` that haven't been posted yet, sorted by score. **If nothing qualifies, post nothing.** A game can come back in a later roundup only if its score went up by 15 or more since it was posted ("📈 heating up").
- Every threshold, cap and the roundup interval live in `settings.yaml`.

### 4.5 Learning from 👍 / 👎
- The bot adds 👍 and 👎 to every alarm and roundup entry. (Roundups go out as one embed per game, grouped into one message with up to 10 embeds, so reactions apply to the message. **Alternative:** post each roundup game as its own short message. Choose one, explain why in SCORING.md.)
- Each run, read reactions from human (non-bot) users on posts up to 7 days old. Label = (👍 − 👎) clamped to ±1.
- Update: `wᵢ ← clip(wᵢ · (1 + 0.05 · label · (fᵢ − mean_f)), 0.02, 0.5)`, then renormalize. Store every labeled example in state so the weights can be re-fit from history later.
- Post a short weekly note in the status channel describing how the weights changed ("You've been 👍-ing games with strong comment hype; hype weight went from 0.15 → 0.19").

### 4.6 "Why" explanations
Every post includes 2–4 plain-English reasons built from the strongest features, with the real numbers. For example:
- "312 upvotes in 3h on r/IndieDev (9× normal for that sub)"
- "Seen on Reddit, Bluesky and Steam in the last 24h"
- "17 different people said they wishlisted or want to play with friends"
- "🧱 Roblox-clone discourse: 11 commenters joking it's a Roblox game"

Write `docs/SCORING.md` explaining all of this for non-programmers.

---

## 5. Discord

### 5.1 Install flow (must stay this simple)
1. The user creates a Discord application and bot in the Developer Portal, copies the token, and opens an invite link. The README gives the exact link format and permissions: View Channels, Send Messages, Embed Links, Add Reactions, Read Message History, Manage Channels.
2. The user adds the GitHub secret `DISCORD_BOT_TOKEN`.
3. The user runs **Actions → "Setup GemBot" → Run workflow**. `setup.yml` then:
   - connects to the Discord gateway once (new bots may need one gateway connection before they can send messages over REST; **verify** and keep this step either way, since it's harmless), then disconnects;
   - finds the server (if the bot is in exactly one guild, uses it; otherwise requires the secret `DISCORD_GUILD_ID` and explains this in the log);
   - creates (or reuses) `#gem-alarm`, `#gem-roundup` and `#gembot-status` under a "GemBot" category;
   - creates the `bot-state` branch if it's missing;
   - posts a welcome message that explains the 👍 / 👎 reactions, plus a test alarm marked "TEST".
4. Done. The README explains how to set `#gem-alarm` to **All Messages** notifications in the Discord mobile app so both phones ping instantly.

### 5.2 Messages
- **Alarm embed:** title `🚨 GEM ALARM: <Game>`, link to the best URL (Steam > itch > post), Gem Score with a bar (for example `▰▰▰▰▰▰▰▱▱▱ 74`), the reasons, a one-line pitch, a "Where people are talking" field with up to 5 source links labeled by platform, a thumbnail, and a footer with the first-seen time. Optional role ping if `ALARM_PING_ROLE_ID` is set.
- **Roundup:** header `📋 Gem Roundup — <time>`, compact entries (name, score, top reason, links).
- **Status channel:** a source health summary only when something changes (a source broke or recovered after 6 or more consecutive failures). No noise.
- Enforce Discord limits in `embeds.py` (title 256, description 4096, 25 fields, field value 1024, 6000 total, 10 embeds per message), truncating cleanly. Handle 429 responses using `retry_after`.

---

## 6. State (GitHub Actions has no memory between runs)

- JSON files on an orphan branch `bot-state`, checked out into `./state` by the workflow:
  `games.json` (game records and mentions, last 30 days), `seen.json` (source IDs, 14 days), `posted.json` (message ID ↔ game, scores at post time), `baselines.json`, `weights.json`, `labels.jsonl`, `meta.json` (last roundup time, source health, daily alarm count).
- Prune on every run. Keep the whole thing under about 5 MB.
- At the end of the run: commit `state: run <timestamp>` and push. On a push conflict, pull, re-apply, and retry up to 3 times.
- The `--dry-run` flag uses a temp state directory and never pushes.

---

## 7. GitHub Actions

- **`scan.yml`:** `cron: "7,37 * * * *"` (off the top of the hour, because GitHub delays runs scheduled on the hour) plus `workflow_dispatch`. `concurrency: { group: gembot, cancel-in-progress: false }`, `permissions: contents: write`, `timeout-minutes: 8`. Cache pip. The roundup goes out inside the scan run when `now − meta.last_roundup ≥ 115 min`, so there's only one writer to state.
- **`setup.yml`:** `workflow_dispatch` only.
- **`ci.yml`:** on push and pull request: ruff, pytest with coverage ≥ 85% on `gembot/`.
- **`smoke.yml`:** `workflow_dispatch`. Hits every real source live, prints a summary table (requests, mentions, failures, top 10 scored games) to the job summary, never posts to Discord unless the input `post_test=true`, and never writes state.
- **Cost note for the README:** public repos get standard Actions minutes free. In a private repo, 48 runs a day can exceed the free monthly minutes. Measure the real run time in smoke tests and put the numbers in the README.
- **Keep-alive:** GitHub can disable scheduled workflows in inactive repos. Confirm the state commits prevent that. If they don't, add a safeguard and document it.

---

## 8. Testing (in depth — required)

Record **real** responses from each source into `tests/fixtures/` during development (scrub usernames down to fake ones where it's cheap to do). Use `respx` so no test hits the network.

**Collectors:** parse each fixture into the expected Mentions. Also test empty responses, malformed JSON/XML, HTTP 403/429/500 (the collector returns an empty list and records the error; the run continues), and that the request budget is enforced.

**Game resolution:**
- Steam / itch URL variants canonicalize to one ID.
- Two posts about "Gorilla Pizza Panic" on Reddit and Bluesky merge into one game; "Pizza Panic" from a different dev does not.
- Shortened links resolve within budget.

**Scoring scenarios** (build each from synthetic data):
1. A tiny dev's post at 10× subreddit normal with strong wishlist comments → **alarm**.
2. A big studio game with huge numbers → **excluded** (blocklist).
3. A single-source post with average engagement → roundup at most, never an alarm.
4. A post with 40 comments where 12 joke about it being a Roblox clone, plus decent velocity → meme ≥ 0.6, bonus applied, alarm reasons include the 🧱 line.
5. A Roblox-joke flood on a post with only 6 comments → meme not counted.
6. A game seen on 3 platforms but with low engagement everywhere → cross boosts it into the roundup, not an alarm.
7. Strong negativity (asset flip, scam) → penalty applied, no alarm.
8. Alarm caps: 5 qualifying games in one run → 3 alarms, 2 carried to the roundup as "would have alarmed."
9. Escalation from roundup to alarm.
10. A roundup with nothing ≥ 45 → no message posted.

**Feedback:** 👍 on high-hype games raises the hype weight within bounds; bot reactions are ignored; weights stay normalized.

**State:** round-trip, pruning, no double alarm across two consecutive simulated runs, push-conflict retry (simulate with a local bare git repo).

**Discord:** embed builders against golden files in `tests/golden/`, limit truncation, 429 retry, and setup is idempotent (running it twice creates no duplicate channels).

**End to end:** `python -m gembot replay tests/fixtures/scenario_day/` runs the full pipeline offline over a recorded "day" of data and produces exactly the expected set of alarm and roundup payloads (golden).

**Live checks (you, during the build):** run `python -m gembot smoke` from your environment where the network allows it, and record what worked in VERIFICATION.md. If your sandbox can't reach a source, say so explicitly and rely on the `smoke.yml` workflow for that source.

---

## 9. Verify these claims (they may be wrong or out of date)
- [ ] Steam tag IDs (492, 1685, 3843, 3859), the search JSON parameters, appdetails category IDs for co-op, and whether community hub member counts are fetchable.
- [ ] itch.io `.xml` browse feeds and their exact URLs.
- [ ] Reddit: whether new OAuth apps can still be self-created, and whether unauthenticated `.json`/`.rss` work from GitHub runners.
- [ ] Bluesky: whether `searchPosts` needs authentication.
- [ ] Discord: whether a new bot needs one gateway connection before REST messages work; current embed limits.
- [ ] GitHub Actions: cron delay behavior, the inactivity auto-disable rule, and free-minute limits for public and private repos.
- [ ] RSS.app (or an alternative) for Instagram and TikTok feeds: does it work, and what does the free tier allow? Recommend options in the README.

---

## 10. Build phases and definition of done
1. **Skeleton:** repo layout, config loading, models, CLI, CI workflow, state store with tests.
2. **Collectors:** Steam, itch, Reddit, Bluesky, RSS, X (disabled), with fixtures and tests. Fill in VERIFICATION.md.
3. **Resolution + enrichment:** entity matching, comments, signal detectors (hype / negativity / roblox), optional LLM.
4. **Scoring + decisions:** features, weights, penalties, explanations, all 10 scenarios passing, SCORING.md.
5. **Discord:** REST client, embeds, setup flow, feedback loop, golden tests.
6. **Workflows:** scan / setup / smoke wired up. Run the replay end to end.
7. **README:** a click-by-click setup guide for a non-programmer (Discord portal → invite → secret → run Setup → phone notifications), how to add Instagram/TikTok feeds, how to tune `settings.yaml`, optional secrets, cost notes, and troubleshooting (the status channel, how to read a smoke run).

**Done means:** CI is green with ≥ 85% coverage, the replay produces the golden output, the smoke workflow runs, the README is complete, and VERIFICATION.md lists every claim from section 9 with what you found.
