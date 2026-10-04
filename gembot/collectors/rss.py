"""RSS slot: Instagram / TikTok (through RSS.app), YouTube channel feeds and any RSS/Atom feed.

Mistakes in ``config/feeds.yaml`` (``config.feeds.problems``: a repeated key, a stray
indented ``feeds:`` line, an entry without a url...) are reported as errors every run while
the valid feeds are still collected; the report is then not OK, so the status channel hears
about it after a few runs. Every enabled entry of ``config/feeds.yaml`` is one unit of work:

1. lint the URL: the RSS.app viewer page (``rss.app/feed/<id>``), JSON feeds, YouTube
   channel pages and malformed YouTube channel ids are not feeds; the error tells the user
   which URL to paste instead (and counts as a config mistake);
2. conditional GET (ETag / Last-Modified); ``304 Not Modified`` means "nothing new". At most
   one retry and ``MAX_FEED_BYTES`` per body. A host that answers 429 is not asked again this
   run: its remaining feeds are skipped with one error (many feeds share rss.app);
3. parse the body with feedparser as a stream (feedparser never fetches or opens anything; its
   HTML sanitizer and URI resolver are off, they are quadratic on hostile markup and the
   collector strips tags and resolves links itself);
4. turn each entry into a :class:`~gembot.models.Mention` whose ``source`` is the platform
   the feed is labelled with (``instagram`` / ``tiktok`` / ``youtube`` / ``rss`` / ...).
   Text fields are clipped to ``MAX_HTML_CHARS`` and every regex here is linear, so a hostile
   feed costs time in proportion to its (capped) size. A malformed URL drops that URL only.

Each entry's outcome (ok + item count, warning, error, paused, skipped) is recorded in
``report.feed_results`` for the smoke summary's "Your feeds" table.

These feeds carry little engagement data. RSS.app items have none
(``extra["engagement_known"] = False``); YouTube feeds expose likes (``media:starRating``)
and views (``extra["views"]``). They mostly count as cross-platform mentions, freshness and
keyword fit.
"""

from __future__ import annotations

import hashlib
import html
import io
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

import feedparser
import httpx
from feedparser.exceptions import ThingsNobodyCaresAboutButMe

from gembot.collectors.base import CollectContext, Collector, FeedResult
from gembot.config import FeedConfig
from gembot.http import Budget, BudgetExceeded, HttpError, RateLimited
from gembot.models import Engagement, Mention

MAX_ENTRY_AGE = timedelta(days=30)  # feeds keep old posts around; ignore anything older
MAX_ENTRIES_PER_FEED = 50
MAX_ID_LENGTH = 64
MAX_TEXT_CHARS = 4000
MAX_FEED_BYTES = 2_000_000  # per feed body; a busy real feed is 50-300 KB
MAX_HTML_CHARS = 50_000  # per title / summary / content, before any regex work (keeps long posts' links)
MAX_URL_CHARS = 2048  # longer "URLs" are junk (and would only feed the URL regexes)
MAX_RETRIES = 1  # per feed request: a slow or rate-limited feed must not eat the run's minutes

# RSS.app often fills dc:creator with the platform name instead of the account.
GENERIC_AUTHORS = frozenset(
    {"instagram", "tiktok", "youtube", "facebook", "twitter", "x", "threads", "rss.app"}
)
SOCIAL_SOURCES = frozenset({"instagram", "tiktok"})
YOUTUBE_HOSTS = frozenset({"youtube.com", "youtu.be"})
YOUTUBE_FEED_HINT = "needs a feeds/videos.xml?channel_id=UC... URL (https://www.youtube.com/feeds/videos.xml?channel_id=UC...)"
YOUTUBE_ID_RULE = "YouTube channel ids are 24 characters and start with UC"
PAUSED_NOTE = "enabled: false in feeds.yaml"

