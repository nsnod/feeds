"""X (Twitter) collector: API v2 recent search. Optional, and paid.

Runs only when the ``X_BEARER_TOKEN`` secret is set. Since February 2026 new developers
get no free tier: the X API is pay-per-use with prepaid credits (roughly $0.005 per post
returned and $0.010 per user returned by the author expansion), capped by a spending limit
in the X Developer Console. Everything here keeps that bill small and predictable:

* one request per query per run: ``retries=0`` (a 429 never loops) and ``next_token`` is
  never followed;
* ``since_id`` per query (kept in :meth:`Collector.scratch`) so a post is paid for once;
  without one the search looks back 24 hours with ``start_time`` (never both);
* ``@username`` + follower count (``expansions=author_id``) only with ``x.expand_authors``;
* a monthly counter of returned posts + users pauses the collector for the rest of the
  calendar month (UTC) once ``x.monthly_read_budget`` is reached.

Failures become plain-language errors, matched on HTTP status + the problem ``reason``
(never on the free-text ``detail``). A rejected token (401), used-up credits (402), an app
that is not enrolled (403) or a rate limit (429) also skip the remaining queries.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from gembot.collectors.base import Collector
from gembot.http import HttpError, RateLimited
from gembot.models import Engagement, Mention, ensure_utc

if TYPE_CHECKING:
    from gembot.config import XSource

SEARCH_PATH = "/tweets/search/recent"
TWEET_FIELDS = "created_at,public_metrics,entities,author_id,conversation_id,lang,note_tweet"
USER_FIELDS = "username,name,public_metrics"
MAX_QUERY_CHARS = 512
MIN_RESULTS, MAX_RESULTS = 10, 100
LOOKBACK = timedelta(hours=24)  # start_time window when there is no since_id yet
TITLE_CHARS = 120
SNOWFLAKE_EPOCH_MS = 1288834974657
SELF_HOSTS = ("x.com", "twitter.com")
# 429 is not listed: HttpClient raises RateLimited for it before looking at ``expect``.
EXPECTED_STATUSES = (200, 400, 401, 402, 403)


class XApiError(HttpError):
    """A mapped X API failure. ``fatal`` ones (token, credits, enrolment, rate limit) end the run."""

    def __init__(self, message: str, status: int | None, *, fatal: bool = False):
        super().__init__(message, status)
        self.fatal = fatal


class XCollector(Collector):
    name = "x"

    _rate_headers: httpx.Headers | None = None

    @property
    def settings(self) -> XSource:
        return self.config.sources.x

    # ---- hooks -------------------------------------------------------
    def enabled(self) -> tuple[bool, str | None]:
        if not self.config.secrets.x_bearer_token:
            return False, "X_BEARER_TOKEN not set (the X API is paid; see README)"
        if not self.settings.enabled:
            return False, "turned off in config/sources.yaml (x.enabled: false)"
        if not self.settings.queries:
            return False, "no search queries in config/sources.yaml (x.queries)"
        if self.monthly_remaining() == 0:
            return False, self._monthly_message()
        return True, None

    def collect(self) -> list[Mention]:
        queries = self.settings.queries
        self._prune_since_ids(queries)
        with self._watch_rate_limit_headers():
            for query in queries:
                label = f'query "{_clip(query, 40)}"'
                if not query.strip():
                    continue
                if len(query) > MAX_QUERY_CHARS:
                    self._fail(
                        label,
                        f"query is {len(query)} characters but X allows at most {MAX_QUERY_CHARS}; "
                        "skipped (shorten it in config/sources.yaml)",
                    )
                    continue
                if self.monthly_remaining() == 0:
                    self.report.warnings.append(f"{self._monthly_message()}; remaining queries skipped")
                    break
                fatal = False
                with self.guard(label):
                    try:
                        self._search(query, label)
                    except XApiError as exc:
                        fatal = exc.fatal
                        raise
                if fatal:
                    break
        return self.found

    # ---- one search --------------------------------------------------
    def _search(self, query: str, label: str) -> None:
        key = query_key(query)
        since_ids: dict[str, str] = self.scratch().setdefault("since_id", {})
        since_id = since_ids.get(key)
        response = self._request(query, since_id)
        if response.status_code == 400 and since_id and _blames_since_id(_json_or_none(response)):
            # since_id older than the 7-day search window: drop it and look back with start_time once
            since_ids.pop(key, None)
            self.report.warnings.append(
                f"{label}: X refused the saved since_id (older than 7 days?); retried once with start_time"
            )
            response = self._request(query, None)
        if response.status_code != 200:
            raise _api_error(response)

        body = response.json()  # invalid JSON -> ValueError -> recorded by guard
        if not isinstance(body, dict):
            raise ValueError(f"unexpected X response ({type(body).__name__}, expected an object)")
        meta = _dict(body.get("meta"))
        users = {
            str(user["id"]): user
            for user in _list(_dict(body.get("includes")).get("users"))
            if isinstance(user, dict) and user.get("id")
        }
        posts = _list(body.get("data"))
        result_count = _int(meta.get("result_count"), default=len(posts))
        self._add_reads(result_count + len(users))  # billed whether or not we can parse them

        problems = [item for item in _list(body.get("errors")) if isinstance(item, dict)]
        if problems:
            self.report.warnings.append(
                f"{label}: X returned {len(problems)} partial error(s), e.g. {_problem_text(problems[0])}"
            )
        skipped = 0
        for post in posts:
            try:
                mention = post_to_mention(post, users) if isinstance(post, dict) else None
            except Exception as exc:  # one odd post must not cost us the rest (or the since_id)
                self.log.debug("unparseable post: %s: %s", type(exc).__name__, exc)
                mention = None
            if mention is None:
                skipped += 1
            else:
                self.found.append(mention)
        if skipped:
            self.report.warnings.append(f"{label}: skipped {skipped} unparseable post(s)")
        newest_id = meta.get("newest_id")
        if result_count > 0 and newest_id:
            since_ids[key] = str(newest_id)

    def _request(self, query: str, since_id: str | None) -> httpx.Response:
        token = self.config.secrets.x_bearer_token
        try:
            return self.http.request(
                "GET",
                self.settings.api_base.rstrip("/") + SEARCH_PATH,
                budget=self.budget,
                params=self.search_params(query, since_id),
                headers={"Authorization": f"Bearer {token}"},
                expect=EXPECTED_STATUSES,
                retries=0,
            )
        except RateLimited as exc:
            raise XApiError(self._rate_limit_message(), 429, fatal=True) from exc
        except HttpError as exc:
            if exc.status is not None and exc.status >= 500:
                raise XApiError(
                    f"X API server error (HTTP {exc.status}); skipped this run", exc.status
                ) from exc
            raise

    def search_params(self, query: str, since_id: str | None = None) -> dict[str, str | int]:
        """Query string for one recent search (``since_id`` or ``start_time``, never both)."""
        params: dict[str, str | int] = {
            "query": query,
            "max_results": self.max_results(),
            "sort_order": self.settings.sort_order,
            "tweet.fields": TWEET_FIELDS,
        }
        if self.settings.expand_authors:
            params["expansions"] = "author_id"
            params["user.fields"] = USER_FIELDS
        if since_id:
            params["since_id"] = since_id
        else:
            params["start_time"] = f"{ensure_utc(self.now - LOOKBACK):%Y-%m-%dT%H:%M:%SZ}"
        return params

    def max_results(self) -> int:
        """``x.max_results`` clamped to 10..100, and to what is left of the monthly budget."""
        count = min(max(self.settings.max_results, MIN_RESULTS), MAX_RESULTS)
        remaining = self.monthly_remaining()
        if remaining is not None:
            count = max(MIN_RESULTS, min(count, remaining))
        return count

    # ---- monthly cost guard ------------------------------------------
    def _month(self) -> str:
        return f"{ensure_utc(self.now):%Y-%m}"

    def monthly_reads(self) -> int:
        """Posts + users X returned to us this calendar month (UTC)."""
        reads = self.scratch().get("reads")
        return _int(reads.get(self._month())) if isinstance(reads, dict) else 0

    def monthly_remaining(self) -> int | None:
        limit = self.settings.monthly_read_budget
        if limit is None:
            return None
        return max(limit - self.monthly_reads(), 0)

    def _add_reads(self, count: int) -> None:
        # only the current month is kept; older months fall away on the first read of a new month
        self.scratch()["reads"] = {self._month(): self.monthly_reads() + max(count, 0)}

    def _monthly_message(self) -> str:
        return (
            f"monthly X read budget used up ({self.monthly_reads()}/{self.settings.monthly_read_budget} "
            f"posts+users in {self._month()}); raise x.monthly_read_budget in config/sources.yaml "
            "or wait for next month"
        )

    # ---- helpers -----------------------------------------------------
    def _prune_since_ids(self, queries: list[str]) -> None:
        since_ids = self.scratch().get("since_id")
        if isinstance(since_ids, dict):
            keep = {query_key(query) for query in queries}
            for key in [k for k in since_ids if k not in keep]:
                del since_ids[key]

    def _fail(self, label: str, message: str) -> None:
        self.report.failed_units += 1
        self.report.errors.append(f"{label}: {message}")
        self.log.warning("%s: %s", label, message)

    @contextmanager
    def _watch_rate_limit_headers(self) -> Iterator[None]:
        """Remember the latest search response headers while collecting.

        ``HttpClient`` turns a 429 into ``RateLimited`` without exposing the response, so a
        response hook on the shared client is how we read ``x-rate-limit-reset``.
        """

        def hook(response: httpx.Response) -> None:  # installed only while this collector runs
            self._rate_headers = response.headers

        hooks = self.http.client.event_hooks["response"]
        hooks.append(hook)
        try:
            yield
        finally:
            hooks.remove(hook)

    def _rate_limit_message(self) -> str:
        reset = _opt_int(self._rate_headers.get("x-rate-limit-reset")) if self._rate_headers else None
        if reset is None:
            when = "reset time unknown"
        else:
            at = datetime.fromtimestamp(reset, UTC)
            minutes = max(math.ceil((at - self.now).total_seconds() / 60), 0)
            when = f"the limit resets at {at:%Y-%m-%d %H:%M} UTC (in {minutes} min)"
        return f"X rate limit reached (HTTP 429); skipped this run, {when}"


# --------------------------------------------------------------------------------------
# parsing (pure functions)
# --------------------------------------------------------------------------------------


def query_key(query: str) -> str:
    """Short stable key for a query's ``since_id`` in scratch (queries can be 512 chars)."""
    return hashlib.sha256(query.encode("utf-8")).hexdigest()[:12]


