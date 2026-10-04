"""itch.io collector: the browse-page RSS feeds (any browse URL + ``.xml``).

itch publishes no engagement numbers in its feeds, so the signal is *where* a game sits:
``Mention.rank`` is its 1-based position in a ranked listing (new-and-popular, the tag
feeds) and ``extra["itch_ranks"]`` records every feed it appeared in this run.

Feed format (checked against a real 2026 capture, see ``tests/fixtures/itch/README.md``):
RSS 2.0, 36 items per page, ``?page=N`` for more. Every ``<item>`` has, in order: guid
(== link), a decorated ``<title>`` ("Name [$4.99] [Action]"), the clean ``<plainTitle>``,
``<imageurl>``, ``<price>`` ("$0.00" even when free), ``<currency>``, ``<link>``, a CDATA
``<description>`` (HTML-escaped short text + ``<img>``), ``<pubDate>`` (== createDate),
``<createDate>``, ``<updateDate>`` and ``<platforms>`` (``<windows>yes</windows>`` ...).

Why ``xml.etree.ElementTree`` and not feedparser: feedparser lowercases the itch-only
elements, flattens ``<platforms>`` and hides structure behind heuristics. The feed has a
small fixed schema, so an exact read is simpler and testable. The input is guarded:
a size cap (enforced while downloading, and again before parsing), the body must be UTF-8,
documents with a DOCTYPE/ENTITY declaration anywhere are refused (no entity expansion, no
XXE; expat >= 2.4 also caps amplification), the root must be ``<rss>`` and at most
``MAX_ITEMS_PER_PAGE`` items are read. Descriptions are clipped before any regex work and
the regexes stop at the next ``<``/``>``, so hostile markup costs linear time.

Blocking: GitHub runner IPs may get Cloudflare challenges, especially on combined
sort+tag URLs. A challenge (``cf-mitigated: challenge`` header or a "Just a moment" page)
or any other 403 is a *strike*. The feed's fallback is then read once (page 1):
``fallback_url``, optionally filtered to games matching ``fallback_keywords``, ranked only
if ``fallback_ranked`` (see ``config/sources.yaml`` for the default: sort-only newest feed
+ tag keywords). Later feeds go straight to their fallbacks (remembered for
``fallback_hold_hours`` in the collector's scratch state), and after ``max_challenges``
strikes the remaining itch feeds are skipped for this run. A 429 that ``HttpClient`` cannot
wait out also stops itch for the run. Every page is fetched at most once per run, so a
fallback to a feed that was already read (newest.xml) costs no request.

ETag / Last-Modified are not used (itch did not send them as of 2026); they are only logged.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import ClassVar
from urllib.parse import urlsplit

import httpx

from gembot.collectors.base import CollectContext, Collector
from gembot.config import ItchFeed, ItchSource
from gembot.http import Budget, HttpError, RateLimited
from gembot.models import Engagement, Mention, ensure_utc

PAGE_SIZE = 36  # items per itch feed page
ACCEPT = "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.1"
EXPECT = (200, 403, 404, 410, 503)  # statuses we classify ourselves (HttpClient raises on others)
MAX_FEED_BYTES = 5_000_000  # a real 36-item page is ~40 KB
MAX_ITEMS_PER_PAGE = 100
MAX_TEXT_CHARS = 500
MAX_NAME_CHARS = 200
MAX_DESCRIPTION_CHARS = 20_000  # of description HTML read; real ones are < 1 KB, text keeps 500 chars
CHALLENGE_MARKERS = (b"challenges.cloudflare.com", b"just a moment", b"__cf_chl")
RESERVED_SUBDOMAINS = frozenset({"www", "api", "static", "img"})

_LABEL = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_TAG_SEGMENT = re.compile(r"/tag-([a-z0-9_-]+)", re.I)
_BRACKET = re.compile(r"\[([^\[\]]*)\]")
_PRICE_LABEL = re.compile(
    r"^(?:[A-Z]{0,3}[^\w\s\[\]]{1,2}\s?|[A-Z]{3}\s?)?\d+(?:[.,]\d+)?\s?(?:[^\w\s\[\]]{1,2}|[A-Z]{3})?$"
)
_PRICE_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
# "[^<>]" (not "[^>]"): with many "<" and no ">" each start would otherwise scan to the end, O(n^2).
_IMG_TAG = re.compile(r"<img\b[^<>]*>", re.I)
_IMG_SRC = re.compile(r"""<img\b[^<>]*?\bsrc\s*=\s*["']([^"'<>]+)["']""", re.I)
_HTML_TAG = re.compile(r"<[^<>]+>")
_DECLARATION = re.compile(r"<!DOCTYPE|<!ENTITY", re.I)
_WS = re.compile(r"\s+")


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class ItchFeedError(HttpError):
    """The feed answered, but not with usable RSS (malformed, HTML, too big, ...)."""


class ItchFeedGone(ItchFeedError):
    """404/410 on page 1: the feed URL no longer exists (config/sources.yaml needs updating)."""


class ItchBlocked(ItchFeedError):
    """Cloudflare challenge or another 403: itch.io refused this client."""


# --------------------------------------------------------------------------------------
# Parsing (pure functions, no network)
# --------------------------------------------------------------------------------------


@dataclass
class ItchItem:
    key: str  # canonical "<dev>/<slug>", lowercase
    dev: str
    slug: str
    url: str  # https://<dev>.itch.io/<slug>
    name: str
    text: str
    position: int  # 1-based position on its page (non-game items count too)
    created_at: datetime
    updated_at: datetime | None = None
    image: str | None = None
    price: str | None = None  # as itch shows it: "$4.99", "3.39€"
    price_value: float | None = None
    currency: str | None = None
    is_free: bool | None = None
    genre: str | None = None
    platforms: list[str] = field(default_factory=list)


@dataclass
class ItchPage:
    size: int  # number of <item> elements on the page (games or not)
    items: list[ItchItem] = field(default_factory=list)  # game items only
    skipped: int = 0  # items that do not link to a game page
    title: str = ""


def canonical_game(url: str) -> tuple[str, str] | None:
    """``(dev, slug)`` for a game page ``https://<dev>.itch.io/<slug>``, else None."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    if not host.endswith(".itch.io"):
        return None
    dev = host.removesuffix(".itch.io")
    if not _LABEL.match(dev) or dev in RESERVED_SUBDOMAINS:
        return None
    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) != 1:
        return None
    slug = segments[0].lower()
    if not _LABEL.match(slug):
        return None
    return dev, slug