_ID_RE = re.compile(r"[A-Za-z0-9._~:/@+-]+")
_YT_VIDEO_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|shorts/|live/|embed/)|youtu\.be/)([\w-]{11})(?![\w-])", re.I
)
_YT_CHANNEL_PAGE_RE = re.compile(r"^/channel/([^/]*)")
_YT_CHANNEL_ID_RE = re.compile(r"UC[A-Za-z0-9_-]{22}")  # fullmatch: "UC" + 22 more, 24 in all
_YT_ID_CHARS_RE = re.compile(r"[A-Za-z0-9_-]+")
_INSTAGRAM_POST_RE = re.compile(r"instagram\.com/(?:[\w.]+/)?(?:p|reels?|tv)/([\w-]+)", re.I)
_TIKTOK_POST_RE = re.compile(r"tiktok\.com/@([\w.-]+)/(?:video|photo)/(\d+)", re.I)
_PROFILE_RE = re.compile(
    r"^https?://(?:www\.)?(?:instagram\.com/([\w.]+)|tiktok\.com/@([\w.-]+))/?(?:\?.*)?$", re.I
)
_TITLE_HANDLE_RE = re.compile(r"\(@([\w.]{1,30})\)")
_HANDLE_RE = re.compile(r"@[\w.]{1,30}")

# Every pattern below must stay linear in its input: no lazy ".*?" that can run to the end of
# the text from each of many start positions (that is O(n^2) on "<!--" or "<script" floods).
# Character classes stop at the next "<" / ">"; script/style and comments use find loops.
_RAW_TEXT_OPEN_RE = re.compile(r"<(script|style)\b", re.I)
_RAW_TEXT_CLOSE_RE = {
    "script": re.compile(r"</script\s*>", re.I),
    "style": re.compile(r"</style\s*>", re.I),
}
_BLOCK_RE = re.compile(r"<\s*/?\s*(?:br|p|div|li|ul|ol|tr|h[1-6]|blockquote|section|article)\b[^<>]*>", re.I)
_TAG_RE = re.compile(r"<[/!?]?[A-Za-z][^<>]*>")  # "<3" is not a tag
_HREF_RE = re.compile(r"""\bhref\s*=\s*(["'])(.*?)\1""", re.I | re.S)  # one long scan per quote kind at most
_IMG_RE = re.compile(r"""<img\b[^<>]*?\bsrc\s*=\s*(["'])(.*?)\1""", re.I | re.S)
_URL_RE = re.compile(r"""https?://[^\s<>"'`]+""", re.I)
# Captions on Instagram / TikTok are not clickable, so people write store links without a scheme.
_BARE_GAME_URL_RE = re.compile(
    r"""(?<![\w./@:-])(?:(?:store\.steampowered\.com|s\.team)/[^\s<>"']+|[a-z0-9][a-z0-9-]*\.itch\.io(?:/[^\s<>"']*)?)""",
    re.I,
)
_HASHTAG_RE = re.compile(r"(?<![\w&#/])#(\w*[^\W\d_]\w*)")  # "#12" (Devlog #12) is not a hashtag
_TRAILING_PUNCT = ".,;:!?)]}'\"…»"


class BadFeedUrl(ValueError):
    """The URL in feeds.yaml can never work as a feed (RSS.app viewer page, YouTube channel page...)."""


class NotAFeed(ValueError):
    """The response body is not an RSS / Atom feed."""


# --------------------------------------------------------------------------------------
# collector
# --------------------------------------------------------------------------------------