def post_to_mention(
    post: Mapping[str, Any], users: Mapping[str, Mapping[str, Any]] | None = None
) -> Mention | None:
    """One v2 post (+ ``includes.users`` by id) as a Mention; None without a usable id/time.

    Accepts both vocabularies: ``retweet_count`` / ``repost_count``, and ignores
    ``edit_history_tweet_ids`` / ``edit_history_post_ids``.
    """
    post_id = str(post.get("id") or "").strip()
    if not post_id:
        return None
    created_at = _created_at(post.get("created_at"), post_id)
    if created_at is None:
        return None
    author_id = str(post.get("author_id") or "").strip() or None
    user = _dict((users or {}).get(author_id)) if author_id else {}
    username = str(user.get("username") or "").strip() or None
    note = _dict(post.get("note_tweet"))
    text = str(note.get("text") or post.get("text") or "")
    metrics = _dict(post.get("public_metrics"))
    reposts = _int(metrics["retweet_count"] if "retweet_count" in metrics else metrics.get("repost_count"))
    entities = [_dict(post.get("entities")), _dict(note.get("entities"))]
    extra = {
        "impressions": _opt_int(metrics.get("impression_count")),
        "conversation_id": post.get("conversation_id"),
    }
    return Mention(
        source="x",
        source_id=post_id,
        url=f"https://x.com/{username}/status/{post_id}"
        if username
        else f"https://x.com/i/web/status/{post_id}",
        title=_title(text),
        text=text,
        author=username or author_id,
        author_audience=_opt_int(_dict(user.get("public_metrics")).get("followers_count")),
        created_at=created_at,
        engagement=Engagement(
            likes=_int(metrics.get("like_count")),
            comments=_int(metrics.get("reply_count")),
            shares=reposts + _int(metrics.get("quote_count")),
        ),
        links=_links(entities),
        raw_tags=_hashtags(entities),
        channel="x",
        extra={k: v for k, v in extra.items() if v is not None},
    )


