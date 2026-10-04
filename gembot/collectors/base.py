"""Collector protocol: per-run request budget and error isolation.

A collector turns one platform into :class:`~gembot.models.Mention` objects. The
rules every collector follows:

* All HTTP goes through ``self.http`` with ``budget=self.budget`` (hard cap per run).
* :meth:`Collector.run` never raises. A broken source returns whatever it managed to
  collect (often nothing) and records the error in its :class:`SourceReport`.
* Inside :meth:`collect`, wrap each independent unit of work (one subreddit listing,
  one search term, one feed) in ``with self.guard("label"):`` so one failure does not
  lose the rest. ``BudgetExceeded`` is re-raised by ``guard`` to stop the loop early;
  ``run`` turns it into a warning and keeps the partial results.
* Append results to ``self.found`` as you go (and ``return self.found``) so partial
  results survive a budget stop or an unexpected crash.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar

from gembot.http import Budget, BudgetExceeded, HttpClient, HttpError
from gembot.models import Comment, Mention

if TYPE_CHECKING:
    from gembot.config import Config
    from gembot.models import State


@dataclass
class FeedResult:
    """How one ``config/feeds.yaml`` entry did this run (the smoke summary's "Your feeds" table)."""

    feed: str
    platform: str
    status: str  # ok | warning | error | paused | skipped | config (a mistake in feeds.yaml)
    items: int = 0
    note: str = ""


@dataclass
class SourceReport:
    source: str
    requests: int = 0
    mentions: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False  # disabled or missing credentials: not a failure
    skip_reason: str | None = None
    ok_units: int = 0  # units of work (feeds, subreddits, searches) that succeeded
    failed_units: int = 0
    config_errors: int = 0  # how many of ``errors`` are mistakes in the user's config/ files
    feed_results: list[FeedResult] = field(default_factory=list)  # RSS only: one row per feed

    @property
    def ok(self) -> bool:
        """A run is healthy if it was skipped on purpose or produced no hard errors.

        Partial failures (e.g. 1 of 11 subreddits failed) still count as OK as long as
        at least one unit succeeded; see ``failed_units``/``ok_units``. A mistake in the
        config (``config_errors``) never fixes itself, so it is not OK even then: after a
        few runs the pipeline posts it to the status channel.
        """
        if self.skipped:
            return True
        if self.config_errors:
            return False
        if not self.errors:
            return True
        return self.ok_units > 0

    def summary(self) -> str:
        if self.skipped:
            return f"{self.source}: skipped ({self.skip_reason})"
        state = "ok" if self.ok else "FAILED"
        extra = f"; {len(self.errors)} error(s)" if self.errors else ""
        return f"{self.source}: {state}, {self.mentions} mentions, {self.requests} requests{extra}"


@dataclass
class CollectContext:
    """Everything a collector may use. ``state`` is read-only for collectors."""

    config: Config
    http: HttpClient
    now: datetime
    state: State | None = None
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("gembot.collectors"))


class Collector(ABC):
    """Base class for every source. Subclasses set ``name`` and implement ``collect``."""

    name: ClassVar[str] = "base"

    def __init__(self, ctx: CollectContext, budget: Budget | None = None):
        self.ctx = ctx
        self.config = ctx.config
        self.http = ctx.http
        self.now = ctx.now
        self.log = ctx.log.getChild(self.name)
        self.budget = budget or Budget(self.name, ctx.config.settings.budgets.for_source(self.name))
        self.report = SourceReport(self.name)
        self.found: list[Mention] = []

    # ---- hooks -------------------------------------------------------
    def enabled(self) -> tuple[bool, str | None]:
        """Return (enabled, reason-if-not). Override to check config flags / credentials."""
        return True, None

    @abstractmethod
    def collect(self) -> list[Mention]:
        """Fetch and parse mentions. May raise; :meth:`run` isolates failures."""

    def fetch_comments(self, mention: Mention, limit: int) -> list[Comment]:
        """Top comments / replies for one of this source's mentions (enrichment stage)."""
        return []

    def fetch_audience(self, mention: Mention) -> int | None:
        """Author follower count / channel audience, if the platform exposes it."""
        return None

    # ---- helpers -----------------------------------------------------
    def scratch(self) -> dict:
        """Mutable per-collector scratch dict persisted in state (``meta.collector_state[name]``).

        Use it for small things like pagination cursors. Without state it is a throwaway dict.
        """
        if self.ctx.state is None:
            if not hasattr(self, "_scratch"):
                self._scratch: dict = {}
            return self._scratch
        return self.ctx.state.meta.collector_state.setdefault(self.name, {})

    @contextmanager
    def guard(self, label: str) -> Iterator[None]:
        """Isolate one unit of work: record its error and carry on with the next unit."""
        try:
            yield
        except BudgetExceeded:
            raise
        except HttpError as exc:
            self.report.failed_units += 1
            self.report.errors.append(f"{label}: {exc}")
            self.log.warning("%s: %s", label, exc)
        except Exception as exc:  # parsing bugs, malformed payloads, ...
            self.report.failed_units += 1
            self.report.errors.append(f"{label}: {type(exc).__name__}: {exc}")
            self.log.warning("%s failed: %s: %s", label, type(exc).__name__, exc)
        else:
            self.report.ok_units += 1

    # ---- entry points used by the pipeline ----------------------------
    def run(self) -> tuple[list[Mention], SourceReport]:
        on, reason = self.enabled()
        if not on:
            self.report.skipped = True
            self.report.skip_reason = reason
            self.log.info("skipped: %s", reason)
            return [], self.report
        mentions: list[Mention] = []
        try:
            mentions = self.collect() or []
        except BudgetExceeded as exc:
            self.report.warnings.append(str(exc))
            mentions = list(self.found)
        except Exception as exc:
            self.report.errors.append(f"{type(exc).__name__}: {exc}")
            self.report.failed_units += 1
            self.log.warning("collector crashed: %s: %s", type(exc).__name__, exc)
            mentions = list(self.found)
        # de-duplicate by key, keep first occurrence (e.g. same post in /new and /rising)
        unique: dict[str, Mention] = {}
        for mention in mentions:
            unique.setdefault(mention.key, mention)
        self.report.mentions = len(unique)
        self.report.requests = self.budget.used
        return list(unique.values()), self.report

    def safe_fetch_comments(self, mention: Mention, limit: int) -> list[Comment]:
        try:
            return self.fetch_comments(mention, limit)
        except Exception as exc:
            self.report.warnings.append(f"comments for {mention.key}: {exc}")
            return []
        finally:
            self.report.requests = self.budget.used

    def safe_fetch_audience(self, mention: Mention) -> int | None:
        try:
            return self.fetch_audience(mention)
        except Exception as exc:
            self.report.warnings.append(f"audience for {mention.key}: {exc}")
            return None
        finally:
            self.report.requests = self.budget.used