class RssCollector(Collector):
    name = "rss"

    def __init__(self, ctx: CollectContext, budget: Budget | None = None):
        super().__init__(ctx, budget)
        self._limited_hosts: set[str] = set()  # hosts that answered 429 this run
        self._feed_note = ""  # set by collect_feed: a 304 or old posts left out (for feed_results)

    def enabled(self) -> tuple[bool, str | None]:
        feeds = self.config.feeds.feeds
        if self.config.feeds.problems:
            return True, None  # mistakes in feeds.yaml are reported as a failure, never "skipped"
        if not feeds:
            return False, "no feeds in config/feeds.yaml"
        if not any(feed.enabled for feed in feeds):
            return False, "every feed in config/feeds.yaml is disabled"
        return True, None

    def collect(self) -> list[Mention]:
        try:
            self._collect_feeds()
        finally:
            # last, so the status channel's "Last error" quotes the mistake in feeds.yaml
            self._report_config_problems()
        return self.found

    def _collect_feeds(self) -> None:
        feeds = self.config.feeds.feeds
        for index, feed in enumerate(feeds):
            if not feed.enabled:
                self._feed_result(feed, "paused", note=PAUSED_NOTE)
                continue
            host = _host(feed.url)
            if host in self._limited_hosts:  # the error is reported once, when the host answered 429
                self._feed_result(feed, "skipped", note=f"not requested: {host} answered HTTP 429 this run")
                continue
            try:
                self._collect_one(feed)
            except BudgetExceeded:
                for later in feeds[index:]:
                    if later.enabled:
                        self._feed_result(
                            later, "skipped", note="the request budget for feeds ran out this run"
                        )
                    else:
                        self._feed_result(later, "paused", note=PAUSED_NOTE)
                raise
            if host in self._limited_hosts:
                same_host = [
                    later.name for later in feeds[index + 1 :] if later.enabled and _host(later.url) == host
                ]
                self._skip_host(host, same_host)

    def _collect_one(self, feed: FeedConfig) -> None:
        """``collect_feed`` inside ``guard``, plus this feed's row in ``report.feed_results``."""
        label = f"feed '{feed.name}'"
        errors, warnings = len(self.report.errors), len(self.report.warnings)
        mentions: list[Mention] = []
        bad_url = False
        self._feed_note = ""
        with self.guard(label):
            try:
                mentions = self.collect_feed(feed)
            except BadFeedUrl:
                bad_url = True
                self.report.config_errors += 1  # this URL can never work: feeds.yaml needs fixing
                raise
            self.found.extend(mentions)
        new_errors = [_unlabelled(text, label) for text in self.report.errors[errors:]]
        if new_errors:
            self._feed_result(feed, "config" if bad_url else "error", note=new_errors[0])
            return
        notes = [_unlabelled(text, label) for text in self.report.warnings[warnings:]]
        if self._feed_note:
            notes.append(self._feed_note)
        status = "warning" if len(self.report.warnings) > warnings else "ok"
        self._feed_result(feed, status, items=len(mentions), note="; ".join(notes))

    def _feed_result(self, feed: FeedConfig, status: str, *, items: int = 0, note: str = "") -> None:
        self.report.feed_results.append(FeedResult(feed.name, feed.source, status, items=items, note=note))

    def _report_config_problems(self) -> None:
        """Each mistake found in feeds.yaml is an error (every run, until it is fixed) and a row
        at the top of the feeds table."""
        rows: list[FeedResult] = []
        for problem in self.config.feeds.problems:
            self.report.errors.append(f"config/feeds.yaml: {problem}")
            self.report.config_errors += 1
            self.log.warning("config/feeds.yaml: %s", problem)
            rows.append(FeedResult("config/feeds.yaml", "", "config", note=problem))
        self.report.feed_results[:0] = rows

    def _skip_host(self, host: str, names: list[str]) -> None:
        """One error for every later feed on a host that said 429 (they are not requested)."""
        if not names:
            return
        listed = ", ".join(names)
        self.report.errors.append(
            f"{host}: rate limited (HTTP 429); skipped {len(names)} more feed(s) on it this run: {listed}"
        )
        self.log.warning("%s: rate limited (429); skipping %s", host, listed)

    def collect_feed(self, feed: FeedConfig) -> list[Mention]:
        """Fetch and parse one feed. Raises on lint / HTTP / parse errors (``guard`` records them)."""
        label = f"feed '{feed.name}'"
        url, warning = check_feed_url(feed.url)
        if warning:
            self.report.warnings.append(f"{label}: {warning}")
        response = self._fetch(url)
        if response.status_code == 304:
            self.log.debug("%s: not modified", label)
            self._feed_note = "nothing new since the last run (HTTP 304)"
            return []
        parsed = parse_feed(response.content)
        problem = _bozo_problem(parsed)
        if problem:
            self.report.warnings.append(f"{label}: malformed feed, kept what could be read ({problem})")
        mentions: list[Mention] = []
        old = bad = 0
        for entry in parsed.entries[:MAX_ENTRIES_PER_FEED]:
            try:
                mention = entry_to_mention(entry, parsed.feed, feed, now=self.now)
            except Exception as exc:  # one odd entry must not lose the whole feed
                bad += 1
                self.log.debug("%s: unreadable entry: %s: %s", label, type(exc).__name__, exc)
                continue
            if mention is None:
                continue
            if self.now - mention.created_at > MAX_ENTRY_AGE:
                old += 1
                continue
            mentions.append(mention)
        if bad:
            self.report.warnings.append(f"{label}: skipped {bad} unreadable entr{'y' if bad == 1 else 'ies'}")
        if old:
            self._feed_note = f"{old} post(s) older than {MAX_ENTRY_AGE.days} days left out"
        self.log.info(
            "%s: %d items (%d older than %d days skipped)", label, len(mentions), old, MAX_ENTRY_AGE.days
        )
        return mentions

    def _fetch(self, url: str) -> httpx.Response:
        try:
            return self.http.get(
                url,
                budget=self.budget,
                conditional=True,
                expect=(200,),
                retries=min(self.http.retries, MAX_RETRIES),
                max_bytes=min(self.http.max_bytes, MAX_FEED_BYTES),  # streamed: abandoned past the cap
            )
        except HttpError as exc:
            if isinstance(exc, RateLimited) or exc.status == 429:
                self._limited_hosts.add(_host(url))
            elif exc.status == 404 and _host(url) in YOUTUBE_HOSTS:
                hint = (
                    f"{exc} (YouTube feeds return 404 now and then; retried next run. If every run says "
                    "this, open the URL in a browser to check the channel_id)"
                )
                raise HttpError(hint, exc.status, exc.url) from exc
            raise