def is_price_label(token: str) -> bool:
    token = token.strip()
    return token.lower() == "free" or bool(_PRICE_LABEL.match(token))


def title_tokens(raw_title: str, plain_title: str | None) -> tuple[str | None, str | None]:
    """``(price label, genre)`` from the decorated title "Name [$4.99] [Action]".

    The decoration is read from the tail after ``plain_title``; names may contain their
    own brackets ("[Spread] [Free] [Platformer]"), so the *last* price-looking token wins
    and the genre is the token right after it. Never used to derive the game name.
    """
    matched = bool(plain_title) and raw_title.startswith(plain_title or "")
    tail = raw_title[len(plain_title or "") :] if matched else raw_title
    tokens = [token.strip() for token in _BRACKET.findall(tail)]
    price_at = None
    for index, token in enumerate(tokens):
        if is_price_label(token):
            price_at = index
    if price_at is None:
        return None, (tokens[-1] or None) if matched and tokens else None
    genre = tokens[price_at + 1] if price_at + 1 < len(tokens) else None
    return tokens[price_at], genre or None


def parse_price(text: str | None) -> float | None:
    if not text:
        return None
    match = _PRICE_NUMBER.search(text)
    return float(match.group().replace(",", ".")) if match else None


def parse_date(text: str | None) -> datetime | None:
    """RFC 1123 ("Fri, 11 Dec 2020 02:30:01 GMT") -> aware UTC datetime, or None."""
    if not text:
        return None
    try:
        return ensure_utc(parsedate_to_datetime(text.strip()))
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def short_text(description_html: str) -> str:
    """Description HTML -> plain text: drop <img>, strip tags, then unescape entities."""
    text = _IMG_TAG.sub(" ", description_html[:MAX_DESCRIPTION_CHARS])
    text = _HTML_TAG.sub(" ", text)
    text = _WS.sub(" ", html.unescape(text)).strip()
    if len(text) > MAX_TEXT_CHARS:
        text = text[: MAX_TEXT_CHARS - 1].rstrip() + "…"
    return text


def _http_url(value: str | None) -> str | None:
    value = html.unescape((value or "").strip())
    if value.startswith("//"):
        value = "https:" + value
    return value if value.lower().startswith(("https://", "http://")) else None


