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
import time
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
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

    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit

    def take(self) -> None:
        if self.used >= self.limit:
            raise BudgetExceeded(f"{self.name}: request budget of {self.limit} used up for this run")
        self.used += 1


_EPOCH_CUTOFF = 1_000_000_000  # larger "reset" values are unix timestamps, smaller ones are seconds


def _retry_after_seconds(response: httpx.Response, now_ts: float | None = None) -> float | None:
    """How long a 429 asks us to wait, from (in order) ``Retry-After``, a JSON ``retry_after``
    (Discord), or a rate-limit reset header (Reddit ``x-ratelimit-reset`` = seconds,
    Bluesky ``ratelimit-reset`` / X ``x-rate-limit-reset`` = unix time)."""
    header = response.headers.get("retry-after")
    if header:
        try:
            return max(float(header), 0.0)
        except ValueError:
            pass
    try:
        body = response.json()
    except Exception:
        body = None
    if isinstance(body, dict) and "retry_after" in body:
        try:
            return max(float(body["retry_after"]), 0.0)
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
        if value > _EPOCH_CUTOFF:
            if now_ts is None:
                continue
            value -= now_ts
        return max(value, 0.0)
    return None


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
    _client: httpx.Client | None = field(default=None, init=False, repr=False)

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout,
                follow_redirects=True,
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
    ) -> httpx.Response:
        """Send a request, charging ``budget`` for every attempt.

        Returns the response when its status is in ``expect`` (or 304 for conditional
        requests). Raises :class:`BudgetExceeded`, :class:`RateLimited` or :class:`HttpError`.
        ``retries`` overrides the client's retry count for this call (``0`` = never retry,
        e.g. for hosts that escalate to blocks when you retry a 429).
        """
        max_retries = self.retries if retries is None else max(retries, 0)
        hdrs = dict(headers or {})
        cache_key = str(httpx.URL(url, params=params)) if conditional else None
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
                response = self.client.request(
                    method,
                    url,
                    params=params,
                    headers=hdrs,
                    json=json,
                    data=data,
                    auth=auth,
                    follow_redirects=follow_redirects,
                )
            except httpx.HTTPError as exc:
                if attempt <= max_retries:
                    self.sleep(min(2.0 * attempt, self.max_backoff_s))
                    continue
                raise HttpError(f"{budget.name}: {type(exc).__name__}: {exc}", url=url) from exc

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


def _short(url: str, limit: int = 120) -> str:
    return url if len(url) <= limit else url[: limit - 1] + "…"