# --------------------------------------------------------------------------------------
# URL lint and parsing
# --------------------------------------------------------------------------------------


def check_feed_url(url: str) -> tuple[str, str | None]:
    """Return ``(url_to_fetch, warning)``; raise :class:`BadFeedUrl` for URLs that are not feeds.

    A YouTube ``/channel/UC...`` page is rewritten to its feed URL (with a warning); RSS.app
    viewer pages, JSON feeds, YouTube ``@handle`` / ``/c/`` / ``/user/`` pages and a YouTube
    ``channel_id`` that is not ``UC`` + 22 letters/digits/``_``/``-`` are errors. Offline: this
    never makes a request (``check-config`` runs it too).
    """
    raw = url.strip()
    try:
        parts = urlsplit(raw)
    except ValueError:  # e.g. "https://[not-an-ip]/feed"
        raise BadFeedUrl(f"not a valid URL: {raw!r}") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BadFeedUrl(f"not an http(s) URL: {raw!r}")
    host = _host(raw)
    path = parts.path
    if host == "rss.app":
        if not path.lower().endswith(".xml"):
            feed_id = path.rstrip("/").rsplit("/", 1)[-1].split(".")[0] or "<id>"
            raise BadFeedUrl(f"use the https://rss.app/feeds/{feed_id}.xml RSS URL (got {raw})")
        return raw, None
    if host in YOUTUBE_HOSTS:
        if path == "/feeds/videos.xml":
            query = parse_qs(parts.query, keep_blank_values=True)
            if "channel_id" in query:
                problem = youtube_channel_id_problem(query["channel_id"][0])
                if problem:
                    raise BadFeedUrl(f"{problem} (got {raw})")
            elif not query.keys() & {"playlist_id", "user"}:
                raise BadFeedUrl(f"{YOUTUBE_FEED_HINT}, got {raw}")
            return raw, None
        match = _YT_CHANNEL_PAGE_RE.match(path) if host == "youtube.com" else None
        if match:
            problem = youtube_channel_id_problem(match.group(1))
            if problem:
                raise BadFeedUrl(f"{problem} (got {raw})")
            fixed = f"https://www.youtube.com/feeds/videos.xml?channel_id={match.group(1)}"
            return fixed, f"{raw} is a channel page, fetched {fixed} instead (put that URL in feeds.yaml)"
        raise BadFeedUrl(f"{YOUTUBE_FEED_HINT}, got {raw}")
    if path.lower().endswith(".json"):
        raise BadFeedUrl(f"JSON feeds are not supported, use the RSS/Atom (.xml) URL (got {raw})")
    return raw, None