def _local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _slug_title(slug: str) -> str:
    return " ".join(part.capitalize() for part in re.split(r"[-_]+", slug) if part) or slug


def _clean_name(raw: str) -> str:
    return _WS.sub(" ", html.unescape(raw)).strip()[:MAX_NAME_CHARS]


def _unique(values: Iterable[str | None]) -> list[str]:
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out


def parse_item(element: ET.Element, *, position: int, now: datetime) -> ItchItem | None:
    """One ``<item>`` -> :class:`ItchItem`, or None when it does not link to a game page."""
    fields: dict[str, ET.Element] = {}
    for child in element:
        fields.setdefault(_local(child.tag), child)

    def text(name: str) -> str:
        node = fields.get(name)
        return (node.text or "").strip() if node is not None else ""

    game = canonical_game(text("link") or text("guid"))
    if game is None:
        return None
    dev, slug = game
    plain = text("plaintitle")
    name = _clean_name(plain) or _slug_title(slug)
    price_label, genre = title_tokens(text("title"), plain or None)

    description = fields.get("description")
    description_html = "".join(description.itertext()) if description is not None else ""
    description_html = description_html[:MAX_DESCRIPTION_CHARS]  # before any regex work
    image = _http_url(text("imageurl"))
    if image is None:
        found = _IMG_SRC.search(description_html)
        image = _http_url(found.group(1)) if found else None

    price = text("price") or (price_label if price_label and price_label.lower() != "free" else None)
    price_value = parse_price(price)
    if price_value == 0 or (price_label or "").lower() == "free":
        is_free: bool | None = True
    else:
        is_free = False if price_value is not None else None

    platforms_node = fields.get("platforms")
    platforms = (
        _unique(_local(c.tag) for c in platforms_node if (c.text or "").strip().lower() == "yes")
        if platforms_node is not None
        else []
    )
    return ItchItem(
        key=f"{dev}/{slug}",
        dev=dev,
        slug=slug,
        url=f"https://{dev}.itch.io/{slug}",
        name=name,
        text=short_text(description_html),
        position=position,
        created_at=parse_date(text("createdate")) or parse_date(text("pubdate")) or now,
        updated_at=parse_date(text("updatedate")),
        image=image,
        price=price,
        price_value=price_value,
        currency=text("currency").upper() or None,
        is_free=is_free,
        genre=genre,
        platforms=platforms,
    )


def parse_feed(content: bytes, *, now: datetime) -> ItchPage:
    """Parse one feed page. Raises :class:`ItchFeedError` for anything that is not sane RSS."""
    if len(content) > MAX_FEED_BYTES:  # HttpClient already stops reading there; this guards other callers
        raise ItchFeedError(f"feed too large ({len(content)} bytes)")
    try:
        document = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ItchFeedError(f"feed is not UTF-8 ({exc.reason} at byte {exc.start})") from exc
    # Searched in the whole document, not just before the first "<rss" (a comment or PI may say
    # "<rss" before the DTD), and in the decoded text that expat parses below: a str is always
    # read as UTF-8 whatever the XML declaration says, so a UTF-16 body cannot hide a DOCTYPE
    # from this check.
    if _DECLARATION.search(document):
        raise ItchFeedError("refusing XML with a DOCTYPE/ENTITY declaration")
    try:
        root = ET.fromstring(document)
    except ET.ParseError as exc:
        raise ItchFeedError(f"malformed XML: {exc}") from exc
    if _local(root.tag) != "rss":
        raise ItchFeedError(f"not an RSS document (root element <{_local(root.tag)}>)")
    channel = next((child for child in root if _local(child.tag) == "channel"), None)
    if channel is None:
        raise ItchFeedError("RSS document has no <channel>")
    elements = [child for child in channel if _local(child.tag) == "item"]
    title = next((c.text or "" for c in channel if _local(c.tag) == "title"), "").strip()
    page = ItchPage(size=len(elements), title=title)
    seen: set[str] = set()
    for position, element in enumerate(elements[:MAX_ITEMS_PER_PAGE], start=1):
        item = parse_item(element, position=position, now=now)
        if item is None:
            page.skipped += 1
        elif item.key not in seen:  # a game listed twice on one page keeps its best position
            seen.add(item.key)
            page.items.append(item)
    return page


def looks_like_rss(body: bytes) -> bool:
    head = body[:512].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return head.startswith(b"<?xml") or b"<rss" in head


