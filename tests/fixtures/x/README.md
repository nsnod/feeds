# X API fixtures

These are **not live captures**. The X API is pay-per-use (no free tier for new
developers since February 2026), so nothing was recorded during the build. The shapes
follow the official X SDK schemas (`xdk` on PyPI/npm, July 2026), tweepy 4.17 /
twitter-api-v2 1.29 and error bodies quoted in 2026 bug reports. IDs are real-looking
snowflakes whose timestamps match `created_at`; usernames, games and links are made up.

| file | endpoint / status | what it exercises |
|---|---|---|
| `search_two_posts.json` | `GET /2/tweets/search/recent` 200 | two posts with Steam links, `includes.users` (author expansion), a long post (`note_tweet` with its own entities), `unwound_url`, links back to x.com / twitter.com, `meta.next_token` (must not be followed) |
| `search_missing_author.json` | 200 | one author missing from `includes.users`, plus an `errors[]` array next to `data` (partial error) |
| `search_post_vocabulary.json` | 200 | the newer "post" names (`repost_count`, `post_count`, `edit_history_post_ids`), a URL entity with only `url`, a post without `created_at` / metrics / entities |
| `search_empty.json` | 200 | no matches: `{"meta": {"result_count": 0}}` (no `data`, no `newest_id`) |
| `error_400.json` | 400 | invalid query (`errors[].parameters.query`) |
| `error_400_since_id.json` | 400 | `since_id` older than the 7-day window (`errors[].parameters.since_id`) |
| `error_401.json` | 401 | bad / revoked bearer token |
| `error_402.json` | 402 | prepaid credits used up. **Body shape unverified**; the collector matches on the status only |
| `error_403.json` | 403 | `reason: client-not-enrolled` (app not in the Pay-per-use package / Production environment) |
| `error_429.json` | 429 | rate limited; tests add the `x-rate-limit-limit/remaining/reset` headers |

If you enable X (`X_BEARER_TOKEN`), replace these with real responses: run one search
with the query from `config/sources.yaml`, scrub usernames, and keep the file names so the
tests in `tests/test_collector_x.py` keep working.