def youtube_channel_id_problem(value: str) -> str | None:
    """Why ``value`` cannot be a YouTube channel id, in plain words (None when it can be).

    Only the shape is checked (offline): ``UC`` + 22 letters, digits, ``_`` or ``-``.
    """
    if _YT_CHANNEL_ID_RE.fullmatch(value):
        return None
    if not value:
        return f"channel_id is empty; {YOUTUBE_ID_RULE}"
    if value.startswith("@"):
        return f"channel_id {value!r} is a handle, not the channel id; {YOUTUBE_ID_RULE}"
    if not _YT_ID_CHARS_RE.fullmatch(value):
        return f"channel_id has characters a channel id never contains (only letters, digits, _ and -); {YOUTUBE_ID_RULE}"
    if len(value) != 24:
        hint = ""
        if value.startswith("UCUC"):
            hint = " (was 'UC' pasted twice?)"
            if _YT_CHANNEL_ID_RE.fullmatch(value[2:]):
                hint += f" - try channel_id={value[2:]}"
        elif len(value) == 22 and not value.startswith("UC"):
            hint = f" (is the 'UC' at the start missing?) - try channel_id=UC{value}"
        return f"channel_id is {len(value)} characters; YouTube channel ids are 24 and start with UC{hint}"
    return f"channel_id does not start with UC; {YOUTUBE_ID_RULE}"


def parse_feed(body: bytes) -> feedparser.FeedParserDict:
    """Parse feed bytes; raise :class:`NotAFeed` when nothing usable comes out.

    Malformed XML that still yields entries is accepted (the caller warns about it). A
    well-formed feed with no entries is fine (zero items).
    """
    if len(body) > MAX_FEED_BYTES:  # HttpClient already stops reading there; this guards other callers
        raise NotAFeed(f"feed too large ({len(body)} bytes, limit {MAX_FEED_BYTES})")
    # A stream, never bytes/str: feedparser fetches str URLs and tries open() on bytes as a file path.
    # Its HTML sanitizer and relative-URI resolver are quadratic on hostile markup (a 128 KB
    # "<!--" flood takes seconds); _to_text strips tags and _links / _thumbnail resolve links.
    parsed = feedparser.parse(io.BytesIO(body), sanitize_html=False, resolve_relative_uris=False)
    problem = _bozo_problem(parsed)
    usable = any(e.get("id") or e.get("link") or e.get("title") for e in parsed.entries)
    if usable or (parsed.get("version") and problem is None):
        return parsed
    head = body[:1024].lstrip().lower()
    if not head:
        raise NotAFeed("empty response body")
    if head.startswith(b"<!doctype html") or b"<html" in head:
        raise NotAFeed("got an HTML page, not an RSS/Atom feed (check the feed URL)")
    if head[:1] in (b"{", b"["):
        raise NotAFeed("got JSON, not RSS/Atom (use the .xml feed URL)")
    raise NotAFeed(f"not a valid RSS/Atom feed ({problem or 'unknown format'})")


def _bozo_problem(parsed: Mapping[str, Any]) -> str | None:
    if not parsed.get("bozo"):
        return None
    exc = parsed.get("bozo_exception")
    if isinstance(exc, ThingsNobodyCaresAboutButMe):  # encoding overrides etc. are harmless
        return None
    return f"{type(exc).__name__}: {exc}" if exc else "malformed XML"


# --------------------------------------------------------------------------------------
# entry -> Mention
# --------------------------------------------------------------------------------------