def classify_response(response: httpx.Response) -> str:
    """``rss`` | ``challenge`` | ``blocked`` | ``gone`` | ``unavailable`` | ``not_rss``."""
    status = response.status_code
    if response.headers.get("cf-mitigated", "").strip().lower() == "challenge":
        return "challenge"
    if status in (404, 410):
        return "gone"
    body = response.content
    if status == 200 and looks_like_rss(body):
        return "rss"
    head = body[:65536].lower()
    if any(marker in head for marker in CHALLENGE_MARKERS):
        return "challenge"
    if status == 403:
        return "blocked"
    if status == 200:
        return "not_rss"
    return "unavailable"


def feed_tags(url: str) -> list[str]:
    """Tag slugs in a browse URL: ``/games/new-and-popular/tag-co-op.xml`` -> ``["co-op"]``."""
    return _unique(slug.lower() for slug in _TAG_SEGMENT.findall(urlsplit(url).path))


def keyword_pattern(keywords: Iterable[str]) -> re.Pattern[str] | None:
    """Case-insensitive whole-word matcher for any of ``keywords`` (None when empty)."""
    words = sorted({k.strip().lower() for k in keywords if k.strip()}, key=len, reverse=True)
    if not words:
        return None
    alternation = "|".join(re.escape(word) for word in words)
    return re.compile(rf"(?<![a-z0-9])(?:{alternation})(?![a-z0-9])", re.I)


def item_matches(item: ItchItem, pattern: re.Pattern[str] | None) -> bool:
    """Heuristic tag match on the name, short text, genre token and URL slug."""
    if pattern is None:
        return True
    return bool(pattern.search(" ".join((item.name, item.text, item.genre or "", item.slug))))


def _is_itch_host(url: httpx.URL) -> bool:
    host = (url.host or "").lower()
    return host == "itch.io" or host.endswith(".itch.io")


# --------------------------------------------------------------------------------------
# Collector
# --------------------------------------------------------------------------------------


