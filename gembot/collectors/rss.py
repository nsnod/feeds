"""RSS slot: Instagram / TikTok (through RSS.app), YouTube channel feeds and any RSS/Atom feed.

Every enabled entry of ``config/feeds.yaml`` is one unit of work:

1. lint the URL: the RSS.app viewer page (``rss.app/feed/<id>``), JSON feeds and YouTube
   channel pages are not RSS; the error tells the user which URL to paste instead;
2. conditional GET (ETag / Last-Modified); ``304 Not Modified`` means "nothing new";
3. parse the body with feedparser as a stream (feedparser never fetches or opens anything);
4. turn each entry into a :class:`~gembot.models.Mention` whose ``source`` is the platform
   the feed is labelled with (``instagram`` / ``tiktok`` / ``youtube`` / ``rss`` / ...).

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
from urllib.parse import urljoin, urlsplit

import feedparser
import httpx
from feedparser.exceptions import ThingsNobodyCaresAboutButMe

from gembot.collectors.base import Collector
from gembot.config import FeedConfig
from gembot.http import HttpError
from gembot.models import Engagement, Mention

MAX_ENTRY_AGE = timedelta(days=30)  # feeds keep old posts around; ignore anything older
MAX_ENTRIES_PER_FEED = 50
MAX_ID_LENGTH = 64
MAX_TEXT_CHARS = 4000

# RSS.app often fills dc:creator with the platform name instead of the account.
GENERIC_AUTHORS = frozenset(
    {"instagram", "tiktok", "youtube", "facebook", "twitter", "x", "threads", "rss.app"}
)
SOCIAL_SOURCES = frozenset({"instagram", "tiktok"})
YOUTUBE_HOSTS = frozenset({"youtube.com", "youtu.be"})
YOUTUBE_FEED_HINT = "needs a feeds/videos.xml?channel_id=UC... URL (https://www.youtube.com/feeds/videos.xml?channel_id=UC...)"

_ID_RE = re.compile(r"[A-Za-z0-9._~:/@+-]+")
_YT_VIDEO_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|shorts/|live/|embed/)|youtu\.be/)([\w-]{11})(?![\w-])", re.I
)
_YT_CHANNEL_PAGE_RE = re.compile(r"^/channel/(UC[\w-]{22})/?")
_INSTAGRAM_POST_RE = re.compile(r"instagram\.com/(?:[\w.]+/)?(?:p|reels?|tv)/([\w-]+)", re.I)
_TIKTOK_POST_RE = re.compile(r"tiktok\.com/@([\w.-]+)/(?:video|photo)/(\d+)", re.I)
_PROFILE_RE = re.compile(
    r"^https?://(?:www\.)?(?:instagram\.com/([\w.]+)|tiktok\.com/@([\w.-]+))/?(?:\?.*)?$", re.I
)
_TITLE_HANDLE_RE = re.compile(r"\(@([\w.]{1,30})\)")
_HANDLE_RE = re.compile(r"@[\w.]{1,30}")

_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1\s*>", re.I | re.S)
_BLOCK_RE = re.compile(r"<\s*/?\s*(?:br|p|div|li|ul|ol|tr|h[1-6]|blockquote|section|article)\b[^<>]*>", re.I)
_TAG_RE = re.compile(r"<!--.*?-->|<[/!?]?[A-Za-z][^<>]*>", re.S)  # "<3" is not a tag
_HREF_RE = re.compile(r"""\bhref\s*=\s*(["'])(.*?)\1""", re.I | re.S)
_IMG_RE = re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*(["'])(.*?)\1""", re.I | re.S)
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

    def enabled(self) -> tuple[bool, str | None]:
        feeds = self.config.feeds.feeds
        if not feeds:
            return False, "no feeds in config/feeds.yaml"
        if not any(feed.enabled for feed in feeds):
            return False, "every feed in config/feeds.yaml is disabled"
        return True, None

    def collect(self) -> list[Mention]:
        for feed in self.config.feeds.feeds:
            if not feed.enabled:
                continue
            with self.guard(f"feed '{feed.name}'"):
                self.found.extend(self.collect_feed(feed))
        return self.found

    def collect_feed(self, feed: FeedConfig) -> list[Mention]:
        """Fetch and parse one feed. Raises on lint / HTTP / parse errors (``guard`` records them)."""
        label = f"feed '{feed.name}'"
        url, warning = check_feed_url(feed.url)
        if warning:
            self.report.warnings.append(f"{label}: {warning}")
        response = self._fetch(url)
        if response.status_code == 304:
            self.log.debug("%s: not modified", label)
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
        self.log.info(
            "%s: %d items (%d older than %d days skipped)", label, len(mentions), old, MAX_ENTRY_AGE.days
        )
        return mentions

    def _fetch(self, url: str) -> httpx.Response:
        try:
            return self.http.get(url, budget=self.budget, conditional=True, expect=(200,))
        except HttpError as exc:
            if exc.status == 404 and _host(url) in YOUTUBE_HOSTS:
                hint = f"{exc} (YouTube feeds return 404 now and then; retried next run)"
                raise HttpError(hint, exc.status, exc.url) from exc
            raise


# --------------------------------------------------------------------------------------
# URL lint and parsing
# --------------------------------------------------------------------------------------


def check_feed_url(url: str) -> tuple[str, str | None]:
    """Return ``(url_to_fetch, warning)``; raise :class:`BadFeedUrl` for URLs that are not feeds.

    A YouTube ``/channel/UC...`` page is rewritten to its feed URL (with a warning); RSS.app
    viewer pages, JSON feeds and YouTube ``@handle`` / ``/c/`` / ``/user/`` pages are errors.
    """
    raw = url.strip()
    parts = urlsplit(raw)
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
            return raw, None
        match = _YT_CHANNEL_PAGE_RE.match(path) if host == "youtube.com" else None
        if match:
            fixed = f"https://www.youtube.com/feeds/videos.xml?channel_id={match.group(1)}"
            return fixed, f"{raw} is a channel page, fetched {fixed} instead (put that URL in feeds.yaml)"
        raise BadFeedUrl(f"{YOUTUBE_FEED_HINT}, got {raw}")
    if path.lower().endswith(".json"):
        raise BadFeedUrl(f"JSON feeds are not supported, use the RSS/Atom (.xml) URL (got {raw})")
    return raw, None


def parse_feed(body: bytes) -> feedparser.FeedParserDict:
    """Parse feed bytes; raise :class:`NotAFeed` when nothing usable comes out.

    Malformed XML that still yields entries is accepted (the caller warns about it). A
    well-formed feed with no entries is fine (zero items).
    """
    # A stream, never bytes/str: feedparser fetches str URLs and tries open() on bytes as a file path.
    parsed = feedparser.parse(io.BytesIO(body))
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
    title = _one_line(_to_text(str(entry.get("title") or ""), _is_html(entry.get("title_detail"))))
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
    if value.lower().startswith(("http://", "https://")):
        parts = urlsplit(value)
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
        out.append((str(entry["summary"]), _is_html(entry.get("summary_detail"))))
    for content in entry.get("content") or []:
        if content.get("value"):
            out.append((str(content["value"]), _is_html(content)))
    return out


def _links(link: str | None, bodies: list[tuple[str, bool]], texts: Iterable[str], base: str) -> list[str]:
    found: list[str] = [link] if link else []
    for value, is_html in bodies:
        if is_html:
            found.extend(urljoin(base, html.unescape(m.group(2).strip())) for m in _HREF_RE.finditer(value))
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
        url = _http_url(urljoin(base, html.unescape(str(candidate).strip()))) if candidate else None
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


def _to_text(value: str, is_html: bool) -> str:
    """HTML -> plain text with line breaks kept; plain text only gets its whitespace tidied."""
    if is_html:
        value = _SCRIPT_RE.sub(" ", value)
        value = _BLOCK_RE.sub("\n", value)
        value = html.unescape(_TAG_RE.sub("", value))
    lines = (" ".join(line.split()) for line in value.splitlines())
    return "\n".join(line for line in lines if line)


def _one_line(value: str) -> str:
    return " ".join(value.split())


def _host(url: str) -> str:
    host = (urlsplit(url.strip()).hostname or "").lower()
    for prefix in ("www.", "m."):
        host = host.removeprefix(prefix)
    return host


def _http_url(value: Any) -> str | None:
    if not value:
        return None
    url = str(value).strip()
    parts = urlsplit(url)
    return url if parts.scheme in ("http", "https") and parts.netloc else None


def _clean_url(url: str) -> str | None:
    url = url.strip()
    while url and url[-1] in _TRAILING_PUNCT:
        if url[-1] == ")" and url.count("(") >= url.count(")"):
            break  # keep balanced parentheses, e.g. wiki/Foo_(game)
        url = url[:-1]
    return _http_url(url)


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