def entry_to_mention(
    entry: dict[str, Any], channel: Mapping[str, Any], feed: FeedConfig, *, now: datetime
) -> Mention | None:
    """Turn one feedparser entry into a Mention (``None`` when nothing identifies the entry)."""
    link = _http_url(entry.get("link"))
    site = _http_url(channel.get("link")) or feed.url
    base = link or site
    title_is_html = _is_html(entry.get("title_detail"))
    title = _one_line(_to_text(_clip(str(entry.get("title") or ""), title_is_html), title_is_html))
    bodies = _bodies(entry)
    texts = [_to_text(value, is_html) for value, is_html in bodies]
    text = max(texts, key=len, default="")[:MAX_TEXT_CHARS]
    source_id = _source_id(entry, link, title)
    if source_id is None:
        return None
    kind = _kind(entry, channel, feed)
    likes = _to_int((entry.get("media_starrating") or {}).get("count"))
    views = _to_int((entry.get("media_statistics") or {}).get("views"))
    generator = _one_line(str(channel.get("generator") or "")) or None

    extra: dict[str, Any] = {
        "feed_name": feed.name,
        "feed_url": feed.url,
        "generator": generator,
        "is_short": "youtube.com/shorts/" in (link or ""),
        "engagement_known": likes is not None or views is not None,
    }
    if views is not None:
        extra["views"] = views
    if entry.get("yt_channelid"):
        extra["yt_channel_id"] = str(entry["yt_channelid"])

    return Mention(
        source=feed.source,
        source_id=source_id,
        url=link or site,
        title=title,
        text=text,
        author=_author(entry, channel, feed, kind, link),
        author_audience=feed.audience,
        created_at=_entry_time(entry, now),
        engagement=Engagement(likes=likes or 0),
        links=_links(link, bodies, [title, *texts], base),
        media_thumb=_thumbnail(entry, bodies, base),
        raw_tags=_tags(entry, [title, *texts]),
        channel=f"{feed.source}:{feed.name}",
        extra=extra,
    )


def normalize_id(raw: str) -> str:
    """Stable, key-safe id: URLs lose scheme / ``www.`` / trailing slash; long or odd ids are hashed."""
    value = raw.strip()
    try:
        parts = urlsplit(value) if value.lower().startswith(("http://", "https://")) else None
    except ValueError:  # "https://[x]/1" is not a URL: hashed below like any other odd id
        parts = None
    if parts is not None:
        host = (parts.hostname or "").removeprefix("www.")
        value = host + parts.path.rstrip("/") + (f"?{parts.query}" if parts.query else "")
    if len(value) > MAX_ID_LENGTH or not _ID_RE.fullmatch(value):
        return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]
    return value


def _source_id(entry: Mapping[str, Any], link: str | None, title: str) -> str | None:
    """Platform-native id when the link has one (YouTube video, Instagram shortcode, TikTok
    video id: identical in every feed that carries the post), else guid / link / title."""
    video = entry.get("yt_videoid") or _first_group(_YT_VIDEO_RE, link)
    if video:
        return str(video)
    native = _first_group(_INSTAGRAM_POST_RE, link) or _first_group(_TIKTOK_POST_RE, link, group=2)
    if native:
        return native
    for raw in (entry.get("id"), link, title):
        if raw and str(raw).strip():
            return normalize_id(str(raw))
    return None


def _kind(entry: Mapping[str, Any], channel: Mapping[str, Any], feed: FeedConfig) -> str:
    if feed.source == "youtube" or entry.get("yt_videoid"):
        return "youtube"
    generator = str(channel.get("generator") or "").lower()
    if feed.source in SOCIAL_SOURCES or generator.startswith("rss.app") or _host(feed.url) == "rss.app":
        return "social"
    return "generic"