class ItchCollector(Collector):
    """Reads ``sources.itch.feeds`` (popular feed first), one Mention per game."""

    name: ClassVar[str] = "itch"

    def __init__(
        self,
        ctx: CollectContext,
        budget: Budget | None = None,
        *,
        sleep: Callable[[float], None] | None = None,
    ):
        super().__init__(ctx, budget)
        self.sleep = sleep if sleep is not None else ctx.http.sleep
        self._index: dict[str, Mention] = {}
        self._pages: dict[tuple[str, int], ItchPage | ItchFeedError] = {}  # (url, page) -> result
        self._requests = 0
        self._strikes = 0
        self._halt: str | None = None
        self._fallback_mode = False

    @property
    def settings(self) -> ItchSource:
        return self.config.sources.itch

    def enabled(self) -> tuple[bool, str | None]:
        if not self.settings.enabled:
            return False, "disabled in config/sources.yaml"
        if not self.settings.feeds:
            return False, "no itch feeds configured"
        return True, None

    def collect(self) -> list[Mention]:
        self._fallback_mode = self._fallback_hold_active()
        feeds = self._ordered_feeds()
        for index, feed in enumerate(feeds):
            if self._halt:
                rest = feeds[index:]
                names = ", ".join(f.name for f in rest)
                self.report.errors.append(
                    f"itch: {self._halt}; skipped {len(rest)} feed(s) this run: {names}"
                )
                self.log.warning("stopping: %s; skipped %s", self._halt, names)
                break
            with self.guard(f"itch:{feed.name}"):
                self._collect_feed(feed)
        return self.found

    # ---- feeds -------------------------------------------------------
    def _ordered_feeds(self) -> list[ItchFeed]:
        popular = self.settings.popular_feed
        return sorted(self.settings.feeds, key=lambda feed: feed.name != popular)

    def _collect_feed(self, feed: ItchFeed) -> None:
        fallback = feed.fallback_url
        if fallback and self._fallback_mode:
            self.log.info("%s: using fallback %s (a primary was challenged recently)", feed.name, fallback)
            self._collect_fallback(feed, fallback)
            return
        try:
            self._collect_pages(feed, feed.url, max_pages=feed.max_pages, ranked=feed.ranked)
        except (ItchBlocked, ItchFeedGone) as exc:
            if not fallback or self._halt:
                raise
            try:
                kept = self._collect_fallback(feed, fallback)
            except HttpError as fallback_exc:
                message = f"{exc}; fallback failed too: {fallback_exc}"
                raise ItchFeedError(message, fallback_exc.status, fallback_exc.url) from fallback_exc
            self.report.warnings.append(f"itch:{feed.name}: {exc}; used fallback {fallback} ({kept} game(s))")
            if isinstance(exc, ItchBlocked):
                self._start_fallback_hold()

    def _collect_fallback(self, feed: ItchFeed, url: str) -> int:
        """Page 1 of the fallback, plus any further pages of it already fetched this run (free)."""
        pages = 1
        while isinstance(self._pages.get((url, pages + 1)), ItchPage):
            pages += 1
        pattern = keyword_pattern(feed.fallback_keywords)
        return self._collect_pages(
            feed, url, max_pages=pages, ranked=feed.fallback_ranked, via_fallback=True, pattern=pattern
        )

    def _collect_pages(
        self,
        feed: ItchFeed,
        url: str,
        *,
        max_pages: int,
        ranked: bool,
        via_fallback: bool = False,
        pattern: re.Pattern[str] | None = None,
    ) -> int:
        """Read up to ``max_pages`` pages of ``url`` for ``feed``; returns the games kept."""
        listed: set[str] = set()  # game keys already taken from this feed in this run
        known = self.ctx.state.seen if self.ctx.state is not None else {}
        tags = feed_tags(feed.url)  # the feed's tags, also when reading a tag-less fallback
        kept = 0
        for page_no in range(1, max_pages + 1):
            try:
                page = self._fetch_page(url, page_no)
            except (ItchBlocked, ItchFeedGone) as exc:
                if page_no == 1:
                    raise
                if isinstance(exc, ItchBlocked):
                    self.report.warnings.append(f"itch:{feed.name}: page {page_no}: {exc}")
                break  # 404/410 past page 1 = end of the listing
            if page_no == 1 and page.size == 0:
                self.report.warnings.append(f"itch:{feed.name}: feed returned no items ({url})")
            new = [item for item in page.items if item.key not in listed]
            for item in new:
                if item_matches(item, pattern):
                    self._add(self._mention(feed, item, page, page_no, tags, ranked, via_fallback))
                    kept += 1
            listed.update(item.key for item in page.items)
            # Stop rules: short page, a repeated page (itch recycles pages past the end) or,
            # for unranked feeds, a page with nothing we have not seen in earlier runs.
            unseen = [item for item in new if f"itch:{item.key}" not in known]
            if page.size < PAGE_SIZE or not new or (not ranked and not unseen):
                break
        return kept

    def _fetch_page(self, url: str, page_no: int) -> ItchPage:
        """Fetch + parse one page; each (url, page) is requested at most once per run."""
        cached = self._pages.get((url, page_no))
        if isinstance(cached, ItchPage):
            return cached
        if cached is not None:
            raise cached
        try:
            page = self._request_page(url, page_no)
        except ItchFeedError as exc:
            self._pages[(url, page_no)] = exc
            raise
        self._pages[(url, page_no)] = page
        return page

    def _request_page(self, url: str, page_no: int) -> ItchPage:
        self._pace()
        params = {"page": str(page_no)} if page_no > 1 else None
        try:
            response = self.http.get(
                url,
                budget=self.budget,
                params=params,
                headers={"Accept": ACCEPT},
                expect=EXPECT,
                max_bytes=min(self.http.max_bytes, MAX_FEED_BYTES),  # streamed: abandoned past the cap
            )
        except RateLimited:
            self._halt = "rate limited by itch.io (HTTP 429)"
            raise
        final = response.url
        shown = str(final)
        if not _is_itch_host(final):
            raise ItchFeedError(f"redirected off itch.io to {shown}", response.status_code, shown)
        if response.history and page_no == 1:
            self.report.warnings.append(f"itch: {url} redirected to {shown}; update config/sources.yaml")
        kind = classify_response(response)
        status = response.status_code
        headers = response.headers
        self.log.info(
            "GET %s -> %s %s (%d bytes, cf-ray=%s, cf-cache-status=%s, etag=%s, last-modified=%s)",
            shown,
            status,
            kind,
            len(response.content),
            headers.get("cf-ray", "-"),
            headers.get("cf-cache-status", "-"),
            headers.get("etag", "-"),
            headers.get("last-modified", "-"),
        )
        if kind == "rss":
            return parse_feed(response.content, now=self.now)
        if kind in ("challenge", "blocked"):
            self._strikes += 1
            if self._strikes >= self.settings.max_challenges:
                self._halt = f"blocked by itch.io {self._strikes} times (Cloudflare challenge / HTTP 403)"
            what = "Cloudflare challenge" if kind == "challenge" else "blocked without a Cloudflare challenge"
            raise ItchBlocked(f"{what} (HTTP {status}) for {shown}", status, shown)
        if kind == "gone":
            message = (
                f"HTTP {status} for {shown}: feed not found, its URL in config/sources.yaml needs updating"
            )
            raise ItchFeedGone(message, status, shown)
        if kind == "unavailable":
            raise ItchFeedError(f"HTTP {status} for {shown} after retries", status, shown)
        content_type = headers.get("content-type", "no content-type")
        raise ItchFeedError(f"expected RSS, got {content_type} from {shown}", status, shown)

    def _pace(self) -> None:
        """Sleep ``request_interval_s`` between itch requests (not before the first one)."""
        interval = self.settings.request_interval_s
        if self._requests and interval > 0 and not self.budget.exhausted:
            self.sleep(interval)
        self._requests += 1

    # ---- fallback hold (persisted in scratch state) --------------------
    def _fallback_hold_active(self) -> bool:
        scratch = self.scratch()
        until = scratch.get("fallback_until")
        if not isinstance(until, str):
            return False
        try:
            until_at = ensure_utc(datetime.fromisoformat(until))
        except ValueError:
            until_at = self.now
        if self.now < until_at:
            self.log.info("fallback URLs in use until %s", until_at.isoformat())
            return True
        scratch.pop("fallback_until", None)
        return False

    def _start_fallback_hold(self) -> None:
        self._fallback_mode = True
        hours = self.settings.fallback_hold_hours
        if hours > 0:
            self.scratch()["fallback_until"] = (self.now + timedelta(hours=hours)).isoformat()

    # ---- mentions ------------------------------------------------------
    def _mention(
        self,
        feed: ItchFeed,
        item: ItchItem,
        page: ItchPage,
        page_no: int,
        tags: list[str],
        ranked: bool,
        via_fallback: bool,
    ) -> Mention:
        offset = (page_no - 1) * PAGE_SIZE
        rank = item.position + offset if ranked else None
        extra: dict[str, object] = {
            "itch_ranks": {feed.name: rank},
            "price": item.price,
            "price_value": item.price_value,
            "currency": item.currency,
            "is_free": item.is_free,
            "genre": item.genre,
            "updated_at": item.updated_at.isoformat() if item.updated_at else None,
            "platforms": list(item.platforms),
        }
        if via_fallback:
            extra["itch_fallback"] = [feed.name]
        return Mention(
            source="itch",
            source_id=item.key,
            url=item.url,
            title=item.name,
            text=item.text,
            author=item.dev,
            created_at=item.created_at,
            engagement=Engagement(),
            links=[item.url],
            media_thumb=item.image,
            raw_tags=_unique([item.genre, *tags, *item.platforms]),
            channel=f"itch:{feed.name}",
            rank=rank,
            list_size=offset + page.size if ranked else None,
            extra=extra,
        )

    def _add(self, mention: Mention) -> None:
        """Store a mention, or merge it into the one already found for the same game."""
        current = self._index.get(mention.source_id)
        if current is None:
            self._index[mention.source_id] = mention
            self.found.append(mention)
            return
        current.extra.setdefault("itch_ranks", {}).update(mention.extra["itch_ranks"])
        if "itch_fallback" in mention.extra:
            merged = [*current.extra.get("itch_fallback", []), *mention.extra["itch_fallback"]]
            current.extra["itch_fallback"] = _unique(merged)
        current.raw_tags = _unique([*current.raw_tags, *mention.raw_tags])
        current.media_thumb = current.media_thumb or mention.media_thumb
        current.text = current.text or mention.text
        if self._prefer(mention, current):
            current.channel, current.rank, current.list_size = (
                mention.channel,
                mention.rank,
                mention.list_size,
            )

    def _prefer(self, new: Mention, current: Mention) -> bool:
        """The popular feed's position wins, otherwise the best (lowest) rank."""
        popular = f"itch:{self.settings.popular_feed}"
        if current.channel == popular:
            return False
        if new.channel == popular:
            return True
        if new.rank is None:
            return False
        return current.rank is None or new.rank < current.rank
