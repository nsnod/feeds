# Reddit fixtures

reddit.com cannot be reached from the build sandbox, so these files follow the response
shapes that Reddit documents and that other projects have recorded (2025-2026). Usernames,
post IDs and game titles are made up. Timestamps sit around `tests.factories.NOW`
(2026-10-03 12:00 UTC).

| file | what it stands for |
|---|---|
| `token.json` | `POST https://www.reddit.com/api/v1/access_token` (client_credentials) |
| `listing_new.json` | `GET https://oauth.reddit.com/r/A+B+.../new?limit=..&raw_json=1`: a co-op announcement self post (Steam link in the text), a Steam link post, an itch.io link post, a help question, a stickied mod post, an NSFW post, a v.redd.it video and a 4-day-old post |
| `listing_rising.json` | `/rising` for the same multireddit: the announcement again (dedupe), a playtest call and a crosspost |
| `comments.json` | `GET https://oauth.reddit.com/comments/<id>?sort=top&depth=1&raw_json=1`: `[post listing, comment listing]` with an AutoModerator sticky, wishlist and Roblox-joke comments, `[deleted]`/`[removed]` and a `more` stub |
| `feed_new.rss` | anonymous `https://www.reddit.com/r/A+B+.../new/.rss?limit=100` (Atom): a self post, an itch.io link post, a v.redd.it video, an old post and a `t5_` entry |
| `blocked.html` | the 403 "You've been blocked by network security" page served to blocked IPs |
| `private.json` | the 403 body for a private subreddit |

Replace these with real (scrubbed) captures from the first `python -m gembot smoke` run that
has Reddit access, and keep the tests passing.