def _author(
    entry: Mapping[str, Any], channel: Mapping[str, Any], feed: FeedConfig, kind: str, link: str | None
) -> str:
    detail = entry.get("author_detail") or {}
    raw = _one_line(html.unescape(str(detail.get("name") or entry.get("author") or "")))
    named = raw if raw and raw.lower() not in GENERIC_AUTHORS else None
    if kind == "youtube":
        return named or _one_line(str(channel.get("title") or "")) or feed.name
    if kind == "social":
        handle = (
            _prefixed(_first_group(_TIKTOK_POST_RE, link))
            or (raw if _HANDLE_RE.fullmatch(raw) else None)
            or _prefixed(_first_group(_TITLE_HANDLE_RE, str(channel.get("title") or "")))
            or _profile_handle(channel.get("link"))
        )
        return handle or named or feed.name
    return named or feed.name


def _profile_handle(url: Any) -> str | None:
    match = _PROFILE_RE.match(str(url or "").strip())
    if not match:
        return None
    return _prefixed(match.group(1) or match.group(2))


def _entry_time(entry: dict[str, Any], now: datetime) -> datetime:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = dict.get(entry, key)  # plain lookup: skip feedparser's deprecated updated->published alias
        if not value:
            continue
        try:
            return min(datetime(*value[:6], tzinfo=UTC), now)  # feedparser gives UTC struct_time
        except (TypeError, ValueError):
            continue
    return now


def _bodies(entry: Mapping[str, Any]) -> list[tuple[str, bool]]:
    """(value, is_html) for the summary/description and every content:encoded / Atom content."""
    out: list[tuple[str, bool]] = []
    if entry.get("summary"):
        is_html = _is_html(entry.get("summary_detail"))
        out.append((_clip(str(entry["summary"]), is_html), is_html))
    for content in entry.get("content") or []:
        if content.get("value"):
            is_html = _is_html(content)
            out.append((_clip(str(content["value"]), is_html), is_html))
    return out


def _links(link: str | None, bodies: list[tuple[str, bool]], texts: Iterable[str], base: str) -> list[str]:
    found: list[str] = [link] if link else []
    for value, is_html in bodies:
        if is_html:
            found.extend(_join(base, m.group(2)) for m in _HREF_RE.finditer(value))
    for text in texts:
        found.extend(m.group(0) for m in _URL_RE.finditer(text))
        found.extend("https://" + m.group(0) for m in _BARE_GAME_URL_RE.finditer(text))
    return _dedupe(url for url in map(_clean_url, found) if url)


def _thumbnail(entry: Mapping[str, Any], bodies: list[tuple[str, bool]], base: str) -> str | None:
    candidates: list[Any] = [thumb.get("url") for thumb in entry.get("media_thumbnail") or []]
    for media in entry.get("media_content") or []:
        medium = str(media.get("medium") or "").lower()
        mime = str(media.get("type") or "").lower()
        if medium == "image" or mime.startswith("image/") or (not medium and not mime):
            candidates.append(media.get("url"))
    for enclosure in entry.get("enclosures") or []:
        if str(enclosure.get("type") or "").lower().startswith("image/"):
            candidates.append(enclosure.get("href"))
    for value, is_html in bodies:
        match = _IMG_RE.search(value) if is_html else None
        if match:
            candidates.append(match.group(2))
    for candidate in candidates:
        url = _http_url(_join(base, str(candidate))) if candidate else None
        if url:
            return url
    return None


def _tags(entry: Mapping[str, Any], texts: Iterable[str]) -> list[str]:
    tags = [tag.lower() for text in texts for tag in _HASHTAG_RE.findall(text)]
    for tag in entry.get("tags") or []:
        term = _one_line(str(tag.get("term") or "")).lstrip("#").lower()
        if term:
            tags.append(term)
    return _dedupe(tags)


# --------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------


def _is_html(detail: Mapping[str, Any] | None) -> bool:
    return str((detail or {}).get("type") or "text/html").lower() != "text/plain"


def _clip(value: str, is_html: bool) -> str:
    """At most ``MAX_HTML_CHARS``; HTML is cut before a tag the limit would split."""
    if len(value) <= MAX_HTML_CHARS:
        return value
    value = value[:MAX_HTML_CHARS]
    if is_html:
        cut = value.rfind("<")
        if cut > value.rfind(">"):
            value = value[:cut]
    return value