def _links(entities: list[dict[str, Any]]) -> list[str]:
    """Outbound URLs (fully unwound when X did that for us), minus links back to X itself."""
    links: list[str] = []
    seen: set[str] = set()  # t.co + resolved forms: note_tweet repeats the post's own URL entities
    for group in entities:
        for item in _list(group.get("urls")):
            if not isinstance(item, dict):
                continue
            url = item.get("unwound_url") or item.get("expanded_url") or item.get("url")
            if not isinstance(url, str) or not url.strip():
                continue
            url = url.strip()
            short = str(item.get("url") or url).strip()
            if url in seen or short in seen:
                continue
            seen.update((url, short))
            host = (urlsplit(url).hostname or "").lower()
            if not any(host == own or host.endswith("." + own) for own in SELF_HOSTS):
                links.append(url)
    return links


def _hashtags(entities: list[dict[str, Any]]) -> list[str]:
    tags: list[str] = []
    for group in entities:
        for item in _list(group.get("hashtags")):
            tag = str(item.get("tag") or "").strip().lower() if isinstance(item, dict) else ""
            if tag and tag not in tags:
                tags.append(tag)
    return tags


def _title(text: str) -> str:
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    return line if len(line) <= TITLE_CHARS else line[: TITLE_CHARS - 1].rstrip() + "…"


