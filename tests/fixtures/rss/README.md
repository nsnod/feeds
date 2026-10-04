# RSS collector fixtures

These files feed `tests/test_collector_rss.py`. The XML **structure** (element names,
namespaces, CDATA wrapping, `&amp;` escaping, which fields RSS.app and YouTube fill in)
is copied from verified samples of real feeds, checked against feedparser 6.0.14. The
**values** (accounts, captions, ids, image URLs, dates) are fictional.

| file | what it imitates |
|---|---|
| `rssapp_instagram.xml` | RSS.app feed of an Instagram profile: 3 items (two `/p/` posts, one `/reel/`), `dc:creator` is literally "Instagram", image URLs with raw `&` inside the description CDATA and `&amp;` in `enclosure` / `media:content` |
| `rssapp_tiktok.xml` | RSS.app feed of a TikTok profile: one item with `dc:creator` "@handle", one with "TikTok" |
| `youtube_channel.xml` | `youtube.com/feeds/videos.xml?channel_id=UC...` Atom: a premiere without `media:community` (no views / likes), a normal video, a Short (`/shorts/` link) and one video older than 30 days |
| `generic_blog.rss` | Plain RSS 2.0 blog: HTML description with a Steam link, a relative link, an image `enclosure`, `<author>email (Name)</author>`, an item without guid/date and one old item |
| `atom_generic.xml` | Plain Atom feed: `type="html"` title/summary/content, relative links and images, a `tag:` id and a `urn:uuid:` entry with no link |
| `not_xml.html` | What you get from the RSS.app viewer page (`rss.app/feed/<id>`) instead of the feed |
| `broken.xml` | Truncated / malformed XML |

Dates are relative to the test clock `tests.factories.NOW` (2026-10-03 12:00 UTC).

**TODO after the first smoke run:** replace `rssapp_instagram.xml`, `rssapp_tiktok.xml` and
`youtube_channel.xml` with real captures (scrub account names, keep the structure), then
update the expected values in the tests.
