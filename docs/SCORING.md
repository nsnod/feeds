# How GemBot scores games

This page explains, without code, how GemBot decides which games are worth your
attention. It is written for the people running the channel. Every number below comes
from `config/settings.yaml`, so you can change any of them (see
[Tuning](#tuning-settingsyaml) at the end).

---

## The short version

Every 30 minutes GemBot reads Reddit, Bluesky, Steam, itch.io and your RSS feeds, groups
the posts by game, and gives each game a **Gem Score from 0 to 100**.

* **72 or more** plus at least two "this is really happening" signals → **🚨 Gem Alarm**
  in `#gem-alarm`. It pings your phones right away.
* **45 or more** → the game goes into the next **📋 Gem Roundup** in `#gem-roundup`
  (about every 2 hours).
* **Below 45** → nothing is posted. The bot keeps watching the game.

Big studios are never posted. Every post comes with 2–4 plain-English reasons that quote
real numbers, so you can see why the bot got excited.

### Two passes per run (to stay fast and polite)

1. **Quick pass.** Every game with a new or changed post in this scan (new likes, new
   comments, a new rank), plus any alarm-worthy game from the last 48 hours that never got
   its alarm out (for example because Discord was down), gets a cheap score. It uses only
   what the bot already collected: how fast the posts are growing, the co-op keywords, and
   how new the game is. The best 40 go on to the next pass (`run.shortlist_size`).
2. **Careful pass.** For those 40, the bot reads up to 100 top comments per post, looks up
   the poster's follower count and the Steam page, then works out the full score.

---

## The seven ingredients ("features")

Each ingredient is a number from **0** (nothing there) to **1** (as strong as it gets).

### 1. Velocity: is it taking off *for where it was posted*?

A post with 300 upvotes is huge in r/SoloDevelopment and normal in r/gaming. So GemBot
compares each post with **that subreddit's (or platform's) normal**: the median
engagement per hour of its posts over the last 14 days.

* engagement per hour = (likes + comments + shares) ÷ hours since posting (posts younger
  than an hour count as an hour old, because the first few likes on a 10-minute-old post
  say very little; `features.velocity_min_age_hours`)
* multiple = engagement per hour ÷ the channel's normal
* **velocity = log₂(multiple) ÷ 4**, capped between 0 and 1

So 2× normal = 0.25, 4× = 0.5, 10× = 0.83, and **16× normal = 1.0**. Normal or below
gives 0. The game gets the velocity of its hottest post from the last 72 hours.

> **Example:** 140 upvotes + 40 comments in 3 hours = 60 per hour. r/SoloDevelopment's
> normal is 6 per hour, so that's 10× normal → log₂(10) = 3.32 → 3.32 ÷ 4 = **0.83**.

Until a channel has at least 8 posts of history (`features.baseline_min_samples`),
GemBot assumes a typical rate instead: Reddit 3/h, Bluesky 1/h, X 2/h, YouTube and
Instagram 5/h, TikTok 10/h, RSS 1/h (`features.default_baseline_eph`).

**itch.io** has no likes in its feeds, so velocity there comes from the
**New & Popular** list:

* rank score = 1 − (rank − 1) ÷ list length (#1 = 1.0; #16 of 30 = 0.5)
* staying power = hours on the list ÷ 24, capped at 1 (`features.itch_list_full_hours`)
* **velocity = 0.6 × rank score + 0.4 × staying power**

> **Example:** #3 of 30 for 9 hours → 0.6 × 0.93 + 0.4 × 0.375 = **0.71**.

Only the main New & Popular feed counts. If the game hasn't been seen on the list for 6
hours, it has dropped off and gets no itch velocity.

**Steam** pages have no likes either. By default, velocity there comes from the game's
**rank on Steam's own "popular upcoming" list** (Steam ranks it by wishlists and follows).
Every Steam search in `sources.yaml` with a `popular…` filter counts as that list, and
the formula is the same as itch.io's above: 0.6 × rank score + 0.4 × staying power, and
nothing once the game has been off the list for 6 hours. A plain "coming soon" search
result has no rank velocity.

> **Example:** #2 of 50 on the popular upcoming list for 30 hours →
> 0.6 × 0.98 + 0.4 × 1 = **0.99**.

Follower growth is used only when follower tracking is switched on
(`steam.track_followers` in `sources.yaml`, off by default because Steam has no keyless
follower count): velocity is then the follower growth per day: **+50% followers per day =
1.0**, +10% per day = 0.2. Growth is measured against at least 100 followers, so 2 → 8
followers isn't "+300%". Gaps shorter than 12 hours are treated as 12 hours, so a lucky
half-hour doesn't count as a trend.

### 2. Underdog: a big reaction for a small creator

* **underdog = log₁₀(1 + 1000 × engagement ÷ audience) ÷ 3**, capped at 1
* audience = the poster's followers (Bluesky, X, feeds) or the subreddit's member count
  (Reddit). Anything under 100 counts as 100.
* The bot uses the most-engaged post whose audience it knows. If no audience is known
  anywhere, this is 0.

> **Example:** 480 likes for an account with 2,100 followers → 1000 × 480 ÷ 2,100 = 229
> → log₁₀(230) = 2.36 → ÷ 3 = **0.79**.

### 3. Cross-platform: is it showing up in several places?

This counts the different platforms (Reddit, Bluesky, Steam, itch.io, X, YouTube, …)
that mentioned the game, or that GemBot first noticed it on, in the last 72 hours:

| platforms | 1 | 2 | 3 | 4 or more |
|---|---|---|---|---|
| **cross** | 0 | 0.6 | 0.85 | 1.0 |

Several posts on one platform still count as one platform.

### 4. Fit: does it look like a friendslop / co-op game?

GemBot takes the best of three clues:

* **Steam categories:** Online Co-op 0.8, Co-op 0.7, Shared/Split-Screen Co-op or LAN
  Co-op 0.6. Plain multiplayer (Multi-player, Cross-Platform Multiplayer, PvP) 0.5.
* **Keywords** from `fit_keywords` in `sources.yaml`, such as "proximity chat" 1.0,
  "co-op horror" 0.9, "ragdoll" 0.7, "me and the boys" 0.9, "like Lethal Company" 1.0.
  The bot searches titles, post text, tags and the Steam description. Matching ignores
  capitals and whole words only count ("coop" doesn't match "Cooper"). Several medium
  hits add up: **fit = 1 − (1 − a)(1 − b)(1 − c)…**. "silly" 0.3 + "multiplayer" 0.4 +
  "funny" 0.3 gives 1 − 0.7 × 0.6 × 0.7 = **0.71**. A longer phrase uses up its words,
  so "co-op horror" doesn't also count as "co-op".
* **The optional AI check** (only if you added `ANTHROPIC_API_KEY`) rates the game 0–1.
  It is blended in: fit = the larger of (half keywords + half AI) and (0.8 × AI). The AI
  can raise a game with no keywords to 0.8, or pull a keyword-stuffed post down to half.

### 5. Hype: do people actually want to play it?

From the sampled comments, GemBot counts the **different people** who say things like
"wishlisted", "take my money", "day one", "when does it come out", "playtest?", "me and
the boys" or "my friends would love this".

* intent rate = those people ÷ everyone who commented
* confidence = everyone who commented ÷ 10, capped at 1 (one "wishlisted!" out of two
  replies is weaker evidence than 10 out of 20; `features.hype_confident_commenters`)
* volume = log(1 + those people) ÷ log(16), capped at 1 (15 people = full marks;
  `features.hype_full_intent_commenters`)
* **hype = ½ × min(1, 2 × intent rate) × confidence + ½ × volume − share of negative
  commenters**

> **Example:** 30 commenters, 15 of them want it, 1 calls it a scam → ½ × 1 × 1 + ½ × 1 −
> 1/30 = **0.97** (without the scam comment it would be 1.00). Two replies, one of them
> "wishlisted!" → ½ × 1 × 0.2 + ½ × 0.25 = **0.23**.

Without comments, hype is 0.

### 6. Meme: the "it's a Roblox game" jokes

GemBot counts the **different people** joking that the game is a Roblox game or clone
("roblox", "looks like roblox", "this is a roblox game", "fortnite creative map").

* **meme = jokers ÷ 5**, capped at 1 (`features.meme_divisor`)
* Jokes only count when **the post** they were made under has **at least 10 comments**
  (`features.meme_min_comments`). Each post is checked on its own, and the jokers of all
  posts that pass are added up.

People are counted, not comments, so one person spamming "roblox" can't fake it. The
10-comment minimum means five friends joking under a 6-comment post scores 0, even when
another post about the same game is busy.

### 7. Fresh: how new is it?

* **1.0** if GemBot first saw the game less than 48 hours ago, then sliding down to
  **0 at 14 days**. Halfway, at 8 days, it is 0.5.
* **+0.2** (capped at 1) when the Steam page says *coming soon* or has a future release
  date.

---

## Adding it up: the Gem Score

Each ingredient has a **weight** (how much it matters). The starting weights are:

| velocity | underdog | cross | fit | hype | meme | fresh |
|---|---|---|---|---|---|---|
| 0.25 | 0.15 | 0.15 | 0.15 | 0.15 | 0.05 | 0.10 |

**Gem Score = 100 × (sum of weight × ingredient) ÷ (sum of weights)**, then the bonuses
and penalties below, then capped between 0 and 100.

> **Worked example: "Gorilla Pizza Panic".** A solo dev posts the game's new Steam page
> on r/SoloDevelopment (18,000 members). Within 3 hours it has 140 upvotes and 40
> comments; 15 of the 30 commenters say they wishlisted it and 1 calls it a scam. GemBot
> also found the new Steam page (Online Co-op, coming soon) by itself 5 hours ago.
>
> | ingredient | value | × weight | = points |
> |---|---|---|---|
> | velocity (10× normal) | 0.83 | 0.25 | 20.8 |
> | underdog (180 reactions, 18,000 members) | 0.35 | 0.15 | 5.2 |
> | cross (Reddit + Steam) | 0.60 | 0.15 | 9.0 |
> | fit (Online Co-op, "proximity chat", "ragdoll") | 1.00 | 0.15 | 15.0 |
> | hype (15 of 30 want it, 1 calls it a scam) | 0.97 | 0.15 | 14.5 |
> | meme | 0 | 0.05 | 0 |
> | fresh (5 hours old, coming soon) | 1.00 | 0.10 | 10.0 |
> | **Gem Score** | | | **74.5** |
>
> That is 72 or more, with three strong signals (velocity, hype, cross), so it's a 🚨
> **Gem Alarm**.

### Never posted: big studios and banned words

* If the developer or publisher matches a company in `config/blocklist.yaml`, the game
  is **excluded** (score 0, never posted). Capitals and punctuation don't matter, and
  longer names that start with the company count too: "Ubisoft Montreal" matches
  "Ubisoft" and "Electronic Arts Inc." matches "Electronic Arts". A different name that
  merely starts with the same letters doesn't: "Blizzardo Games" is not "Blizzard".
* The names checked are the Steam page's developers and publishers, and the developer
  and publisher GemBot has on file for the game. When a "my game …" post is the only clue,
  GemBot guesses that the poster is the developer. Such a guess is a **username**, not a
  studio name, so it only counts when it is exactly the studio's name (an account called
  "Ubisoft"): a solo dev posting as u/Valve_Index_Fan is not Valve.
* If a banned keyword ("nft", "crypto", "play-to-earn", …) appears as a whole word in the
  game's name or in a post about it, the game is excluded.

### Penalties (points taken off)

| penalty | when | points |
|---|---|---|
| Negativity | more than **30%** of the sampled commenters call it an asset flip, scam, AI slop, stolen or abandoned. At least **3** people must have commented (`penalties.negativity_min_commenters`), so one grumpy reply out of two isn't "50% negative"; 3 of 4 is. | −15 |
| Old release | the Steam page shows an exact release date **more than 30 days ago**. Steam lists the Early Access launch date as the release date, so a **new** Early Access launch is never penalised; a game that went into Early Access months ago is. No exact date, no penalty. | −20 |
| Self-promo spammer | the **same person** posted the **same game more than 3 times in 7 days** on one platform. Store listings don't count. | −10 |

Penalties are never shown as reasons in Discord, but they are written to the run log
with the details (e.g. "12 of 30 commenters negative (asset flip, scam)").

### The 🧱 Roblox bonus: jokes are attention, not hate

When lots of people joke "this is just a Roblox game", lots of people are *looking at
it*. Games like that often blow up with streamers, which is exactly what you want to know
about. So GemBot treats the jokes as **attention**, not as negativity:

* **+8 points** when meme ≥ 0.6 (at least 3 different jokers under posts that have 10+
  comments each) **and** either velocity ≥ 0.4 **or** the game's discussion has 30+
  comments (all its posts together).
* When the bonus applies, the post **always** includes the line "🧱 Roblox-clone
  discourse: N commenters joking it's a Roblox game".

> **Example:** a post with 40 comments, 12 different people joking it's a Roblox clone,
> and 6.9× normal velocity scores 66.8 before the bonus and **74.8** with it, so it's an
> alarm.

---

## Alarm or roundup?

### 🚨 Gem Alarm (`#gem-alarm`)

A game alarms when **all** of these are true:

1. Gem Score **≥ 72** (`decisions.alarm_score`)
2. At least **2** of these 4 signals (`decisions.alarm_min_signals`):
   velocity ≥ 0.5 · hype ≥ 0.5 · cross ≥ 0.6 · meme ≥ 0.6. A good score alone isn't
   enough: something has to actually be happening.
3. The game hasn't alarmed before. Each game alarms at most once a year: the bot
   remembers an alarm for 365 days (`state.alarm_memory_days`).

**Caps against spam:** at most **3 alarms per run** and **12 per day** (the day resets at
midnight UTC). If more games qualify, the highest scores win. The extras go to the
**next roundup**, marked **"would have alarmed"**. They wait there and don't alarm 30
minutes later, because that would defeat the cap.

**📈 Escalated:** a game that was already in a roundup and later qualifies for an alarm
gets its alarm labelled "📈 Escalated". For example, it was 54 in this morning's roundup
and its post exploded this afternoon.

### 📋 Gem Roundup (`#gem-roundup`)

A roundup is due when the last one went out at least **115 minutes** ago (scans run every
30 minutes, so this means about every 2 hours). When it's due, the roundup takes:

1. First, any **"would have alarmed"** games waiting from an alarm cap.
2. Then the best games with Gem Score **≥ 45** that were scored in the last **48 hours**,
   **never alarmed**, and **never in a roundup**.
3. **📈 Heating up:** a game that was already in a roundup can come back, but only if its
   score went up by **15 or more** since then (e.g. 50 → 66).

At most **8** games per roundup, highest score first. **If nothing qualifies, nothing is
posted.** The roundup clock still resets, so the bot checks again 2 hours later instead of
every 30 minutes.

### Why each roundup game gets its own message

A roundup is posted as **one short header message** ("📋 Gem Roundup — 14:00") followed by
**one short message per game**. It is not one big message with 10 embeds.

The reason is the 👍/👎 learning. Discord reactions belong to a **whole message**. If 8
games shared one message, a 👍 couldn't say *which* game you liked, and the bot would
have to give the same thumbs-up to all 8. That would teach it nothing, or teach it the
wrong thing. With one message per game, every 👍/👎 is a clean label for exactly one
game.

The cost is a few more messages in `#gem-roundup` (up to 9 every 2 hours instead of 1).
That channel isn't meant to ping anyone, so mute it or set it to "Only @mentions" and
read it when you like. `#gem-alarm` is the one that should ping your phones.

---

## The "why" lines

Every alarm and roundup entry shows 2–4 reasons, picked from the ingredients that added
the most points (weight × ingredient), each with real numbers:

* "140 upvotes and 40 comments in 3h on r/SoloDevelopment (10× normal for that sub)"
* "Seen on Reddit, Bluesky and Steam in the last 24h"
* "15 different people said they wishlisted or want to play with friends"
* "🧱 Roblox-clone discourse: 12 commenters joking it's a Roblox game"
* "#3 on itch.io New & Popular for 9h"
* "#2 on Steam's popular upcoming list for 30h"
* "Small creator: 2,100 followers, 480 likes"
* "Big reaction for a small sub: 300 upvotes in r/CoOpGaming (8,400 members)"
* "Friendslop fit: proximity chat, co-op horror"
* "Steam page: Online Co-op, coming soon"
* "New Steam page, first seen 5h ago"

Reddit counts *upvotes* and *comments*; Bluesky and X count *likes*, *replies* and
*reposts*. The underdog line always quotes the post that earned the underdog points,
which is often not the fastest-growing post.

**Always at least 2.** When only one ingredient has something to say, the bot adds a
plainer second line, in this order: a general line for a strong ingredient that had no
numbers to quote ("Big reaction for the size of its audience"), facts from the Steam page
("Steam page: Action, Indie, $4.99"), when GemBot first spotted the game ("First spotted 9
days ago") and where it was seen ("Spotted on r/IndieDev"). Only a game with nothing at
all to say gets a single line.

---

## Learning from your 👍 and 👎

The bot adds 👍 and 👎 under every alarm and every roundup game. Once per run it reads the
reactions from **people** on posts up to 7 days old. The bot's own reactions are ignored.

* **Label** = (number of 👍 − number of 👎), capped between −1 and +1.
* For each ingredient, the bot compares the game's value with the average of its seven
  values. A 👍 on a game where that ingredient was **above average** nudges its weight
  **up**; one where it was **below average** nudges it **down**. A 👎 does the opposite.
* The nudge is small (5% at most per post, `feedback.learning_rate`):
  **new weight = old weight × (1 + 0.05 × label × (ingredient − average))**.
* Each weight always stays between **0.02** and **0.5** (`feedback.weight_min` /
  `weight_max`), so the bot can never ignore an ingredient completely or rely on just
  one. Afterwards the weights are rescaled to add up to 1 again. If one has hit a limit,
  the rescaling keeps it inside the limits.
* Every reaction is saved (`labels.jsonl` on the `bot-state` branch), so the weights can
  be recalculated from history later.

**Weekly note:** once a week the bot posts one sentence in `#gembot-status` about what
changed, for example:

> You've been 👍-ing games with strong comment hype; hype weight went from 0.15 → 0.19

If nothing moved by at least 0.005, it stays quiet.

---

## Tuning `settings.yaml`

Edit `config/settings.yaml` on GitHub (the pencil icon), commit, and the next scan uses
the new numbers. Change one or two knobs at a time and watch for a few days.

| you want… | change | for example |
|---|---|---|
| **More alarms** | lower `decisions.alarm_score` | 72 → 68 |
| | or let one signal be enough: `decisions.alarm_min_signals` | 2 → 1 (much noisier!) |
| | or raise the caps `max_alarms_per_run` / `max_alarms_per_day` | 3 → 4 / 12 → 16 |
| **Fewer alarms** | raise `decisions.alarm_score` | 72 → 78 |
| | or lower `decisions.max_alarms_per_day` | 12 → 6 |
| | or raise a signal bar in `alarm_signal_thresholds` | `velocity: 0.5` → `0.6` |
| **More roundup entries** | lower `decisions.roundup_score` | 45 → 40 |
| | or allow more per roundup: `decisions.roundup_max_items` | 8 → 10 |
| | or let games come back sooner: `decisions.heating_up_delta` | 15 → 10 |
| **Fewer / less frequent roundups** | raise `decisions.roundup_score` | 45 → 50 |
| | or post less often: `decisions.roundup_interval_minutes` | 115 → 235 (≈ every 4 h) |
| **Velocity reacts sooner** | lower `features.velocity_log2_divisor` | 4 → 3 (8× normal = full marks) |
| **Care less about Roblox jokes** | lower `bonuses.roblox_points` | 8 → 4 |
| **Softer on negativity** | raise `penalties.negativity_threshold` | 0.30 → 0.40 |
| | or ask for more commenters first: `penalties.negativity_min_commenters` | 3 → 5 |
| **New co-op words** | add them to `fit_keywords` in `sources.yaml` | `"push to talk": 0.6` |
| **Hide a studio** | add it to `companies` in `blocklist.yaml` | `- Some Big Publisher` |

**About the weights:** the `weights:` block in `settings.yaml` is only the **starting
point**. Once you start reacting, the bot keeps its learned weights on the `bot-state`
branch (`weights.json`), and editing `settings.yaml` won't change them. To start over
from your new `settings.yaml` weights, delete `weights.json` from the `bot-state`
branch. The bot falls back to the defaults on its next run.
