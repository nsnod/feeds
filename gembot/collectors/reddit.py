"""Reddit collector: new posts from game-dev / indie subreddits.

Engine and dev subreddits are where small games show up before any announcement, so
this collector reads ``/new`` (and ``/rising``) of every configured subreddit as ONE
multireddit request (``r/A+B+C``) per listing.

Access (Reddit, as of October 2026; see docs/VERIFICATION.md):

* **OAuth** when ``REDDIT_CLIENT_ID`` / ``REDDIT_CLIENT_SECRET`` are set: an app-only
  (client_credentials) token, then ``oauth.reddit.com`` listings with full numbers
  (score, comments, upvote ratio, subscribers) and top comments for enrichment.
  Self-service API apps were closed on 2025-11-11, so new credentials need Reddit's
  approval; the public Data API itself is scheduled to end around March 2027.
* **Anonymous RSS** otherwise: ONE combined ``/new/.rss`` request per run (Reddit allows
  roughly one RSS request per minute per IP, and GitHub runners share IPs). RSS has no
  scores or comment counts, so mentions carry ``extra["engagement_known"] = False``.
  Reddit retires RSS on 2026-11-13 (``sources.reddit.rss_retirement_date``); after that
  the collector is skipped until API credentials are added.
* Anonymous ``.json`` has answered 403 "blocked by network security" from every network
  since 2026-05-29. It is only tried when ``sources.reddit.try_public_json`` is true.

A 403 HTML page is Reddit's IP block, never a credentials problem; the error says so.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import feedparser
import httpx

from gembot import __version__
from gembot.collectors.base import Collector
from gembot.http import HttpError, RateLimited
from gembot.models import Comment, Engagement, Mention

if TYPE_CHECKING:
    from gembot.collectors.base import CollectContext
    from gembot.config import RedditSource
    from gembot.http import Budget

WWW = "https://www.reddit.com"
OAUTH_API = "https://oauth.reddit.com"
TOKEN_URL = f"{WWW}/api/v1/access_token"

MODE_OAUTH = "oauth"
MODE_ANONYMOUS = "anonymous"  # optional public .json, then RSS
MODE_OFF = "off"

MAX_LISTING_LIMIT = 100  # Reddit caps listings at 100 items per request
MAX_COMMENT_LIMIT = 500
RATE_LIMIT_FLOOR = 5.0  # stop when fewer OAuth requests than this are left in the window
BAD_SUBREDDIT_SKIP_HOURS = 24  # private / banned / missing subreddits are left out this long

BAD_CREDENTIALS = "Reddit rejected REDDIT_CLIENT_ID/SECRET"
IP_BLOCKED = "Reddit blocked this runner's IP (not a credentials problem)"
RSS_RATE_LIMITED = "reddit RSS rate-limited this run"

_REDIRECTS = (301, 302, 303, 307, 308)
_REDDIT_HOSTS = ("reddit.com", "redd.it", "redditmedia.com", "redditstatic.com", "reddituploads.com")
_BAD_SUB_REASONS = ("private", "banned", "quarantined", "gold_only")
_DELETED = ("[deleted]", "[removed]")

# Markdown link targets ``[text](url)`` (one level of balanced parentheses) and bare URLs.
_MD_LINK_RE = re.compile(r"\]\(\s*<?(https?://(?:[^\s()<>]|\([^\s()<>]*\))+)>?\s*(?:\"[^\"]*\"\s*)?\)")
_BARE_URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"'`|]+")
_TRAILING_PUNCT = ".,;:!?*'\"~"
_SELF_TEXT_RE = re.compile(r"<!--\s*SC_OFF\s*-->(.*?)<!--\s*SC_ON\s*-->", re.S)
_MD_DIV_RE = re.compile(r"<div class=\"md\">(.*?)</div>\s*(?:&#32;|submitted by)", re.S)
_FOOTER_LINK_RE = re.compile(r"<a\s[^>]*href=\"([^\"]+)\"[^>]*>\s*\[link\]\s*</a>")
_HREF_RE = re.compile(r"href=\"([^\"]+)\"")
_BLOCK_END_RE = re.compile(r"(?i)<br\s*/?>|</(?:p|div|li|h[1-6]|blockquote|pre|tr|table)>")
_TAG_RE = re.compile(r"<[^>]+>")


class RedditBlocked(HttpError):
    """Reddit served its 403 "blocked by network security" page (an IP block)."""


class SubredditUnavailable(HttpError):
    """A subreddit in the request is private, banned, quarantined or does not exist."""


class RedditCollector(Collector):
    name = "reddit"

    def __init__(self, ctx: CollectContext, budget: Budget | None = None):
        super().__init__(ctx, budget)
        self._mode: str | None = None
        self._token: str | None = None
        self._auth_error: HttpError | None = None  # credentials failed for good this run
        self._stop_reason: str | None = None  # no more Reddit requests this run (IP block, rate limit)
        self._by_key: dict[str, Mention] = {}

    # ------------------------------------------------------------------ setup
    @property
    def cfg(self) -> RedditSource:
        return self.config.sources.reddit

    @property
    def user_agent(self) -> str:
        owner = self.config.secrets.reddit_username or "gembot"
        return f"python:gembot:{__version__} (by /u/{owner})"

    @property
    def mode(self) -> str:
        """``oauth`` with API credentials, ``anonymous`` (RSS / opt-in .json) or ``off``."""
        if self._mode is None:
            if self.config.secrets.has_reddit_oauth:
                self._mode = MODE_OAUTH
            elif self._rss_available() or self.cfg.try_public_json:
                self._mode = MODE_ANONYMOUS
            else:
                self._mode = MODE_OFF
        return self._mode

    def _rss_available(self) -> bool:
        return self.now.date() < self.cfg.rss_retirement_date

    def enabled(self) -> tuple[bool, str | None]:
        if not self.cfg.enabled:
            return False, "disabled in sources.yaml"
        if not self.cfg.subreddits:
            return False, "no subreddits configured in sources.yaml"
        if self.mode == MODE_OFF:
            retired = self.cfg.rss_retirement_date.isoformat()
            return False, f"Reddit RSS was retired on {retired}; add Reddit API credentials (see README)"
        return True, None

    def _announce_mode(self) -> None:
        if self.mode == MODE_OAUTH:
            self.log.info("reddit: using the OAuth API (app-only token)")
            return
        if self._rss_available():
            retired = self.cfg.rss_retirement_date.isoformat()
            message = (
                f"reddit: using anonymous RSS (no engagement numbers; Reddit retires RSS on {retired})"
                " — add REDDIT_CLIENT_ID/SECRET for full data"
            )
        else:
            message = "reddit: RSS is retired; trying the anonymous .json listing (blocked since May 2026)"
        self.report.warnings.append(message)
        self.log.warning(message)

    # ------------------------------------------------------------------ collect
    def collect(self) -> list[Mention]:
        self._announce_mode()
        if self.mode == MODE_OAUTH:
            self._collect_oauth()
        else:
            self._collect_anonymous()
        return self.found

    def _collect_oauth(self) -> None:
        try:
            self._ensure_token()
        except HttpError as exc:
            self.report.failed_units += 1
            self.report.errors.append(f"reddit token: {exc}")
            self.log.warning("reddit token: %s", exc)
            return
        for listing in dict.fromkeys(self.cfg.listings):
            if self._stop_reason:
                break
            subs = self._active_subreddits()
            if not subs:
                self.report.warnings.append("reddit: every configured subreddit is unavailable right now")
                break
            with self.guard(f"reddit/{listing}"):
                posts = self._oauth_listing(subs, listing)
                self._add_posts(posts, listing)
                if listing == "new":
                    self._remember_newest(posts)

    def _collect_anonymous(self) -> None:
        subs = self._active_subreddits()
        if not subs:
            self.report.warnings.append("reddit: every configured subreddit is unavailable right now")
            return
        rss = self._rss_available()
        if self.cfg.try_public_json:
            if not rss:
                with self.guard("reddit .json"):
                    self._collect_public_json(subs)
                return
            try:
                self._collect_public_json(subs)
            except (HttpError, ValueError) as exc:
                message = f"reddit public .json failed ({exc}); falling back to RSS"
                self.report.warnings.append(message)
                self.log.warning(message)
            else:
                self.report.ok_units += 1
                return
        self._collect_rss(subs)

    # ------------------------------------------------------------------ OAuth
    def _ensure_token(self) -> str:
        if self._token:
            return self._token
        secrets = self.config.secrets
        response = self.http.post(
            TOKEN_URL,
            budget=self.budget,
            data={"grant_type": "client_credentials"},
            auth=(secrets.reddit_client_id or "", secrets.reddit_client_secret or ""),
            headers={"User-Agent": self.user_agent},
            expect=(200, 401, 403),
            retries=1,
        )
        status = response.status_code
        if status == 401:
            self._auth_error = HttpError(f"{BAD_CREDENTIALS} (HTTP 401)", 401, TOKEN_URL)
            raise self._auth_error
        if status == 403:
            if _is_html(response):
                self._stop_reason = IP_BLOCKED
                raise RedditBlocked(IP_BLOCKED, 403, TOKEN_URL)
            self._auth_error = HttpError(
                "Reddit refused the token request (HTTP 403); the app may not be approved for API access",
                403,
                TOKEN_URL,
            )
            raise self._auth_error
        payload = _json_or_none(response)
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not token:
            detail = payload.get("error") if isinstance(payload, dict) else None
            if detail:  # e.g. {"error": "invalid_grant"} with HTTP 200
                self._auth_error = HttpError(f"{BAD_CREDENTIALS} ({detail})", status, TOKEN_URL)
            else:
                self._auth_error = HttpError("Reddit token response had no access_token", status, TOKEN_URL)
            raise self._auth_error
        self._token = str(token)
        return self._token

    def _api_get(self, url: str, params: dict[str, Any]) -> Any:
        """GET an oauth.reddit.com path; refreshes the token once on 401. Returns parsed JSON."""
        response = self._api_send(url, params)
        if response.status_code == 401:
            self._token = None
            response = self._api_send(url, params)
            if response.status_code == 401:
                raise HttpError("Reddit API answered 401 even with a fresh token", 401, url)
        self._note_rate_limit(response)
        status = response.status_code
        if status == 200:
            payload = _json_or_none(response)
            if payload is None:
                raise HttpError(f"invalid JSON from {_path(url)}", status, url)
            return payload
        if status == 403 and _is_html(response):
            self._stop_reason = IP_BLOCKED
            raise RedditBlocked(IP_BLOCKED, 403, url)
        body = _json_or_none(response)
        reason = body.get("reason") if isinstance(body, dict) else None
        if status in _REDIRECTS:
            raise SubredditUnavailable(
                f"{_path(url)}: HTTP {status}, a subreddit does not exist (Reddit redirected to search)",
                status,
                url,
            )
        if status == 404 or reason in _BAD_SUB_REASONS:
            raise SubredditUnavailable(f"{_path(url)}: HTTP {status} ({reason or 'not found'})", status, url)
        raise HttpError(f"{_path(url)}: HTTP {status}{f' ({reason})' if reason else ''}", status, url)

    def _api_send(self, url: str, params: dict[str, Any]) -> httpx.Response:
        token = self._ensure_token()
        return self.http.get(
            url,
            budget=self.budget,
            params=params,
            headers={"Authorization": f"bearer {token}", "User-Agent": self.user_agent},
            follow_redirects=False,
            expect=(200, 401, 403, 404, *_REDIRECTS),
        )

    def _note_rate_limit(self, response: httpx.Response) -> None:
        raw = response.headers.get("x-ratelimit-remaining")
        if raw is None or self._stop_reason:
            return
        try:
            remaining = float(raw)
        except ValueError:
            return
        if remaining >= RATE_LIMIT_FLOOR:
            return
        reset = response.headers.get("x-ratelimit-reset")
        when = f", resets in {reset}s" if reset else ""
        self._stop_reason = (
            f"reddit: API rate limit almost used up ({remaining:g} requests left{when}); "
            "no more Reddit requests this run"
        )
        self.report.warnings.append(self._stop_reason)
        self.log.warning(self._stop_reason)

    def _oauth_listing(self, subs: list[str], listing: str) -> list[dict[str, Any]]:
        """One multireddit listing; if a subreddit in it is unavailable, check them one by one."""
        try:
            return self._oauth_pages(subs, listing)
        except SubredditUnavailable as exc:
            if len(subs) == 1:
                self._exclude(subs[0], exc)
                return []
            message = f"reddit/{listing}: {exc}; checking the subreddits one by one"
            self.report.warnings.append(message)
            self.log.warning(message)
        posts: list[dict[str, Any]] = []
        for sub in subs:
            if self._stop_reason:
                break
            with self.guard(f"r/{sub}/{listing}"):
                posts.extend(self._probe_subreddit(sub, listing))
        return posts

    def _probe_subreddit(self, sub: str, listing: str) -> list[dict[str, Any]]:
        try:
            return self._oauth_pages([sub], listing, max_pages=1)
        except SubredditUnavailable as exc:
            self._exclude(sub, exc)
            return []

    def _oauth_pages(
        self, subs: list[str], listing: str, max_pages: int | None = None
    ) -> list[dict[str, Any]]:
        limit = max(1, min(self.cfg.limit, MAX_LISTING_LIMIT))
        pages = max(1, self.cfg.new_max_pages) if listing == "new" else 1
        if max_pages is not None:
            pages = min(pages, max_pages)
        url = f"{OAUTH_API}/r/{'+'.join(subs)}/{listing}"
        seen_until = self._newest_seen()
        posts: list[dict[str, Any]] = []
        after: str | None = None
        for _page in range(pages):
            params: dict[str, Any] = {"limit": limit, "raw_json": 1}
            if after:
                params["after"] = after
            children, after = _listing_children(self._api_get(url, params))
            page_posts = _t3_data(children)
            posts.extend(page_posts)
            # Only follow `after` while a full page is still all newer than what we saw last run.
            times = [_float(p.get("created_utc")) for p in page_posts if not p.get("stickied")]
            if (
                not after
                or len(children) < limit
                or self._stop_reason
                or not times
                or min(times) <= seen_until
            ):
                break
        return posts

    def _newest_seen(self) -> float:
        """Epoch of the newest /new post seen last run (or the age cutoff on a first run)."""
        cutoff = (self.now - timedelta(hours=self.cfg.max_post_age_hours)).timestamp()
        return max(_float(self.scratch().get("newest_created_utc")), cutoff)

    def _remember_newest(self, posts: list[dict[str, Any]]) -> None:
        times = [_float(p.get("created_utc")) for p in posts if not p.get("stickied")]
        if times:
            scratch = self.scratch()
            scratch["newest_created_utc"] = max(max(times), _float(scratch.get("newest_created_utc")))

    # ------------------------------------------------------------------ subreddit list
    def _active_subreddits(self) -> list[str]:
        excluded: dict[str, str] = self.scratch().get("excluded") or {}
        for key, until in list(excluded.items()):
            try:
                expired = datetime.fromisoformat(until) <= self.now
            except (TypeError, ValueError):
                expired = True
            if expired:
                del excluded[key]
        subs: dict[str, str] = {}
        for sub in self.cfg.subreddits:
            name = sub.strip().removeprefix("r/").removeprefix("/r/").strip("/")
            if name and name.lower() not in excluded:
                subs.setdefault(name.lower(), name)
        return list(subs.values())

    def _exclude(self, sub: str, exc: Exception) -> None:
        until = self.now + timedelta(hours=BAD_SUBREDDIT_SKIP_HOURS)
        self.scratch().setdefault("excluded", {})[sub.lower()] = until.isoformat()
        message = (
            f"r/{sub} is unavailable ({exc}); skipping it for {BAD_SUBREDDIT_SKIP_HOURS}h. "
            "Remove it from sources.yaml if this keeps happening"
        )
        self.report.warnings.append(message)
        self.log.warning(message)

    # ------------------------------------------------------------------ anonymous
    def _anon_headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agent}

    def _collect_public_json(self, subs: list[str]) -> None:
        url = f"{WWW}/r/{'+'.join(subs)}/new.json"
        response = self.http.get(
            url,
            budget=self.budget,
            params={"limit": max(1, min(self.cfg.rss_limit, MAX_LISTING_LIMIT)), "raw_json": 1},
            headers=self._anon_headers(),
            follow_redirects=False,
            retries=0,  # retrying a blocked host only makes it worse
            expect=(200, 403),
        )
        if response.status_code == 403:
            if _is_html(response):
                raise RedditBlocked(IP_BLOCKED + " for anonymous .json", 403, url)
            raise HttpError(f"{_path(url)}: HTTP 403", 403, url)
        payload = _json_or_none(response)
        if payload is None:
            raise HttpError(f"invalid JSON from {_path(url)}", response.status_code, url)
        children, _after = _listing_children(payload)
        self._add_posts(_t3_data(children), "new")

    def _collect_rss(self, subs: list[str]) -> None:
        url = f"{WWW}/r/{'+'.join(subs)}/new/.rss"
        try:
            response = self.http.get(
                url,
                budget=self.budget,
                params={"limit": max(1, min(self.cfg.rss_limit, MAX_LISTING_LIMIT))},
                headers=self._anon_headers(),
                retries=1,  # one retry on 429 (Retry-After, bounded); RSS allows ~1 request/min per IP
                expect=(200, 403, 404),
            )
        except RateLimited as exc:
            self.report.failed_units += 1
            message = f"{RSS_RATE_LIMITED} (GitHub runners share IPs; ~1 RSS request/min allowed): {exc}"
            self.report.warnings.append(message)
            self.log.warning(message)
            return
        except HttpError as exc:
            self.report.failed_units += 1
            self.report.errors.append(f"reddit RSS: {exc}")
            self.log.warning("reddit RSS: %s", exc)
            return
        with self.guard("reddit RSS"):
            if response.status_code != 200 or _looks_like_html(response):
                if _is_block_page(response):
                    raise RedditBlocked(IP_BLOCKED, response.status_code, url)
                raise HttpError(
                    f"{_path(url)}: HTTP {response.status_code}, not an RSS feed "
                    "(a subreddit in sources.yaml may be private, banned or misspelled)",
                    response.status_code,
                    url,
                )
            self._add_rss(response.content)

    def _add_rss(self, content: bytes) -> None:
        feed = feedparser.parse(content)
        if not feed.entries and feed.bozo:
            raise ValueError(f"malformed RSS feed: {feed.get('bozo_exception')}")
        bad = 0
        for entry in feed.entries:
            try:
                mention = self._rss_to_mention(entry)
            except Exception as exc:  # one odd entry must not lose the rest
                bad += 1
                self.log.debug("reddit RSS entry skipped: %s: %s", type(exc).__name__, exc)
                continue
            if mention is not None:
                self._add(mention, "new")
        if bad:
            self.report.warnings.append(
                f"reddit RSS: skipped {bad} malformed entr{'y' if bad == 1 else 'ies'}"
            )

    def _rss_to_mention(self, entry: Any) -> Mention | None:
        entry_id = str(entry.get("id") or "")
        if not entry_id.startswith("t3_"):
            return None
        created = _parse_time(entry.get("published") or entry.get("updated"))
        if created is None or created < self._cutoff():
            return None
        permalink = str(entry.get("link") or "")
        contents = entry.get("content") or []
        body = contents[0].get("value", "") if contents else str(entry.get("summary") or "")
        self_html = _self_text_html(body)
        text = _html_to_text(self_html)
        footer = _FOOTER_LINK_RE.search(body)
        target = html.unescape(footer.group(1)) if footer else None
        is_self = target is None or _same_url(target, permalink)
        candidates = [target] if target and not is_self else []
        candidates += [html.unescape(h) for h in _HREF_RE.findall(self_html)]
        candidates += _text_urls(text)
        tags = entry.get("tags") or []
        sub = str(tags[0].get("term") or "") if tags else ""
        if not sub:
            sub = _subreddit_from_permalink(permalink)
        author = str(entry.get("author") or "").strip().removeprefix("/u/").removeprefix("u/") or None
        thumbs = entry.get("media_thumbnail") or []
        thumb = thumbs[0].get("url") if thumbs else None
        if is_self:
            domain = f"self.{sub}"
        else:
            domain = (urlsplit(target or "").hostname or "").lower()
        return Mention(
            source="reddit",
            source_id=entry_id[3:],
            url=permalink or f"{WWW}/comments/{entry_id[3:]}/",
            title=html.unescape(str(entry.get("title") or "")),
            text=text,
            author=author,
            author_audience=self._fallback_audience(sub),
            created_at=created,
            engagement=Engagement(),
            links=_clean_links(candidates),
            media_thumb=thumb or None,
            raw_tags=[],
            channel=f"r/{sub}" if sub else None,
            extra={"name": entry_id, "is_self": is_self, "domain": domain, "engagement_known": False},
        )

    # ------------------------------------------------------------------ listing JSON -> Mention
    def _add_posts(self, posts: list[dict[str, Any]], listing: str) -> None:
        bad = 0
        for data in posts:
            try:
                mention = self._post_to_mention(data)
            except Exception as exc:  # one odd post must not lose the listing
                bad += 1
                self.log.debug("reddit post skipped: %s: %s", type(exc).__name__, exc)
                continue
            if mention is not None:
                self._add(mention, listing)
        if bad:
            self.report.warnings.append(
                f"reddit/{listing}: skipped {bad} malformed post{'' if bad == 1 else 's'}"
            )

    def _add(self, mention: Mention, listing: str) -> None:
        existing = self._by_key.get(mention.key)
        if existing is None:
            mention.extra["listings"] = [listing]
            self._by_key[mention.key] = mention
            self.found.append(mention)
            return
        if listing not in existing.extra["listings"]:
            existing.extra["listings"].append(listing)
        existing.engagement = mention.engagement  # the later request has the fresher numbers

    def _cutoff(self) -> datetime:
        return self.now - timedelta(hours=self.cfg.max_post_age_hours)

    def _fallback_audience(self, sub: str) -> int | None:
        wanted = sub.lower()
        for name, count in self.cfg.fallback_subscribers.items():
            if name.lower().removeprefix("r/") == wanted:
                return int(count)
        return None

    def _post_to_mention(self, data: dict[str, Any]) -> Mention | None:
        if data.get("stickied") or data.get("over_18"):
            return None
        created = _from_epoch(data.get("created_utc"))
        if created is None or created < self._cutoff():
            return None
        post_id = str(data.get("id") or str(data.get("name") or "").removeprefix("t3_"))
        if not post_id:
            return None
        permalink = str(data.get("permalink") or "")
        url = WWW + permalink if permalink.startswith("/") else (permalink or f"{WWW}/comments/{post_id}/")
        sub = str(data.get("subreddit") or "") or _subreddit_from_permalink(url)
        selftext = html.unescape(str(data.get("selftext") or ""))
        is_self = bool(data.get("is_self"))
        candidates: list[str] = []
        if not is_self and data.get("url"):
            candidates.append(html.unescape(str(data["url"])))
        candidates += _text_urls(selftext)
        author = data.get("author")
        subscribers = data.get("subreddit_subscribers")
        audience = int(subscribers) if isinstance(subscribers, int | float) else self._fallback_audience(sub)
        flair = data.get("link_flair_text")
        ratio = data.get("upvote_ratio")
        return Mention(
            source="reddit",
            source_id=post_id,
            url=url,
            title=html.unescape(str(data.get("title") or "")),
            text=selftext,
            author=str(author) if author and author != "[deleted]" else None,
            author_audience=audience,
            created_at=created,
            engagement=Engagement(
                likes=int(data.get("score") or 0),
                comments=int(data.get("num_comments") or 0),
                ratio=float(ratio) if isinstance(ratio, int | float) else None,
            ),
            links=_clean_links(candidates),
            media_thumb=_thumbnail(data),
            raw_tags=[str(flair).strip()] if flair and str(flair).strip() else [],
            channel=f"r/{sub}" if sub else None,
            extra={
                "name": str(data.get("name") or f"t3_{post_id}"),
                "is_self": is_self,
                "domain": data.get("domain"),
                "is_video": bool(data.get("is_video")),
            },
        )

    # ------------------------------------------------------------------ comments
    def fetch_comments(self, mention: Mention, limit: int) -> list[Comment]:
        """Top-level comments, best first. OAuth only: anonymous comment JSON is blocked and
        RSS comment feeds are flat and would spend the ~1 request/min RSS allowance."""
        if mention.source != "reddit" or self.mode != MODE_OAUTH:
            return []
        if self._stop_reason or self._auth_error is not None or limit <= 0:
            return []
        limit = min(limit, MAX_COMMENT_LIMIT)
        post_id = mention.source_id.removeprefix("t3_")
        payload = self._api_get(
            f"{OAUTH_API}/comments/{post_id}",
            {"sort": "top", "limit": limit, "depth": 1, "raw_json": 1},
        )
        return parse_comments(payload, limit)


# ---------------------------------------------------------------------- parsing helpers


def parse_comments(payload: Any, limit: int) -> list[Comment]:
    """``/comments/<id>`` -> top-level ``t1`` comments (skips ``more`` and deleted/removed)."""
    if not isinstance(payload, list) or len(payload) < 2:
        raise ValueError("unexpected comments payload (expected [post listing, comment listing])")
    children, _after = _listing_children(payload[1])
    comments: list[Comment] = []
    for child in children:
        if not isinstance(child, dict) or child.get("kind") != "t1":
            continue
        data = child.get("data") or {}
        author = str(data.get("author") or "")
        body = html.unescape(str(data.get("body") or "")).strip()
        if not body or body in _DELETED:
            continue
        is_bot = author == "AutoModerator" or (
            data.get("distinguished") == "moderator" and bool(data.get("stickied"))
        )
        comments.append(
            Comment(
                id=str(data.get("id") or ""),
                author=author,
                text=body,
                score=int(data.get("score") or 0),
                created_at=_from_epoch(data.get("created_utc")),
                is_bot=is_bot,
            )
        )
        if len(comments) >= limit:
            break
    return comments


def _listing_children(payload: Any) -> tuple[list[Any], str | None]:
    data = payload.get("data") if isinstance(payload, dict) else None
    children = data.get("children") if isinstance(data, dict) else None
    if not isinstance(children, list):
        raise ValueError("unexpected Reddit listing shape (no data.children)")
    after = data.get("after")
    return children, str(after) if after else None


def _t3_data(children: list[Any]) -> list[dict[str, Any]]:
    return [
        c["data"]
        for c in children
        if isinstance(c, dict) and c.get("kind") == "t3" and isinstance(c.get("data"), dict)
    ]


def _thumbnail(data: dict[str, Any]) -> str | None:
    images = (data.get("preview") or {}).get("images") or []
    if images and isinstance(images[0], dict):
        source = (images[0].get("source") or {}).get("url")
        if source:
            return html.unescape(str(source))
    thumb = str(data.get("thumbnail") or "")
    return html.unescape(thumb) if thumb.startswith(("http://", "https://")) else None


def _text_urls(text: str) -> list[str]:
    """URLs in markdown or plain text, in order: ``[t](url)`` targets and bare URLs."""
    found: list[tuple[int, str]] = []
    spans: list[tuple[int, int]] = []
    for match in _MD_LINK_RE.finditer(text):
        found.append((match.start(1), match.group(1)))
        spans.append(match.span(1))
    for match in _BARE_URL_RE.finditer(text):
        if not any(start <= match.start() < end for start, end in spans):
            found.append((match.start(), match.group(0)))
    return [url for _pos, url in sorted(found)]


def _clean_links(candidates: list[str]) -> list[str]:
    """Normalize, drop Reddit-internal URLs (permalinks, v.redd.it, i.redd.it, ...) and dedupe."""
    links: dict[str, None] = {}
    for raw in candidates:
        url = raw.strip().replace("\\", "").rstrip(_TRAILING_PUNCT)
        if not url.startswith(("http://", "https://")) or _is_reddit_url(url):
            continue
        links.setdefault(url, None)
    return list(links)


def _is_reddit_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in _REDDIT_HOSTS)


def _same_url(a: str, b: str) -> bool:
    def norm(url: str) -> str:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().removeprefix("www.").removeprefix("old.")
        return f"{host}{parts.path.rstrip('/')}"

    return norm(a) == norm(b)


def _subreddit_from_permalink(url: str) -> str:
    match = re.search(r"/r/([^/]+)/", url)
    return match.group(1) if match else ""


def _self_text_html(content: str) -> str:
    match = _SELF_TEXT_RE.search(content) or _MD_DIV_RE.search(content)
    return match.group(1) if match else ""


def _html_to_text(fragment: str) -> str:
    text = _BLOCK_END_RE.sub("\n", fragment)
    text = html.unescape(_TAG_RE.sub("", text))
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _from_epoch(value: Any) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return datetime.fromtimestamp(float(value), UTC)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _json_or_none(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _is_html(response: httpx.Response) -> bool:
    return "html" in response.headers.get("content-type", "").lower() or _is_block_page(response)


def _looks_like_html(response: httpx.Response) -> bool:
    head = response.content[:512].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html")) or "text/html" in response.headers.get(
        "content-type", ""
    )


def _is_block_page(response: httpx.Response) -> bool:
    return "network security" in response.text[:4000].lower()


def _path(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path if len(parts.path) <= 80 else parts.path[:79] + "…"
    return f"{parts.hostname}{path}"
