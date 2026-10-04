"""A small, polite HTTP layer shared by every collector and the Discord client.

* every request is charged to a per-source :class:`Budget` (hard cap per run);
* a descriptive User-Agent is always sent;
* 429 responses are retried after ``Retry-After`` / ``retry_after`` (bounded sleep);
* 5xx / transport errors get a short bounded retry;
* conditional GETs (ETag / Last-Modified) use a cache dict that the pipeline persists in state.

Tests inject an ``httpx`` transport (respx or ``httpx.MockTransport``) and a fake ``sleep``.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from gembot.models import HttpCacheEntry

log = logging.getLogger(__name__)


class BudgetExceeded(RuntimeError):
    """Raised when a source tries to make more requests than its per-run budget allows."""


class HttpError(RuntimeError):
    """A request failed for good (after retries). ``status`` is None for transport errors."""

    def __init__(
        self,
        message: str,
        status: int | None = None,
        url: str | None = None,
        headers: httpx.Headers | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.url = url
        self.headers = headers  # response headers, when there was a response (e.g. rate-limit resets)


class RateLimited(HttpError):
    """429 whose requested wait is longer than we are willing to sleep, or retries ran out."""


@dataclass
class Budget:
    name: str
    limit: int
    used: int = 0
    # Optional wall-clock cut-off (``time.monotonic()`` value): the pipeline sets it so slow
    # sources stop early and posting/saving always get their share of the job's time limit.
    deadline: float | None = None

    @property
    def remaining(self) -> int:
        return 0 if self.out_of_time else max(self.limit - self.used, 0)

    @property
    def out_of_time(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit or self.out_of_time

    def take(self) -> None:
        if self.used >= self.limit:
            raise BudgetExceeded(f"{self.name}: request budget of {self.limit} used up for this run")
        if self.out_of_time:
            raise BudgetExceeded(f"{self.name}: out of time for this run (other steps still need the time)")
        self.used += 1


_EPOCH_CUTOFF = 1_000_000_000  # larger "reset" values are unix timestamps, smaller ones are seconds


def _retry_after_seconds(response: httpx.Response, now_ts: float | None = None) -> float | None:
    """How long a 429 asks us to wait, from (in order) ``Retry-After``, a JSON ``retry_after``
    (Discord), or a rate-limit reset header (Reddit ``x-ratelimit-reset`` = seconds,
    Bluesky ``ratelimit-reset`` / X ``x-rate-limit-reset`` = unix time)."""
    header = response.headers.get("retry-after")
    if header:
        try:
            return _finite_wait(float(header))
        except ValueError:
            pass
        try:  # RFC 9110 also allows an HTTP-date
            when = parsedate_to_datetime(header)
        except (TypeError, ValueError, IndexError):
            when = None
        if when is not None and now_ts is not None:
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            return max(when.timestamp() - now_ts, 0.0)
    try:
        body = response.json()
    except Exception:
        body = None
    if isinstance(body, dict) and "retry_after" in body:
        try:
            return _finite_wait(float(body["retry_after"]))
        except (TypeError, ValueError):
            pass
    for name in ("x-ratelimit-reset", "ratelimit-reset", "x-rate-limit-reset"):
        raw = response.headers.get(name)
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if not math.isfinite(value):
            return math.inf
        if value > _EPOCH_CUTOFF:
            if now_ts is None:
                continue
            value -= now_ts
        return max(value, 0.0)
    return None


def _finite_wait(value: float) -> float:
    """NaN/inf/negative waits are nonsense: treat them as "too long" (raise RateLimited)."""
    if not math.isfinite(value):
        return math.inf
    return max(value, 0.0)


@dataclass
class HttpClient:
    user_agent: str
    timeout: float = 15.0
    retries: int = 2
    max_backoff_s: float = 20.0
    transport: httpx.BaseTransport | None = None
    sleep: Callable[[float], None] = time.sleep
    cache: MutableMapping[str, HttpCacheEntry] = field(default_factory=dict)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    max_bytes: int = 10 * 1024 * 1024  # largest response body we are willing to read
    deadline_s: float | None = None  # per request, whole response; default 2 x timeout
    max_redirects: int = 5
    _client: httpx.Client | None = field(default=None, init=False, repr=False)

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout,
                follow_redirects=False,  # followed by hand so every hop is budgeted
                transport=self.transport,
                headers={"User-Agent": self.user_agent},
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        *,
        budget: Budget,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json: Any = None,
        data: Any = None,
        auth: Any = None,
        conditional: bool = False,
        follow_redirects: bool = True,
        expect: tuple[int, ...] = (200,),
        retries: int | None = None,
        max_bytes: int | None = None,
    ) -> httpx.Response:
        """Send a request, charging ``budget`` for every attempt (and every redirect hop).

        Returns the response when its status is in ``expect`` (or 304 for conditional
        requests). Raises :class:`BudgetExceeded`, :class:`RateLimited` or :class:`HttpError`.
        ``retries`` overrides the client's retry count for this call (``0`` = never retry,
        e.g. for hosts that escalate to blocks when you retry a 429). The body is streamed and
        abandoned past ``max_bytes`` or past the per-request deadline, so a huge or trickling
        response can't stall a run.
        """
        max_retries = self.retries if retries is None else max(retries, 0)
        hdrs = dict(headers or {})
        # copy_merge_params keeps the URL's own query (httpx.URL(url, params=None) drops it,
        # which would make every ``feed.xml?channel_id=...`` share one cache entry)
        cache_key = str(httpx.URL(url).copy_merge_params(params or {})) if conditional else None
        if cache_key and cache_key in self.cache:
            entry = self.cache[cache_key]
            if entry.etag:
                hdrs["If-None-Match"] = entry.etag
            if entry.last_modified:
                hdrs["If-Modified-Since"] = entry.last_modified

        attempt = 0
        while True:
            budget.take()
            attempt += 1
            try:
                request = self.client.build_request(
                    method, url, params=params, headers=hdrs, json=json, data=data
                )
                response = self._send(request, budget, auth, follow_redirects, max_bytes)
            except (BudgetExceeded, HttpError):
                raise
            except httpx.HTTPError as exc:
                retryable = not isinstance(exc, httpx.LocalProtocolError | httpx.UnsupportedProtocol)
                if retryable and attempt <= max_retries:
                    self.sleep(min(2.0 * attempt, self.max_backoff_s))
                    continue
                raise HttpError(f"{budget.name}: {_describe(exc)} for {_short(url)}", url=url) from exc

            status = response.status_code
            if status == 429:
                wait = _retry_after_seconds(response, self.clock().timestamp())
                wait = 2.0 * attempt if wait is None else wait
                if attempt > max_retries or wait > self.max_backoff_s:
                    raise RateLimited(
                        f"{budget.name}: rate limited (429), retry after {wait:.1f}s",
                        429,
                        url,
                        response.headers,
                    )
                log.info("%s: 429, sleeping %.1fs", budget.name, wait)
                self.sleep(wait)
                continue
            if status >= 500 and attempt <= max_retries:
                self.sleep(min(2.0 * attempt, self.max_backoff_s))
                continue
            if status == 304 and conditional:
                return response
            if status in expect:
                if cache_key and (response.headers.get("etag") or response.headers.get("last-modified")):
                    self.cache[cache_key] = HttpCacheEntry(
                        etag=response.headers.get("etag"),
                        last_modified=response.headers.get("last-modified"),
                        at=self.clock(),
                    )
                return response
            raise HttpError(f"{budget.name}: HTTP {status} for {_short(url)}", status, url, response.headers)

    def _send(
        self,
        request: httpx.Request,
        budget: Budget,
        auth: Any,
        follow_redirects: bool,
        max_bytes: int | None,
    ) -> httpx.Response:
        """Send one request (plus up to ``max_redirects`` budgeted redirect hops) and read the
        body under a size cap and a deadline. Returns a fully-read response."""
        limit = self.max_bytes if max_bytes is None else max_bytes
        started = time.monotonic()
        deadline = started + (self.deadline_s if self.deadline_s is not None else 2.0 * self.timeout)
        history: list[httpx.Response] = []
        while True:
            response = self.client.send(request, auth=auth, stream=True, follow_redirects=False)
            try:
                if follow_redirects and response.is_redirect and response.next_request is not None:
                    if len(history) >= self.max_redirects:
                        raise HttpError(
                            f"{budget.name}: too many redirects for {_short(str(request.url))}",
                            response.status_code,
                            str(request.url),
                        )
                    history.append(response)
                    budget.take()  # every hop is a real request to someone's server
                    request = response.next_request
                    continue
                body = self._read_body(response, budget, limit, deadline)
            finally:
                response.close()
            headers = httpx.Headers(
                [(k, v) for k, v in response.headers.multi_items() if k.lower() not in _BODY_HEADERS]
            )
            final = httpx.Response(
                response.status_code,
                headers=headers,
                content=body,
                request=response.request,
                extensions=response.extensions,
            )
            final.history = history
            return final

    def _read_body(self, response: httpx.Response, budget: Budget, limit: int, deadline: float) -> bytes:
        url = str(response.request.url)
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise HttpError(
                f"{budget.name}: response too large ({int(declared)} bytes) from {_short(url)}",
                response.status_code,
                url,
            )
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > limit:
                raise HttpError(
                    f"{budget.name}: response larger than {limit} bytes from {_short(url)}",
                    response.status_code,
                    url,
                )
            if time.monotonic() > deadline:
                raise HttpError(
                    f"{budget.name}: response too slow from {_short(url)}", response.status_code, url
                )
            chunks.append(chunk)
        return b"".join(chunks)

    def get(self, url: str, *, budget: Budget, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, budget=budget, **kwargs)

    def get_json(self, url: str, *, budget: Budget, **kwargs: Any) -> Any:
        response = self.get(url, budget=budget, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            message = f"{budget.name}: invalid JSON from {_short(url)}"
            raise HttpError(message, response.status_code, url) from exc

    def post(self, url: str, *, budget: Budget, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, budget=budget, **kwargs)


# Headers that describe the wire encoding of the body we already decoded and re-wrapped.
_BODY_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})


def _describe(exc: httpx.HTTPError) -> str:
    """Transport errors without their message for protocol errors: h11/httpcore put offending
    header *values* (e.g. a malformed ``Authorization: Bearer <token>``) into the text."""
    if isinstance(exc, httpx.ProtocolError):
        return type(exc).__name__
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _short(url: str, limit: int = 120) -> str:
    return url if len(url) <= limit else url[: limit - 1] + "…"