def _to_text(value: str, is_html: bool) -> str:
    """HTML -> plain text with line breaks kept; plain text only gets its whitespace tidied."""
    if is_html:
        value = _drop_raw_text(value)
        value = _BLOCK_RE.sub("\n", value)
        value = html.unescape(_TAG_RE.sub("", _drop_comments(value)))
    lines = (" ".join(line.split()) for line in value.splitlines())
    return "\n".join(line for line in lines if line)


def _drop_raw_text(value: str) -> str:
    """Replace each ``<script>`` / ``<style>`` element with a space, in one forward pass.

    An element without its closing tag is left as text (and no later one of that kind can
    close either), which is what the old ``<(script|style)\\b.*?</\\1\\s*>`` regex did too.
    """
    out: list[str] = []
    pos = 0
    unclosed: set[str] = set()
    for opened in _RAW_TEXT_OPEN_RE.finditer(value):
        kind = opened.group(1).lower()
        if opened.start() < pos or kind in unclosed:
            continue  # inside an element already dropped, or known to have no closing tag
        closed = _RAW_TEXT_CLOSE_RE[kind].search(value, opened.end())
        if closed is None:
            unclosed.add(kind)
            continue
        out.append(value[pos : opened.start()])
        out.append(" ")
        pos = closed.end()
    out.append(value[pos:])
    return "".join(out)


def _drop_comments(value: str) -> str:
    """Remove ``<!-- ... -->`` in one forward pass; an unclosed ``<!--`` stays as text."""
    out: list[str] = []
    pos = 0
    while (start := value.find("<!--", pos)) >= 0:
        end = value.find("-->", start + 4)
        if end < 0:
            break  # no "-->" anywhere after this, so no later comment closes either
        out.append(value[pos:start])
        pos = end + 3
    out.append(value[pos:])
    return "".join(out)


def _unlabelled(text: str, label: str) -> str:
    """An error / warning without its ``feed '...': `` and exception-name prefixes (the feeds
    table already names the feed and its status)."""
    text = text.removeprefix(f"{label}: ")
    for kind in (BadFeedUrl, NotAFeed):
        text = text.removeprefix(f"{kind.__name__}: ")
    return text


def _one_line(value: str) -> str:
    return " ".join(value.split())


def _host(url: str) -> str:
    try:
        host = (urlsplit(url.strip()).hostname or "").lower()
    except ValueError:
        return ""
    for prefix in ("www.", "m."):
        host = host.removeprefix(prefix)
    return host


def _http_url(value: Any) -> str | None:
    if not value:
        return None
    url = str(value).strip()
    if len(url) > MAX_URL_CHARS:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:  # "https://[link in bio]" in a caption: drop this URL, keep the post
        return None
    return url if parts.scheme in ("http", "https") and parts.netloc else None


def _join(base: str, href: str) -> str:
    """``urljoin`` that never raises: a malformed base or href comes back as-is (callers then
    drop it through ``_http_url``, an absolute href still works with a broken base)."""
    href = html.unescape(href.strip())
    try:
        return urljoin(base, href)
    except ValueError:
        return href


def _clean_url(url: str) -> str | None:
    url = url.strip()
    opened, closed = url.count("("), url.count(")")  # counted once: ")))..." stays linear
    end = len(url)
    while end and url[end - 1] in _TRAILING_PUNCT:
        if url[end - 1] == ")":
            if opened >= closed:
                break  # keep balanced parentheses, e.g. wiki/Foo_(game)
            closed -= 1
        end -= 1
    return _http_url(url[:end])


def _first_group(pattern: re.Pattern[str], value: str | None, group: int = 1) -> str | None:
    match = pattern.search(value) if value else None
    return match.group(group) if match else None


def _prefixed(handle: str | None) -> str | None:
    return f"@{handle}" if handle else None


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(int(str(value).replace(",", "").strip()), 0)
    except ValueError:
        return None


def _dedupe(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))
