# 💎 GemBot

GemBot is a Discord bot for a small indie-game news channel. It watches Steam, itch.io, Reddit,
Bluesky, YouTube, Instagram and TikTok for **upcoming small indie games**, especially
**"friendslop"**: cheap, chaotic co-op games you play with friends (think *Lethal Company*,
*Content Warning*, *R.E.P.O.*, *PEAK*). It also catches early breakout games and games that only
just started development.

It doesn't just forward posts. Every game gets a **Gem Score** (0–100) built from how fast people
are reacting compared to what's normal, how small the creator is, how many platforms it shows up
on, how co-op and chaotic it looks, whether commenters actually want it, and even whether people
are joking that it's a Roblox game (that's attention!). Then it:

- 🚨 pings **#gem-alarm** right away for the rare game that is clearly taking off (max 3 per run,
  12 per day);
- 📋 posts a **#gem-roundup** every ~2 hours with the best of the rest (only if something is good
  enough; no filler);
- 👍 / 👎 learns from your reactions and slowly re-weights what it pays attention to.

It runs **entirely on GitHub Actions** (free for public repositories). No server, no hosting.

- How the score works, in plain English: [`docs/SCORING.md`](docs/SCORING.md)
- What we checked about each source and what we found: [`docs/VERIFICATION.md`](docs/VERIFICATION.md)
- For developers: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)

---

## Contents

