# Bluesky fixtures

These responses are **synthetic**. Their shapes follow the 2026 `app.bsky.*` / `com.atproto.*`
lexicons (from the `@atproto/api` 0.23 / `@atproto/bsky` / `@atproto/pds` sources) and observed
samples. Every account, DID, token and post is fake. Times are relative to the test clock
`NOW = 2026-10-03T12:00:00Z` (`tests/factories.py`). Link facets carry correct UTF-8 byte offsets.

**TODO:** replace them with real (scrubbed) captures from the first `smoke` run that has
`BLUESKY_HANDLE` / `BLUESKY_APP_PASSWORD` set, then update the expectations in
`tests/test_collector_bluesky.py`.

| file | endpoint | what it covers |
|---|---|---|
| `create_session.json` | `POST bsky.social/xrpc/com.atproto.server.createSession` | tokens, `didDoc` with the `#atproto_pds` service (`amanita.us-east.host.bsky.network`), `active: true` |
| `refresh_session.json` | `POST bsky.social/xrpc/com.atproto.server.refreshSession` | rotated access + refresh tokens |
| `search_indiedev.json` | `GET {pds}/xrpc/app.bsky.feed.searchPosts` | 8 posts: an external Steam link embed with link and tag facets; images plus `record.tags` plus a bare URL in the text; video; gallery (`items[].thumbnail`); recordWithMedia (a quote plus an external itch link, with a first line over 120 chars); then three that must be skipped: author self-label `!no-unauthenticated`, post label `porn`, and a post 100h old |
| `search_empty.json` | same | no hits |
| `thread.json` | `GET public.api.bsky.app/xrpc/app.bsky.feed.getPostThread?depth=1&parentHeight=0` | 3 `threadViewPost` replies (one from an opted-out author), one `notFoundPost`, one `blockedPost` |
| `profile.json` | `GET public.api.bsky.app/xrpc/app.bsky.actor.getProfile` | `followersCount` 812 |
| `error_expired.json` | any authenticated call | `400 {"error": "ExpiredToken"}` (the PDS route; AppView-direct answers 401) |
| `error_auth.json` | createSession | `401 {"error": "AuthenticationRequired"}` (wrong handle or app password) |
| `cdn_403.html` | `searchPosts` without a token | the CDN's HTML refusal page (`text/html`, BunnyCDN) |
