# Steam fixtures

Live Steam was not reachable from the build sandbox. These files follow the **documented
2026 response shapes** (field names, markup structure, quirks) from the research notes of
2026-10-04. The game names, app ids and studios are made up.

| file | endpoint | what it covers |
|---|---|---|
| `search_comingsoon_page0.json` | `search/results/?infinite=1&filter=comingsoon` | `{success, results_html, total_count, start}` envelope, `total_count` as a **string**, a bundle row, a package row, a playtest row, a row without `data-ds-tagids`, `&amp;` in a title |
| `search_popular.json` | `search/results/?infinite=1&filter=popularcomingsoon` | ranked list, a demo row, overlap with the coming-soon page |
| `search_empty.json` | `search/results/?infinite=1` | empty page (`results_html` holds only the comment) |
| `featuredcategories.json` | `api/featuredcategories` | spotlight/specials blocks, `coming_soon` with a demo, `new_releases` with a non-app (`type: 1`) item |
| `appdetails_coming_soon.json` | `api/appdetails?appids=3456780` | online co-op horror, `"Q1 2027"`, no `price_overview`, HTML entities in `short_description` |
| `appdetails_released.json` | `api/appdetails?appids=3611110` | exact date `"Sep 30, 2026"`, `price_overview`, Early Access genre (id `"70"`) |
| `appdetails_keyed_by_other_id.json` | `api/appdetails?appids=3501230` | Sept-2026 quirk: keyed by another id, `data.steam_appid` is the requested one |
| `appdetails_fail.json` | `api/appdetails?appids=3544440` | `{"<id>": {"success": false}}` (adult-only / region-locked / invalid) |
| `appdetails_free_empty.json` | `api/appdetails?appids=3578900` | `{"success": true, "data": []}` |

**Replace these with real captures from the first smoke run** (`python -m gembot smoke`
or the `smoke.yml` workflow), keeping the same file names, and re-run
`tests/test_collector_steam.py`. Things to check on a real capture: the `success`/`start`
keys of the search envelope, whether `data-ds-tagids` is present on rows, and how often the
appdetails response is keyed by a different id.