1. [Set it up (about 15 minutes)](#set-it-up-about-15-minutes)
2. [Make your phones ping](#make-your-phones-ping)
3. [Optional extras (Bluesky, Reddit, X, Claude, role ping)](#optional-extras)
4. [Add Instagram, TikTok and YouTube feeds](#add-instagram-tiktok-and-youtube-feeds)
5. [Tune it (`settings.yaml`)](#tune-it)
6. [What it costs](#what-it-costs)
7. [Troubleshooting](#troubleshooting)

---

## Set it up (about 15 minutes)

You need: a GitHub account, a Discord server where you have **Manage Server**, and this repository
(fork or copy it into your GitHub account; a **public** repository is recommended — see
[What it costs](#what-it-costs)).

### Step 1 — Create the bot in Discord

1. Go to <https://discord.com/developers/applications> and click **New Application**. Name it
   `GemBot`, accept the terms, click **Create**.
2. On **General Information**, copy the **Application ID** and keep it handy (you need it in a
   minute).
3. In the left sidebar click **Bot**.
   - Under **Token** click **Reset Token**, confirm (enter your 2FA code if asked) and click
     **Copy**. This is the bot's password: it is shown only once. Paste it somewhere safe for
     step 2.
   - Leave **Requires OAuth2 Code Grant** **off**.
   - Leave all three **Privileged Gateway Intents** **off** (GemBot doesn't need them).
4. *(Optional, to keep the bot private)* In **Installation** set **Install Link** to **None** and
   save, then in **Bot** switch **Public Bot** off. (Do it in that order or the portal shows an
   error.)
5. **Invite the bot to your server.** Open this link in your browser, after replacing
   `YOUR_APP_ID` with the Application ID from step 1.2:

   ```
   https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot&permissions=85072
   ```

   Pick your server → **Continue** → **Authorize** → solve the captcha.

   `85072` = View Channels + Send Messages + Embed Links + Add Reactions + Read Message History +
   Manage Channels. (Don't use Discord's default "Discord Provided Link": for new apps it doesn't
   add the bot user.)

### Step 2 — Give GitHub the bot token

1. In your GitHub repository go to **Settings → Secrets and variables → Actions**.
2. Click **New repository secret**.
3. Name: `DISCORD_BOT_TOKEN`. Secret: the token you copied in step 1.3. Click **Add secret**.

That's the only required secret.

### Step 3 — Run "Setup GemBot"

1. Open the **Actions** tab of your repository. If GitHub asks, click **I understand my workflows,
   go ahead and enable them**.
2. In the left list click **Setup GemBot** → **Run workflow** (right side) → **Run workflow**.
3. Wait about a minute for the green tick. In Discord you'll now see a **GemBot** category with:
   - `#gem-alarm` — the important stuff, with a test alarm marked **TEST**;
   - `#gem-roundup` — the 2-hourly digest;
   - `#gembot-status` — a welcome message, and later only messages about broken sources.

Setup is safe to run again: it never creates duplicate channels. If the bot is in more than one
server, the setup log tells you to add a second secret, `DISCORD_GUILD_ID` (see
[Optional extras](#optional-extras) for how to copy a server ID).

### Step 4 — Done

The **GemBot scan** workflow now runs by itself every 30 minutes. You can also start one by hand:
**Actions → GemBot scan → Run workflow**. The first run saves what it sees; alarms and roundups
start as soon as something scores high enough.

> **Expect a quiet start without the optional extras.** With only the Discord token, GemBot sees
> Steam and itch.io listings plus Reddit's RSS (no votes or comments, and only until 13 Nov 2026).
> Most of a Gem Score comes from people reacting (comments, engagement, several platforms), so
> listing-only games usually land below the roundup line of 45: on the first live smoke run the
> best one scored 39.8. Adding **Bluesky** (free, see [Optional extras](#optional-extras)) is the
> single biggest improvement.

> **Want to see it work before waiting?** Run **Actions → Smoke test → Run workflow**. It checks
> every source once and shows a table on the run page (see [Read a smoke run](#read-a-smoke-run)).
> Tick **post_test** to also send a TEST alarm to Discord.

---

## Make your phones ping

Do this on **both** of your phones so a 🚨 alarm reaches you instantly:

1. In the Discord app, **long-press `#gem-alarm`** (or open it and tap the channel name at the
   top) → **Notifications** (or **Notification Settings**) → choose **All Messages** (not "Use
   Server Default").
2. Make sure the server itself isn't muted: tap the server name → **Notifications** → not muted,
   and **Mobile Push Notifications** on.
3. App-wide: tap your avatar (**You**) → **Settings → Notifications** → allow notifications.
4. Phone-wide: allow notifications for Discord in your phone's settings (iOS: Settings → Discord →
   Notifications; Android: Settings → Apps → Discord → Notifications).

Tip: leave `#gem-roundup` on "Only @mentions" so only the alarms buzz. If Discord desktop is open
and active, mobile pushes can be delayed — that's a Discord setting (Desktop → Notifications →
Push Notification Inactive Timeout).

---

## Optional extras

Add any of these as repository secrets (**Settings → Secrets and variables → Actions → New
repository secret**). Everything works without them; each one turns on more.

| Secret | What it does | How to get it |
|---|---|---|
| `BLUESKY_HANDLE` + `BLUESKY_APP_PASSWORD` | **Turns on Bluesky.** Bluesky no longer allows searching without a login (checked Oct 2026). | Make a free Bluesky account for the bot (a separate one is best). Then **Settings → Privacy and security → App passwords → Add App Password**. Use the handle (e.g. `gembot.bsky.social`) and that app password — never your main password. GemBot logs in rarely and stores its session **encrypted** with a key derived from the app password, so the public state branch reveals nothing. |
| `REDDIT_CLIENT_ID` + `REDDIT_CLIENT_SECRET` (+ optional `REDDIT_USERNAME`) | Full Reddit data: upvotes, comment counts, top comments. | Only if you **already have** a Reddit "script" app (Reddit closed self-service API access in Nov 2025; new access needs an application at <https://support.reddithelp.com/hc/en-us/requests/new?ticket_form_id=14868593862164>, and Reddit says its public API ends around March 2027). **Without these**, GemBot reads Reddit's public RSS (one request per run, no vote counts) until Reddit switches RSS off on **13 Nov 2026**; after that Reddit is skipped and `#gembot-status` says so. |
| `X_BEARER_TOKEN` | Turns on X (Twitter). **Costs money.** | See [X (paid)](#x-paid) below. |
| `ANTHROPIC_API_KEY` | Uses Claude (the small Haiku model) to double-check that a post is about one specific game, rate its "friendslop" fit and write a one-line pitch. Max 40 short calls per run. | <https://console.anthropic.com> → API keys. Cost is tiny (well under a cent per run). |
| `ALARM_PING_ROLE_ID` | Alarms also @mention this role. | Turn on Developer Mode (phone: You → Settings → App Settings → Advanced → Developer Mode; desktop: User Settings → Advanced). Then Server Settings → Roles → long-press/right-click the role → **Copy Role ID**. In the role's settings turn on **Allow anyone to @mention this role**. |
| `DISCORD_GUILD_ID` | Needed only if the bot is in more than one server. | With Developer Mode on: long-press/right-click the server icon → **Copy Server ID**. |

`ALARM_PING_ROLE_ID` and `DISCORD_GUILD_ID` are just IDs, not passwords; they live with the
secrets only so everything is set up in one place.

### X (paid)

X stopped offering a free API tier for new developers in February 2026; it is now **pay-per-use**
(about **$0.005 per post** returned, checked Oct 2026). To turn it on:

1. Sign in at the X Developer Console (console.x.com), create a Project and an App, and put the app
   on the **Pay-per-use** plan in the **Production** environment.
2. Buy a little credit ($5–$10 is plenty to try) and **set a monthly spending limit** — that is
   your hard stop.
3. Copy the app's **Bearer Token** (Keys and tokens) into the `X_BEARER_TOKEN` secret.

GemBot asks for at most 25 new posts per run and only posts newer than the last one it saw.
Rough cost with 48 scans a day: about 5 new posts per run ≈ 7,200 posts ≈ **$36/month**; 25 every
run would be ≈ $180/month. That's why GemBot **pauses X for the rest of the month** once
`x.monthly_read_budget` in `config/sources.yaml` is reached (default 3,000 posts ≈ **$15/month**).
Smoke tests and dry runs read X too (up to 25 posts each) but can't record that in the budget,
so your spending limit on X is the real hard stop. To turn X off, delete the secret.

---

## Add Instagram, TikTok and YouTube feeds

GemBot can't read Instagram or TikTok directly. Instead you give it an **RSS feed** — a web
address that lists an account's newest posts — in `config/feeds.yaml`.

### Instagram or TikTok → RSS.app (about 2 minutes per account)

1. Open <https://rss.app> and **Sign up** (email or Google; no card needed).
2. In another tab open the public account you want to follow and copy its address, e.g.
   `https://www.instagram.com/somestudio/` or `https://www.tiktok.com/@somestudio`.
3. In RSS.app go to **My Feeds → New Feed** (top right), choose **Instagram** or **TikTok** (or use
   the general "RSS Feed Generator" box).
4. Paste the account address → **Generate** → wait for the preview → save the feed.
5. Copy the feed's **RSS URL**. It must look like `https://rss.app/feeds/AbCdEfGhIjKlMnOp.xml`
   (starts with `https://rss.app/feeds/`, ends in `.xml`). Not the `.json` link and not the page
   `https://rss.app/feed/…` without the "s" — GemBot warns you in the log if you paste one of those.
6. On GitHub open `config/feeds.yaml`, click the ✏️ pencil, and add an entry (keep the spaces
   exactly):

   ```yaml
   feeds:
     - name: "Some Studio (Instagram)"
       url: "https://rss.app/feeds/AbCdEfGhIjKlMnOp.xml"
       source: instagram        # or: tiktok
       audience: 25000          # their follower count, typed by you
   ```

   Click **Commit changes**. The next scan uses it.
7. **`audience`**: these feeds don't include follower numbers, so type the count you see on the
   profile (write `25000`, not `25K`). It powers the "small creator, big reaction" part of the
   score. Leave it out and that part of the score simply doesn't count for this feed (GemBot
   doesn't guess).

**RSS.app limits (checked Oct 2026; prices change, see rss.app/pricing):** the **Free plan** allows
**2 feeds**, refreshed **once every 24 hours**, showing the latest **5 posts**. Paid plans start
around $8/month (Basic: 15 feeds, hourly refresh). Instagram/TikTok feeds sometimes stall when
those sites change; if a feed shows nothing new for a week, refresh or recreate it in RSS.app.
Preview images from Instagram/TikTok expire after a few days, so old Discord posts may lose their
picture — that's normal.

**Alternatives** (if RSS.app doesn't work for you): a self-hosted
[RSS-Bridge](https://github.com/RSS-Bridge/rss-bridge) or [RSSHub](https://docs.rsshub.app)
(needs your own server; RSSHub's Instagram route needs your own Instagram login cookie). Their
public instances are unreliable for Instagram/TikTok. Zapier/IFTTT can't follow other people's
accounts.

### YouTube channel (free, no sign-up)

1. Open the channel on YouTube and find its **channel ID** (starts with `UC`, 24 characters):
   tap the channel description ("…more") → **Share channel → Copy channel ID**. (A handle like
   `@somestudio` is *not* the ID.)
2. Your feed URL is `https://www.youtube.com/feeds/videos.xml?channel_id=UC…your ID…`
3. Add it to `config/feeds.yaml` with `source: youtube`. YouTube feeds include view and like
   counts; `audience` (subscriber count) is optional.

### Check your feeds

After you commit `config/feeds.yaml`, open the **Actions** tab: the CI run's **"Check your
config/ folder"** step lists every feed and turns red with the exact line to fix if something
is off. The **Smoke test** summary has a **"Your feeds"** table with each feed's status and
item count. A mistake never stops the scans: the feeds that are fine keep working, and
`#gembot-status` reminds you after a few runs. The three common mistakes:

- **An extra or indented `feeds:` line** inside a feed (e.g. under `audience:`). Delete it:
  the file has exactly one `feeds:` line, at the very top.
- **A second `feeds:` block** further down (e.g. `feeds: [2]` at the end). Delete it and put
  every feed under the first `feeds:` line.
- **`UC` pasted twice** in a YouTube channel ID (`channel_id=UCUCxsk…`). The ID is 24
  characters and starts with a single `UC`.

---

## Tune it

Everything lives in plain text files in `config/`. Edit them on GitHub (✏️ → Commit); the next
scan picks the change up. [`docs/SCORING.md`](docs/SCORING.md) explains every number.

| I want… | Change in `config/settings.yaml` |
|---|---|
| fewer alarms | raise `decisions.alarm_score` (e.g. 72 → 78) or lower `max_alarms_per_day` |
| more alarms | lower `decisions.alarm_score` (e.g. 72 → 66) |
| more games in roundups | lower `decisions.roundup_score` (45 → 40) or raise `roundup_max_items` |
| roundups less often | raise `decisions.roundup_interval_minutes` (115 ≈ every 2 h; 235 ≈ every 4 h) |
| care more about co-op fit | raise `weights.fit` (the bot also learns this from your 👍/👎) |
| ignore a big publisher | add it to `config/blocklist.yaml` → `companies` |
| watch another subreddit / search term | `config/sources.yaml` → `reddit.subreddits` / `bluesky.terms` |
| new "friendslop" words | `config/sources.yaml` → `fit_keywords` |

The weights in `settings.yaml` are only starting points. Your 👍/👎 nudge them (a little each time)
and the learned values are kept in the state; once a week `#gembot-status` tells you what changed.

---

## What it costs

- **Public repository: $0.** GitHub Actions is free and unlimited on standard runners for public
  repos. Trade-off: the state branch (`bot-state`, lists of games and scores) is publicly
  readable. No secrets are ever stored there (the Bluesky session is encrypted).
- **Private repository:** GitHub Free includes **2,000 minutes/month** (Pro/Team: 3,000). Every
  job is **rounded up to a whole minute**, and the scan runs 48 times a day ≈ **1,440–1,488
  jobs/month**:
  - if a scan takes under 60 seconds, that's ~1,440–1,488 minutes — it fits, with ~500 left for
    CI and smoke tests;
  - if a scan takes 61–120 seconds, it's ~2,900 minutes — the free quota runs out around day 21
    (with a payment method, the extra ≈ 900 min × $0.006 ≈ **$5–6/month**; without one, Actions
    pauses until the 1st).
  - **Run time (measured on the first live runs, 4 Oct 2026):** **105–135 seconds** of
    scanning, nearly all of it Steam's polite 2-second pause between requests (up to 60 Steam
    requests a run: ~60 s of listings, then store details for the shortlist), **plus ~15
    seconds** of job setup. A scan job takes 2–2.5 minutes and bills **2–3 minutes**, which
    does *not* fit in 2,000 free minutes at 48 runs a day. On a private repo, either scan every
    2 hours (change the `cron` line in `.github/workflows/scan.yml` to `"7 */2 * * *"`: ~370
    runs ≈ 1,100 minutes/month), or lower `budgets.steam` in `config/settings.yaml` (each 10
    fewer Steam requests saves ~20 s) until jobs stay under 2 minutes and scan hourly
    (`"7 * * * *"`: ~1,490 minutes). Check yours: open any **GemBot scan** run (the duration is
    shown at the top) or run the **Smoke test**, whose summary prints the time. Hard limits:
    sources stop after 3 minutes, enrichment after 5, and the job is cancelled at 8.
- Optional paid extras: X (see [above](#x-paid)), Claude (tiny), RSS.app beyond 2 feeds.

---

## Troubleshooting

### The `#gembot-status` channel

GemBot stays quiet there unless something changes:

- ⚠️ **"<source> has failed 6 runs in a row"** — that source is down or blocking GitHub's servers.
  The rest keeps working. It says ✅ when the source recovers.
- ⏰ **"Nobody has pushed to the main branch for 45 days"** — GitHub switches off scheduled
  workflows in public repos after 60 days without activity (the bot's own state commits may not
  count). Push any small change (e.g. edit this README on GitHub). If scans already stopped:
  **Actions → GemBot scan → Enable workflow**.
- 📊 the weekly note about how your 👍/👎 changed the weights.

### Read a smoke run

**Actions → Smoke test → Run workflow**, then open the run. The summary shows one row per source:

| Status | Meaning |
|---|---|
| ✅ ok | worked |
| ⚠️ partial | some requests failed (e.g. one feed), the rest worked |
| ❌ failed | nothing worked — the note says why (blocked, rate-limited, bad secret, …) |
| ⏭️ skipped | turned off or missing its secret (e.g. X without `X_BEARER_TOKEN`) — not an error |

Below it: the top 10 games it would score right now, what it *would* have posted (nothing is
posted unless you ticked `post_test`), and the run time. A smoke run never changes the bot's state.

### Common problems

| Problem | Fix |
|---|---|
| Setup fails: "DISCORD_BOT_TOKEN is missing" | Add the secret (step 2). Secret names are case-sensitive. |
| Setup fails: "rejected the bot token" | The token changed (it changes every time you click Reset Token). Copy a fresh one into the secret. |
| Setup fails: "The bot is in N servers" | Add the `DISCORD_GUILD_ID` secret. |
| "Missing Permissions" / "Missing Access" | Re-invite the bot with the link in step 1.5, or give the bot's role those permissions on the GemBot category. |
| Nothing posts for a long time | That can be normal (no filler!). Run the Smoke test to see current scores, or lower `decisions.roundup_score`. |
| Scans run hours late or skip | GitHub's scheduler has been unreliable since late Aug 2026 ("delayed during periods of high load"). Optional fix: a free external scheduler (e.g. cron-job.org) that calls `POST https://api.github.com/repos/<you>/<repo>/actions/workflows/scan.yml/dispatches` with body `{"ref":"main"}` and a fine-grained token (this repo only, *Actions: write*). |
| Reddit shows "RSS retired" | Reddit turned off RSS on 13 Nov 2026. Add Reddit API credentials if you have them, or ignore it — the other sources keep working. |
| Bluesky "skipped" | Add `BLUESKY_HANDLE` + `BLUESKY_APP_PASSWORD`. |
| "Discord/Cloudflare temporarily blocked this runner's IP" | GitHub's machines are shared; Discord sometimes blocks one for a while. Nothing to do — the next run retries. |

### For developers

```bash
python -m pip install -e ".[dev]"
ruff check . && ruff format --check .
pytest --cov=gembot                                  # unit + scenario tests (no network)
python -m gembot replay tests/fixtures/scenario_day/ --check   # a recorded day, end to end
python -m gembot run --dry-run                       # live sources, temp state, posts nothing
python -m gembot smoke                               # live source check + summary table
```

Fixtures in `tests/fixtures/` follow each source's documented 2026 response format (the build
machine couldn't reach the live sources); replace them with real captures from your first smoke
run when you can.
