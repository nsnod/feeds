# itch.io fixtures

itch.io could not be reached from the build sandbox, so these files are **synthetic**. They
follow the documented 2026 feed schema, taken from a real capture of
`https://itch.io/games/made-with-gb-studio.xml` (NextUI-Itchio-Pak v1.0.25 testdata):

* RSS 2.0, 36 items per full page, `?page=N` for more;
* 12 children per item, in this order: `guid`, `title`, `plainTitle`, `imageurl`, `price`,
  `currency`, `link`, `description`, `pubDate`, `createDate`, `updateDate`, `platforms`;
* `guid` == `link`; `title` is decorated (`Name [$4.99] [Action]`, `Name [Free] [Genre]`);
  `price` is `$0.00` for free games; `description` is CDATA holding HTML-escaped text plus
  an `<img>`; dates are RFC 1123 GMT and `pubDate` == `createDate`;
* `<platforms>` holds `<windows>yes</windows>`-style children or is empty.

A few items break the schema on purpose (no `plainTitle`, no `imageurl`, no `createDate`,
unparseable dates, a link to a jam page instead of a game) to exercise the parser's
fallbacks. Real feeds are a single line; these put one item per line for readable diffs.

| file | what |
|---|---|
| `make_fixtures.py` | builds every `.xml` here: `python tests/fixtures/itch/make_fixtures.py` |
| `new_and_popular.xml` | 8 items (7 games + 1 jam link), pathological titles, emoji title, EUR price |
| `tag_coop.xml` | 5 items overlapping new-and-popular and newest (dedupe / merge tests) |
| `newest_page1.xml`, `newest_page2.xml` | 36 distinct games each (pagination) |
| `newest_page3.xml` | 10 games: a short last page |
| `empty_channel.xml` | an empty `<channel>` (past-the-end page) |
| `malformed.xml` | a truncated document |
| `cloudflare_challenge.html` | the shape of a Cloudflare managed-challenge page (served with HTTP 403 and `cf-mitigated: challenge`) |

**Replace these with real captures from the first smoke run** (`python -m gembot smoke` on
GitHub Actions; scrub nothing, itch feeds are public), keeping the file names, and update
the expected values in `tests/test_collector_itch.py`.