def _created_at(value: Any, post_id: str) -> datetime | None:
    if isinstance(value, str):
        try:
            return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            pass
    if post_id.isdigit():  # snowflake ids carry their creation time
        return datetime.fromtimestamp(((int(post_id) >> 22) + SNOWFLAKE_EPOCH_MS) / 1000, UTC)
    return None


def _api_error(response: httpx.Response) -> XApiError:
    status = response.status_code
    body = _dict(_json_or_none(response))
    reason = str(body.get("reason") or "").strip()
    if status == 401:
        return XApiError(
            "X rejected X_BEARER_TOKEN (HTTP 401); check the secret or regenerate the Bearer Token "
            "in the X Developer Console",
            status,
            fatal=True,
        )
    if status == 402:
        return XApiError(
            "X API credits used up — buy credits / raise the spending limit in the X Developer Console (HTTP 402)",
            status,
            fatal=True,
        )
    if status == 403:
        if reason == "client-not-enrolled":
            return XApiError(
                "X refused the app (HTTP 403 client-not-enrolled): put the app in the Pay-per-use package, "
                "Production environment, in the X Developer Console",
                status,
                fatal=True,
            )
        return XApiError(
            f"X refused the request (HTTP 403 {reason or 'forbidden'}): {_problem_text(body)}",
            status,
            fatal=True,
        )
    return XApiError(f"X refused the query (HTTP {status}): {_problem_text(body)}", status)


def _blames_since_id(body: Any) -> bool:
    """A 400 is about ``since_id`` unless X names other parameters only."""
    named = {
        str(name)
        for item in _list(_dict(body).get("errors"))
        for name in _dict(_dict(item).get("parameters"))
    }
    return not named or "since_id" in named


def _problem_text(problem: Mapping[str, Any]) -> str:
    messages = [
        str(item.get("message") or item.get("detail"))
        for item in _list(problem.get("errors"))
        if isinstance(item, dict) and (item.get("message") or item.get("detail"))
    ]
    text = "; ".join(messages[:2]) or str(
        problem.get("detail") or problem.get("message") or problem.get("title") or "no details"
    )
    return _clip(text, 300)


def _json_or_none(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _opt_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any, default: int = 0) -> int:
    number = _opt_int(value)
    return default if number is None else number


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
