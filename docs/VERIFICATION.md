# Verification log

`BUILD_SPEC.md` section 9 lists claims that "may be wrong or out of date". This file records what we
found for each one, how we found it, and what GemBot does about it.

**How this was checked (2026-10-04).** The build ran in a sandbox whose network policy blocks every
source GemBot talks to (Steam, itch.io, Reddit, Bluesky, Discord, X, YouTube, RSS.app). `curl` and
web fetches to those hosts returned `403` from the sandbox proxy, so **no live request to any source
was possible from the build machine** and `python -m gembot smoke` could not be run there. Instead,
each claim was researched against the most recent primary material that *was* reachable, then
re-checked by an independent "skeptic" pass that tried to refute it:

- source code of the official clients published on npm / PyPI in Sept–Oct 2026 (`@atproto/api`,
  `@atproto/bsky`, `@atproto/pds`, `discord-api-types`, `@discordjs/rest`, `discord.py`, X's
  generated `xdk` SDKs, `tweepy`, `feedparser`), read as text;
- GitHub issues / pull requests / commits on github.com dated 2025–2026 that quote the official docs
  or report live behaviour (Discord's `discord-api-docs` repo, Bluesky's `atproto` repo, GitHub
  community discussions, many third-party projects hitting the same APIs);
- real captured fixtures shipped inside other packages (e.g. an itch.io feed captured in 2026);
- web search (until the session's search budget ran out).

Everything marked **"smoke"** below is re-checked live by the `smoke.yml` workflow on a GitHub
runner (see README → "Read a smoke run"). The test fixtures in `tests/fixtures/` follow the
documented 2026 response shapes; each fixture folder has a README saying so, and they should be
replaced with real captures from the first smoke run.

Legend: ✅ confirmed · ✏️ corrected (the spec was wrong or outdated) · ⚠️ partly / with caveats ·
❓ unverifiable from the sandbox (left to the smoke run).

---

## 1. Steam

| Claim | Result | What we found | What GemBot does |
|---|---|---|---|
| Tag IDs Indie 492, Co-op 1685, Online Co-Op 3843, Multiplayer 3859 | ✅ | All four are correct (Steam's own `tagdata/populartags/english` list; unchanged by Valve's May 2026 tag overhaul). Other useful IDs: Local Co-Op 3841, Horror 1667, Survival Horror 3978, Physics 3968, Funny 4136, Party Game 7178, Massively Multiplayer 128. There is **no** "Proximity chat" tag. | Searches use these IDs (`config/sources.yaml`); MMOs excluded with `untags=128`; proximity chat detected from description text. |
| Store search JSON (`search/results/?json=1` or `infinite=1`) | ✏️ | `json=1` alone returns only `{name, logo}` items (no appid, no paging) — not usable. `infinite=1` returns `{success, results_html, total_count, start}`; app IDs are read from `data-ds-appid` on `a.search_result_row` rows (bundle rows carry several ids and are skipped). `tags=` is AND-combined. Sorting by release date does **not** surface newly created pages (Steam sorts "Q1 2027" as Mar 31 2027), so "new" means "appid never seen before". | Uses `infinite=1`, parses rows with selectolax, rotates through result pages with a cursor kept in state, and always reads page 1 of "popular upcoming" for a rank signal. |
| `api/featuredcategories` | ✅ | Has `coming_soon`, `new_releases`, `top_sellers`, `specials` blocks with `items[].id/name/header_image/...`. Short and curated (includes demos). | Used as a secondary seed. |
| `api/appdetails?appids=` | ✏️ | One appid per request (multi-id only works with `filters=price_overview`). **Since ~2026-09-25 the response is often keyed by a different id** (e.g. 730 → `"2678630"`); confirmed by 5+ independent projects. `genres[].id` is a string, `categories[].id` an int. Limit ≈ 200 requests / 5 min per IP; a burst past it turns 429s into 403 blocks. | Picks the entry whose `data.steam_appid` equals the requested id (never "the only entry"); ≤ 25 new apps per run; ~2 s between Steam requests; stops Steam for the run on the first 429/403. |
| Co-op category IDs | ✅ | 1 Multi-player, 9 Co-op, 38 Online Co-op, 39 Shared/Split Screen Co-op, 48 LAN Co-op, 24 Shared/Split Screen, 27 Cross-Platform Multiplayer, 36 Online PvP, 49 PvP, 20 MMO. | `coop_category_ids` / `multiplayer_category_ids` in `sources.yaml` feed the "fit" score. |
| Community hub follower counts | ✏️ (dropped) | No official keyless API. Follower counts are only visible to the developer; the old `memberslistxml` endpoint is undocumented/legacy and its 2026 status is unknown; `GetNumberOfCurrentPlayers` is current players (404 for unreleased games). | **Dropped for v1** (`track_followers: false`). Steam velocity instead uses the game's rank on Steam's own "popular upcoming" list (driven by wishlists/follows). |
| GitHub runner IPs blocked by Steam | ✅ smoke | No reports of outright blocking; projects run appdetails from Actions and only see throttling. **First smoke run (2026-10-04):** 45 requests from a GitHub runner, all answered, 190 listings collected. | Polite pacing + per-run budget. |

## 2. itch.io

| Claim | Result | What we found | What GemBot does |
|---|---|---|---|
| Adding `.xml` to a browse URL gives an RSS feed | ✅ | Still true in 2026 (itch.io's founder pointed an app at `/games/tag-…xml` on 2026-09-24; several 2026 tools rely on it). | `https://itch.io/games/new-and-popular.xml`, `/games/newest.xml`, `/games/new-and-popular/tag-<slug>.xml`. |
| Exact feed URLs / tag slugs | ✅ smoke | **First smoke run (2026-10-04):** all 8 feed requests returned `200` RSS (~27 KB each), 262 games. Slugs: `multiplayer`, `co-op` (not `coop`), `local-multiplayer`, `horror`, `physics`. Feeds hold **36 items per page** (`?page=N`). Items carry itch-specific `<plainTitle>`, `<imageurl>`, `<price>`, `<createDate>`, `<platforms>`; `<title>` is decorated (`Name [$4.99] [Action]`). `pubDate` equals the project's *creation* date, not its release. | Uses `plainTitle`, rank = position in the feed, "new" = first time we see the game. |
| Feeds reachable from cloud runners | ⚠️ smoke | **First smoke run (2026-10-04):** every feed, including the combined `new-and-popular/tag-*.xml` ones, returned `200` through Cloudflare (`cf-cache-status: DYNAMIC`) with no challenge; one run proves little, so the fallback stays. Since a DDoS in Oct 2025 itch.io sits behind Cloudflare; combined sort+tag pages (`newest/tag-X`) have shown challenge loops (itch.io issue #1866). Single-facet feeds were explicitly un-challenged in Sept 2026. | Detects Cloudflare challenges, falls back per feed, and stops itch for the run after 2 challenges. |
| Engagement numbers in feeds | ✅ (none) | No ratings/views/downloads in feeds. | itch velocity = rank on New & Popular + hours on the list, as the spec says. |

## 3. Reddit

| Claim | Result | What we found | What GemBot does |
|---|---|---|---|
| New OAuth "script" apps can be self-created | ✏️ | **No.** Reddit's *Responsible Builder Policy* (2025-11-11) closed self-service API access; you now file a Data API access request that a person reviews (weeks; hobby projects are often refused). On 2026-09-30 Reddit announced the public Data API ends ~**March 2027**. | OAuth (`client_credentials`, one multireddit request per listing) is used **if** `REDDIT_CLIENT_ID`/`REDDIT_CLIENT_SECRET` exist (e.g. an app created before Nov 2025). |
| Unauthenticated `.json` works from GitHub runners | ✏️ | **No.** Since 2026-05-29 anonymous `.json` (www and old.reddit) returns `403 "You've been blocked by network security"` from *all* networks, not just datacenters. | Not used by default (`try_public_json: false`). |
| Unauthenticated `.rss` works | ⚠️ | **First smoke run (2026-10-04):** the one combined request from a GitHub runner returned 100 posts (no 429). Works, but ≈ 1 request/minute per IP (shared runner IPs often get 429) and **Reddit retires RSS on 2026-11-13**. RSS has no scores or comment counts. | Without credentials GemBot makes **one** combined RSS request per run until 2026-11-13, then skips Reddit with a clear status message. RSS posts still count for cross-platform/fit/freshness, not velocity. |
| Per-subreddit baselines | ✅ | Needs score + comment counts → OAuth mode only. | Baselines are built automatically from what is collected (14-day median per channel). |

## 4. Bluesky

| Claim | Result | What we found | What GemBot does |
|---|---|---|---|
| `app.bsky.feed.searchPosts` needs authentication | ✅ (yes) | Confirmed live from a GitHub runner on 2026-10-04 (`403`, HTML page from the CDN). Unauthenticated search is refused (`403`, HTML from the CDN) on both `api.bsky.app` and `public.api.bsky.app` (Aug–Oct 2026, incl. from GitHub Actions). Bluesky staff: "Bluesky mostly no longer allows public post search". With a login, search through the account's PDS works. | Needs `BLUESKY_HANDLE` + `BLUESKY_APP_PASSWORD`. Without them GemBot probes public search at most once a day and otherwise skips Bluesky with a warning. |
| Logging in every run is fine | ✏️ | bsky.social limits new logins to **~10 per account per day** (observed `ratelimit-policy: 10;w=86400`); a 30-minute schedule would lock itself out. Access tokens last 2 h; refresh tokens 90 days and rotate. | Keeps the session between runs and refreshes it; the refresh token is stored **encrypted** in the state branch (key derived from the app password, so a public state branch reveals nothing), plus a hard cap of 8 new logins/day. |
| Threads and profiles | ✅ | `getPostThread` and `getProfile(s)` still work without auth on `public.api.bsky.app` (never send tokens there). | Used for comment sampling and follower counts. |
| A wrong handle and a wrong password can be told apart | ✅ smoke | `createSession` answers the same `401 AuthenticationRequired` for both (seen live on 2026-10-04). Per the lexicon, `com.atproto.identity.resolveHandle` on the public AppView answers `200 {did}` for an existing handle and `400 InvalidRequest` ("Unable to resolve handle") otherwise, without a login. **Live smoke run on 2026-10-04 09:06 UTC ([run 37191019488](https://github.com/nsnod/feeds/actions/runs/37191019488)):** `resolveHandle` on the public AppView gave a verdict without a login — the configured handle does not exist — so the endpoint works without auth. | After a rejected login, **one** logged-out `resolveHandle` request per set of credentials (no token, not a login attempt; repeated with the daily login retry only if it failed) decides whether the error points at `BLUESKY_HANDLE` or `BLUESKY_APP_PASSWORD`; any other answer keeps the general advice. An existing handle may still be someone else's, so that advice names both. A `BLUESKY_HANDLE` that may hold a password is never looked up, since the lookup is a GET and the value would sit in its URL: a bare value with the App Password shape, the password itself anywhere in it, the secrets swapped, or extra text after `.bsky.social` (the error then says so). A full handle with four-letter words between hyphens (`game-devs-team-blog.bsky.social`) is looked up as usual. New secrets that meet the daily login cap get "daily login limit reached" with the time of their first try, not the old secrets' diagnosis. |

## 5. Discord

| Claim | Result | What we found | What GemBot does |
|---|---|---|---|
| A new bot must connect to the gateway once before REST messages work | ✏️ | That rule was **removed** from Discord's docs (commit `5f4decb`, last mention removed in Aug 2021, PR #3531); HTTP-only bots post fine. | The Setup workflow still connects once (the spec asks for it and it is harmless), labelled "optional". |
| Embed limits | ✅ | Title 256, description 4096, 25 fields (more is now an error), field name 256 / value 1024, footer 2048, author 256, **6000 characters across all embeds in a message**, 10 embeds per message, content 2000 (not part of the 6000). | Enforced in `gembot/discord/embeds.py` (counted in UTF-16 units for safety). |
| 429 handling | ✅ | Body `{message, retry_after (float s), global}`; `Retry-After` header is a rounded integer. Too many 401/403/429s (10,000 per 10 min per IP) trigger a temporary Cloudflare ban (non-JSON body `error code: 1015`). | Waits `retry_after` (bounded), paces reactions ≥ 0.3 s apart, never loops on Cloudflare blocks. |
| Invite permissions | ✅ | View Channels 1024 + Send Messages 2048 + Embed Links 16384 + Add Reactions 64 + Read Message History 65536 + Manage Channels 16 = **85072**. New apps' default "Discord Provided Link" adds only `applications.commands`, not the bot user — use the `scope=bot` URL. | README gives the exact link. |
| Reading reactions | ✅ | `GET …/reactions/{emoji}` defaults to 25 users (we ask for 100); users have `"bot": true` only if they are bots; `count` includes the bot's own reaction. | Bot reactions are ignored; labels use humans only. |

## 6. GitHub Actions

| Claim | Result | What we found | What GemBot does |
|---|---|---|---|
| Cron delays at the top of the hour | ✅ + ⚠️ | Docs: "The schedule event can be delayed during periods of high loads … High load times include the start of every hour … some queued jobs may be dropped." **Since late Aug 2026 many users report scheduled runs hours late or missing**, even at off-peak minutes. | Cron every 10 minutes (`7,17,27,37,47,57 * * * *`); a gate step skips a scheduled run when the last scan (newest `bot-state` commit) is under 25 minutes old, so missed slots are covered by the next one. Live: on 2026-10-04 the `7,37` schedule fired only twice in ~11 hours. README documents an optional external trigger (e.g. cron-job.org calling `workflow_dispatch`) if runs go missing. |
| Inactivity auto-disable | ⚠️ | Docs: "In a public repository, scheduled workflows are automatically disabled when no repository activity has occurred in 60 days." What counts as activity is undocumented; there is **no evidence that bot commits to a non-default branch (`bot-state`) count**. A popular keep-alive action was taken down by GitHub for "bypassing the 60-day inactivity policy". | Safeguard: from day 45 without a commit to the default branch, GemBot posts a reminder in `#gembot-status` ("push any small commit or re-enable the workflow"). README explains how to re-enable (Actions → workflow → **Enable workflow**). |
| Free minutes | ✅ | Public repos: standard runners free and unlimited. Private: Free plan 2,000 min/month, Pro/Team 3,000; **each job is rounded up to a whole minute**; Linux $0.006/min since Jan 2026. 48 runs/day ≈ 1,440–1,488 jobs/month. | Single short job per scan; numbers in README. |
| Action versions | ✏️ | Current majors (Oct 2026): `actions/checkout@v7`, `actions/setup-python@v7` (Node 20 actions are deprecated). | Workflows use v7. |

## 7. Instagram / TikTok / YouTube feeds

| Claim | Result | What we found | What GemBot does |
|---|---|---|---|
| RSS.app makes feeds from Instagram and TikTok profiles | ⚠️ | Yes (vendor pages 2025–26). Feed URL `https://rss.app/feeds/<id>.xml` (the `/feed/<id>` page and the `.json` link are not RSS). Items have caption, link, image (signed, expiring), date — **no likes/views/followers**. | `feeds.yaml` slot; URL lint warns about the wrong link types; `audience` is typed by the user. |
| RSS.app free tier | ✏️ | There is a permanent **Free plan: 2 feeds, refreshed every 24 h, 5 posts per feed** (plus a 7-day Basic trial). Paid: Basic ≈ $8.32/mo (15 feeds, 60-min refresh), Developer ≈ $16.64/mo (100 feeds, 15 min). | README recommends RSS.app first, with these limits. |
| Alternatives | ⚠️ | Self-hosted RSS-Bridge or RSSHub can work (RSSHub's Instagram route needs your own login cookie since Sept 2026); their public instances are unreliable for Instagram/TikTok. Zapier/IFTTT only watch *your own* account. | Listed in README in that order. |
| YouTube channel feeds | ✅ | `https://www.youtube.com/feeds/videos.xml?channel_id=UC…` still works (occasional transient 404s); includes views and likes. | Parsed by the RSS collector (views → `extra`, likes → engagement). |

## 8. X (optional)

| Claim | Result | What we found | What GemBot does |
|---|---|---|---|
| X API v2 recent search with a bearer token | ✅ | `GET https://api.x.com/2/tweets/search/recent`. **No free tier for new developers since Feb 2026**; pay-per-use ≈ $0.005 per post returned (+ ≈ $0.010 per expanded author). Errors: 402 out of credits, 403 `client-not-enrolled` (app not in the Pay-per-use package). | Off unless `X_BEARER_TOKEN` is set; `since_id` paging; no `next_token` following; monthly read cap in `sources.yaml`. |

## 9. Optional Claude classifier

The spec's model `claude-haiku-4-5-20251001` is the dated ID of Claude Haiku 4.5, which supports
structured JSON output (`output_config.format`). GemBot calls the Messages API through its own
budgeted HTTP client (≤ 40 calls/run) and only when `ANTHROPIC_API_KEY` is set.

---

## Live results

| Date | Where | Result |
|---|---|---|
| 2026-10-04 | build sandbox | All sources blocked by the sandbox network policy (`403` from the egress proxy for store.steampowered.com, itch.io, www.reddit.com, public.api.bsky.app, api.bsky.app, discord.com, api.x.com, www.youtube.com, rss.app). Nothing could be checked live; see the tables above. |
| 2026-10-04 | GitHub Actions, first smoke run ([run 37182420260](https://github.com/nsnod/feeds/actions/runs/37182420260)), no optional secrets | **0 sources failed.** Steam ✅ 45 requests, 190 listings · Reddit (anonymous RSS) ✅ 1 request, 100 posts · itch.io ✅ 8 requests, 262 games, no Cloudflare challenge · Bluesky ⏭️ public search `403` (needs a login, as expected) · feeds and X ⏭️ not configured. 552 mentions, 40 games scored; best score **39.8** (Steam listing only), so nothing would have been posted (roundup line 45). Took **103.5 s** of scanning + ~15 s of job setup. |
| 2026-10-04 | GitHub Actions, first two scans ([run 37182868398](https://github.com/nsnod/feeds/actions/runs/37182868398), [run 37183555617](https://github.com/nsnod/feeds/actions/runs/37183555617)), no optional secrets | Both ✅: `init-state` created the orphan `bot-state` branch, state was committed and pushed (1.05 MB after two runs, no secrets in it). Second run: 555 mentions (84 new), one roundup-worthy game (**46.1**, #1 on itch.io New & Popular); Discord not set up yet, so nothing was posted. Steam used its whole budget of 60 requests (5 store-detail lookups deferred to the next run), so the scan step took **135 s**. Between the two runs, 430 of 503 stored mentions changed only their `observed_at` stamp; these "still there" stamps now move only every 2 h / 12 h. |
