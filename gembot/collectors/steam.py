"""Steam store collector (no API key needed).

Discovery
    ``store.steampowered.com/search/results/?infinite=1`` returns a JSON envelope
    ``{success, results_html, total_count, start}`` whose ``results_html`` holds rendered
    ``<a class="search_result_row">`` rows (some IPs get the bare HTML instead, so a body
    that is not JSON is parsed as HTML). Every configured search adds its own params
    (``filter=comingsoon`` / ``popularcomingsoon``, ``tags=492,3843``, ...) on top of
    ``category1=998`` (games only), ``untags=128`` (no MMOs), ``ignore_preferences=1``,
    ``ndl=1``, ``cc`` and ``l=english``.

    * "comingsoon" lists hold ~2000 games sorted by release window, so a brand new page
      can land anywhere. Each run reads ``max_pages`` pages from a rotating cursor kept in
      ``self.scratch()["cursor:<search name>"]``; the cursor wraps to 0 at ``total_count``.
    * "popular*" lists always read from page 1 because the position matters: every row
      gets ``Mention.rank`` (1-based) and ``list_size``. Steam's popular-upcoming list is
      driven by wishlists/follows, so its rank is the Steam velocity signal.
    * ``api/featuredcategories`` (``coming_soon`` + ``new_releases``, apps only) is a
      small secondary seed.

Enrichment
    ``api/appdetails?appids=<id>`` takes ONE id per request (several ids only work with
    ``filters=price_overview``). Since late September 2026 the outer keys of the response
    drift (it is often keyed by another id), so they are treated as opaque: only an entry
    whose ``data.steam_appid`` equals the requested id is accepted. When the only data
    belongs to another app (``requested->returned`` alias, e.g. 730->2678630) the app is
    kept without details and not asked about again for 7 days.
    ``success: false`` means invalid, region-locked or Adult Only for anonymous callers:
    the app is dropped and remembered in ``scratch()["skip"]`` for 7 days, like non-games
    and MMOs (genre 29 / category 20, on top of ``untags=128``). ``data: []`` means "no
    details": the app is kept without ``extra["steam"]``. ``data-ds-tagids`` on search
    rows is only a short top-N hint, so it feeds ``raw_tags`` but never filters anything.
    Stored details younger than ``appdetails_refresh_hours`` are reused from state; at
    most ``max_new_apps_per_run`` appdetails calls are made per run (ranked apps first,
    then new apps, then stale ones). Apps over the cap are emitted without fresh details
    and the pipeline enriches them later via :meth:`SteamCollector.fetch_appdetails`.

Politeness
    store.steampowered.com tolerates about 200 requests per 5 minutes per IP, so
    consecutive requests are spaced by ``request_interval_s`` (2 s) with an injectable
    ``sleep``. Steam requests are sent with ``retries=0``: the FIRST 429 (pressing on turns
    into Akamai 403 blocks that last ~5 minutes) or 403 stops the Steam stage for this run,
    the error is recorded and everything collected so far is kept.

Followers
    Not collected. There is no keyless follower API: a hub's follower count is only shown
    to its developers, and ``steamcommunity.com/games/<appid>/memberslistxml`` belongs to
    the deprecated community XML API. ``sources.steam.track_followers`` stays ``false`` and
    nothing is scraped; the popular-upcoming rank stands in for follower growth.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from selectolax.lexbor import LexborHTMLParser, LexborNode

from gembot.collectors.base import CollectContext, Collector
from gembot.config import SteamSearch
from gembot.http import Budget, BudgetExceeded, HttpError, RateLimited
from gembot.models import Mention, SteamInfo, ensure_utc

STORE = "https://store.steampowered.com"
SEARCH_URL = f"{STORE}/search/results/"
FEATURED_URL = f"{STORE}/api/featuredcategories"
APPDETAILS_URL = f"{STORE}/api/appdetails"
HEADERS = {"Accept-Language": "en-US,en;q=0.9"}
GAMES_CATEGORY = 998  # search category1: games only
UNAVAILABLE_RETRY = timedelta(days=7)
MAX_SKIP_ENTRIES = 500
MMO_GENRE_ID = 29  # appdetails genre "Massively Multiplayer"
MMO_CATEGORY_ID = 20  # appdetails category "MMO"
FEATURED_LISTS = (("coming_soon", "featured-coming-soon"), ("new_releases", "featured-new-releases"))

# Steam user-tag id -> name (store.steampowered.com/tagdata/populartags/english, checked 2026-10-04).
TAG_NAMES: dict[int, str] = {
    19: "Action",
    128: "Massively Multiplayer",
    492: "Indie",
    745697: "Social Deduction",
    1667: "Horror",
    1685: "Co-op",
    1719: "Comedy",
    1721: "Psychological Horror",
    1775: "PvP",
    3841: "Local Co-Op",
    3843: "Online Co-Op",
    3859: "Multiplayer",
    3968: "Physics",
    3978: "Survival Horror",
    4136: "Funny",
    4508: "Co-op Campaign",
    4840: "4 Player Local",
    5711: "Team-Based",
    7108: "Party",
    7178: "Party Game",
    7368: "Local Multiplayer",
    10816: "Split Screen",
}

_SKIP_NAME = re.compile(r"\b(playtest|demo|soundtrack|ost)\b", re.IGNORECASE)
_LEADING_INT = re.compile(r"\s*(\d+)")
_FULL_MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)  # fmt: skip
_MONTHS = tuple(month[:3] for month in _FULL_MONTHS)
_MONTH_DAY_YEAR = re.compile(r"^([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})$")  # "Oct 15, 2026"
_DAY_MONTH_YEAR = re.compile(r"^(\d{1,2})\s+([A-Za-z]+)\.?,?\s+(\d{4})$")  # "15 Oct, 2026"


class AppUnavailable(Exception):
    """appdetails has no data for this app.

    Either ``success: false`` (invalid, region-locked or adult-only for anonymous users) or
    only another app's data came back; then ``alias`` is that app's id.
    """

    def __init__(self, message: str, *, alias: int | None = None):
        super().__init__(message)
        self.alias = alias


# --------------------------------------------------------------------------------------
# Pure parsing helpers
# --------------------------------------------------------------------------------------


@dataclass
class SearchRow:
    appid: int
    name: str
    release_text: str | None = None
    tag_ids: list[int] = field(default_factory=list)
    capsule: str | None = None


@dataclass
class SearchPage:
    rows: list[SearchRow]
    raw_rows: int  # every result row, including bundles/packages/playtests that were skipped
    total_count: int | None


def store_url(appid: int) -> str:
    return f"{STORE}/app/{appid}/"


def is_skipped_name(name: str | None) -> bool:
    """Playtests, demos and soundtracks are not games we want to report."""
    return bool(name) and bool(_SKIP_NAME.search(name or ""))


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _month(word: str) -> int | None:
    word = word.lower()
    if word == "sept":
        return 9
    if word in _MONTHS:
        return _MONTHS.index(word) + 1
    if word in _FULL_MONTHS:
        return _FULL_MONTHS.index(word) + 1
    return None


def parse_release_date(text: str | None) -> date | None:
    """Exact dates ("Oct 15, 2026", "15 Oct, 2026", "October 15, 2026") -> date.

    Months ("October 2026"), quarters ("Q1 2027"), years ("2027"), "Coming soon" and
    "To be announced" are not exact and give ``None``.
    """
    if not text:
        return None
    clean = " ".join(text.split())
    if match := _MONTH_DAY_YEAR.match(clean):
        month, day, year = _month(match[1]), int(match[2]), int(match[3])
    elif match := _DAY_MONTH_YEAR.match(clean):
        day, month, year = int(match[1]), _month(match[2]), int(match[3])
    else:
        return None
    if month is None:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def strip_html(text: str | None) -> str:
    """Plain text from a Steam description (tags removed, entities decoded, whitespace collapsed)."""
    if not text:
        return ""
    if "<" in text or "&" in text:
        text = LexborHTMLParser(text).text(separator=" ")
    return " ".join(text.split())


def _parse_tag_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    try:
        values = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(values, list):
        return []
    return [tag for tag in (_to_int(v) for v in values) if tag is not None]


def _node_text(node: LexborNode | None) -> str:
    return " ".join(node.text().split()) if node is not None else ""


def _parse_row(node: LexborNode) -> SearchRow | None:
    attrs = node.attributes
    if "data-ds-packageid" in attrs or "data-ds-bundleid" in attrs:
        return None
    match = _LEADING_INT.match(attrs.get("data-ds-appid") or "")
    if match is None:
        return None
    name = _node_text(node.css_first("span.title"))
    if is_skipped_name(name):
        return None
    img = node.css_first("div.search_capsule img")
    return SearchRow(
        appid=int(match[1]),
        name=name,
        release_text=_node_text(node.css_first("div.search_released")) or None,
        tag_ids=_parse_tag_ids(attrs.get("data-ds-tagids")),
        capsule=(img.attributes.get("src") or None) if img is not None else None,
    )


def parse_search_html(html: str, total_count: int | None = None) -> SearchPage:
    rows: list[SearchRow] = []
    raw_rows = 0
    if html.strip():
        for node in LexborHTMLParser(html).css("a.search_result_row"):
            raw_rows += 1
            row = _parse_row(node)
            if row is not None:
                rows.append(row)
    return SearchPage(rows=rows, raw_rows=raw_rows, total_count=total_count)


def parse_search_response(body: str) -> SearchPage:
    """Parse a ``search/results/?infinite=1`` body: the JSON envelope or, failing that, bare HTML.

    A body that starts like JSON but does not parse is an error (``ValueError``), so a
    truncated response is never mistaken for an empty page.
    """
    text = body.strip()
    if not text.startswith("{"):
        return parse_search_html(text)
    data = json.loads(text)
    success = data.get("success", 1)
    if success not in (1, True, "1"):
        raise ValueError(f"search returned success={success!r}")
    html = data.get("results_html") or ""
    if not isinstance(html, str):
        raise ValueError("search results_html is not a string")
    return parse_search_html(html, _to_int(data.get("total_count")))


def parse_featured(data: Any) -> dict[str, list[SearchRow]]:
    """``api/featuredcategories`` -> {channel suffix: rows}, apps (type 0) only."""
    if not isinstance(data, dict):
        raise ValueError("featuredcategories: expected an object")
    lists: dict[str, list[SearchRow]] = {}
    for key, channel in FEATURED_LISTS:
        block = data.get(key)
        items = block.get("items") if isinstance(block, dict) else None
        rows: list[SearchRow] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or _to_int(item.get("type", 0)) != 0:
                continue
            appid = _to_int(item.get("id"))
            name = " ".join(str(item.get("name") or "").split())
            if not appid or is_skipped_name(name):
                continue
            capsule = item.get("header_image") or item.get("large_capsule_image") or None
            rows.append(SearchRow(appid=appid, name=name, capsule=capsule))
        lists[channel] = rows
    return lists


def _entries(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("appdetails: expected an object")
    return [entry for entry in payload.values() if isinstance(entry, dict)]


def _returned_appid(entry: dict[str, Any]) -> int | None:
    data = entry.get("data")
    if not entry.get("success") or not isinstance(data, dict):
        return None
    return _to_int(data.get("steam_appid"))  # seen as both int and str


def pick_appdetails_entry(payload: Any, appid: int) -> dict[str, Any] | None:
    """The successful entry whose ``data.steam_appid`` is ``appid``, whatever its outer key.

    The outer keys drift (Sept 2026: ``appids=730`` came back keyed by 2678630), so they are
    never trusted and there is no "lone entry" fallback.
    """
    for entry in _entries(payload):
        if _returned_appid(entry) == appid:
            return entry
    return None


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [" ".join(str(v).split()) for v in value if isinstance(v, str | int) and str(v).strip()]


def _id_descriptions(value: Any) -> tuple[list[int], list[str]]:
    ids: list[int] = []
    names: list[str] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        item_id = _to_int(item.get("id"))  # categories[].id is an int, genres[].id a string
        if item_id is not None:
            ids.append(item_id)
        description = " ".join(str(item.get("description") or "").split())
        if description:
            names.append(description)
    return ids, names


def parse_appdetails(
    payload: Any, appid: int, *, now: datetime, early_access_genre_id: int = 70
) -> SteamInfo | None:
    """Parse an appdetails response for ``appid``.

    Returns ``None`` when there are no usable details (``data: []``, empty payload) and
    raises :class:`AppUnavailable` for ``success: false`` or when only another app's data
    came back (``alias`` set), ``ValueError`` for a payload that is not an object.
    """
    entry = pick_appdetails_entry(payload, appid)
    if entry is None:
        entries = _entries(payload)
        returned = [other for other in map(_returned_appid, entries) if other is not None]
        if returned:
            raise AppUnavailable(f"appdetails alias {appid}->{returned[0]}", alias=returned[0])
        if entries and not any(entry.get("success") for entry in entries):
            raise AppUnavailable(f"appdetails {appid}: success=false (invalid, region-locked or adult-only)")
        return None
    data = entry["data"]
    category_ids, categories = _id_descriptions(data.get("categories"))
    genre_ids, genres = _id_descriptions(data.get("genres"))
    release = data.get("release_date") if isinstance(data.get("release_date"), dict) else {}
    release_text = " ".join(str(release.get("date") or "").split()) or None
    price = data.get("price_overview") if isinstance(data.get("price_overview"), dict) else {}
    labels = {name.lower() for name in categories + genres}
    return SteamInfo(
        appid=appid,
        name=" ".join(str(data.get("name") or "").split()),
        type=str(data["type"]) if data.get("type") else None,
        developers=_str_list(data.get("developers")),
        publishers=_str_list(data.get("publishers")),
        category_ids=category_ids,
        categories=categories,
        genre_ids=genre_ids,
        genres=genres,
        release_date_text=release_text,
        release_date=parse_release_date(release_text),
        coming_soon=bool(release.get("coming_soon")),
        early_access=early_access_genre_id in genre_ids or "early access" in labels,
        is_free=bool(data.get("is_free")),
        price=price.get("final_formatted") or None,
        header_image=data.get("header_image") or None,
        short_description=strip_html(data.get("short_description")),
        fetched_at=now,
    )


def _union(first: Iterable[Any], second: Iterable[Any]) -> list[Any]:
    return list(dict.fromkeys([*first, *second]))


def _reject_reason(info: SteamInfo) -> str | None:
    """Why an app with these details is not reported (non-game, playtest/demo/OST, MMO), or None."""
    if info.type and info.type != "game":
        return f"type:{info.type}"
    if is_skipped_name(info.name):
        return "name"
    if MMO_GENRE_ID in info.genre_ids or MMO_CATEGORY_ID in info.category_ids:
        return "mmo"
    return None


def _is_ranked(search: SteamSearch) -> bool:
    kind = str(search.params.get("filter", "")).lower()
    return kind.startswith("popular") or kind in ("topsellers", "globaltopsellers")


def _parse_at(value: Any) -> datetime | None:
    try:
        return ensure_utc(datetime.fromisoformat(str(value)))
    except ValueError:
        return None


# --------------------------------------------------------------------------------------
# Collector
# --------------------------------------------------------------------------------------


class SteamCollector(Collector):
    name = "steam"

    def __init__(
        self,
        ctx: CollectContext,
        budget: Budget | None = None,
        *,
        sleep: Callable[[float], None] | None = None,
    ):
        super().__init__(ctx, budget)
        self.settings = ctx.config.sources.steam
        # Same sleep as the HTTP layer by default (time.sleep live, a no-op in tests/replay).
        self.sleep = sleep or self.http.sleep
        self.throttled = False
        self._requests = 0
        self._by_appid: dict[int, Mention] = {}
        self._fallback_tag_names = {
            tag_id: name.replace("_", " ") for name, tag_id in self.settings.tags.items()
        }

    def enabled(self) -> tuple[bool, str | None]:
        if not self.settings.enabled:
            return False, "disabled in sources.yaml"
        return True, None

    # ---- entry points ------------------------------------------------
    def collect(self) -> list[Mention]:
        stop: BudgetExceeded | None = None
        try:
            self._discover()
        except BudgetExceeded as exc:
            stop = exc
        try:
            # Even without budget left, details already stored in state are still applied.
            self._enrich(fetch=stop is None)
        except BudgetExceeded as exc:
            stop = stop or exc
        if stop is not None:
            raise stop  # run() records it as a warning and keeps self.found
        return self.found

    def fetch_appdetails(self, appid: int) -> SteamInfo | None:
        """Details for one app, or ``None`` (unavailable, no data, HTTP or parse error, throttled).

        Only :class:`~gembot.http.BudgetExceeded` is raised.
        """
        if self.throttled:
            return None
        try:
            return self._appdetails(int(appid))
        except BudgetExceeded:
            raise
        except AppUnavailable as exc:
            if exc.alias is not None:
                self._log_alias(int(appid), exc.alias)
            return None
        except Exception as exc:  # HttpError, malformed payloads
            self.report.warnings.append(f"appdetails {appid}: {exc}")
            return None
        finally:
            self.report.requests = self.budget.used

    # ---- HTTP ---------------------------------------------------------
    def _get(self, url: str, params: dict[str, Any]) -> httpx.Response:
        if self.budget.exhausted:  # fail before sleeping
            raise BudgetExceeded(
                f"{self.budget.name}: request budget of {self.budget.limit} used up for this run"
            )
        if self._requests:
            self.sleep(self.settings.request_interval_s)
        self._requests += 1
        try:
            # One try per Steam request (retries=0): the first 429 must stop the stage, because
            # Steam escalates repeated 429s into 403 blocks.
            return self.http.get(url, budget=self.budget, params=params, headers=HEADERS, retries=0)
        except HttpError as exc:
            if isinstance(exc, RateLimited) or exc.status == 403:
                self._throttle(exc)
            raise

    def _log_alias(self, appid: int, returned: int) -> None:
        self.report.warnings.append(f"appdetails alias {appid}->{returned} (outer key drift); no details")
        self.log.info("appdetails alias %s->%s", appid, returned)

    def _throttle(self, exc: HttpError) -> None:
        # Every caller checks ``self.throttled`` before sending, so this runs once per run.
        self.throttled = True
        self.report.warnings.append(f"throttled by Steam ({exc}); stopping the Steam stage for this run")
        self.log.warning("throttled by Steam (%s); stopping for this run", exc)

    def _appdetails(self, appid: int) -> SteamInfo | None:
        params = {"appids": appid, "cc": self.settings.cc, "l": self.settings.lang}
        payload = self._get(APPDETAILS_URL, params).json()
        return parse_appdetails(
            payload, appid, now=self.now, early_access_genre_id=self.settings.early_access_genre_id
        )

    # ---- discovery ------------------------------------------------------
    def _discover(self) -> None:
        for search in self.settings.searches:
            if self.throttled:
                return
            with self.guard(f"search {search.name}"):
                self._run_search(search)
        if self.settings.featured_categories and not self.throttled:
            with self.guard("featuredcategories"):
                data = self._get(FEATURED_URL, {"cc": self.settings.cc, "l": self.settings.lang}).json()
                for channel, rows in parse_featured(data).items():
                    for row in rows:
                        self._add(row, f"steam:{channel}")

    def _search_page(self, search: SteamSearch, start: int) -> tuple[SearchPage, int]:
        params: dict[str, Any] = {
            "count": self.settings.page_size,
            "cc": self.settings.cc,
            "l": self.settings.lang,
            "category1": GAMES_CATEGORY,
            "ignore_preferences": 1,
            "ndl": 1,
        }
        if self.settings.exclude_tags:
            params["untags"] = ",".join(str(tag) for tag in self.settings.exclude_tags)
        params.update(search.params)
        params.update(infinite=1, start=start)
        page = parse_search_response(self._get(SEARCH_URL, params).text)
        return page, int(params["count"])

    def _run_search(self, search: SteamSearch) -> None:
        channel = f"steam:{search.name}"
        pages = max(search.max_pages, 1)
        if _is_ranked(search):
            rows: list[SearchRow] = []
            start = 0
            try:
                for _ in range(pages):
                    page, count = self._search_page(search, start)
                    rows.extend(page.rows)
                    if page.raw_rows < count:
                        break
                    start += count
            finally:  # keep earlier pages even if a later one fails
                for rank, row in enumerate(rows, start=1):
                    self._add(row, channel, rank=rank, list_size=len(rows))
            return
        # Rotating window over a long "coming soon" list.
        scratch = self.scratch()
        key = f"cursor:{search.name}"
        start = max(_to_int(scratch.get(key)) or 0, 0)
        fetched: set[int] = set()
        for _ in range(pages):
            if start in fetched:  # wrapped onto a page already read this run
                break
            fetched.add(start)
            page, _count = self._search_page(search, start)
            for row in page.rows:
                self._add(row, channel)
            start += page.raw_rows
            if page.raw_rows == 0 or (page.total_count is not None and start >= page.total_count):
                start = 0
            scratch[key] = start

    def _tag_names(self, tag_ids: Iterable[int]) -> list[str]:
        names = (TAG_NAMES.get(tag) or self._fallback_tag_names.get(tag) for tag in tag_ids)
        return list(dict.fromkeys(name for name in names if name))

    def _add(
        self, row: SearchRow, channel: str, *, rank: int | None = None, list_size: int | None = None
    ) -> None:
        tag_names = self._tag_names(row.tag_ids)
        mention = self._by_appid.get(row.appid)
        if mention is None:
            mention = Mention(
                source="steam",
                source_id=str(row.appid),
                url=store_url(row.appid),
                title=row.name,
                text=row.release_text or "",
                created_at=self.now,
                raw_tags=tag_names,
                media_thumb=row.capsule,
                channel=channel,
                rank=rank,
                list_size=list_size,
                extra={"release_text": row.release_text, "tag_ids": list(row.tag_ids), "channels": [channel]},
            )
            self._by_appid[row.appid] = mention
            self.found.append(mention)
            return
        # Same app from another list: one mention, the best rank wins, tags are merged.
        extra = mention.extra
        extra["tag_ids"] = _union(extra["tag_ids"], row.tag_ids)
        extra["channels"] = _union(extra["channels"], [channel])
        mention.raw_tags = _union(mention.raw_tags, tag_names)
        if rank is not None and (mention.rank is None or rank < mention.rank):
            mention.rank, mention.list_size, mention.channel = rank, list_size, channel
        if not extra["release_text"] and row.release_text:
            extra["release_text"] = row.release_text
            mention.text = mention.text or row.release_text
        mention.title = mention.title or row.name
        mention.media_thumb = mention.media_thumb or row.capsule

    # ---- enrichment -----------------------------------------------------
    def _enrich(self, *, fetch: bool) -> None:
        skip = self._skip_map()
        pending: list[tuple[tuple[bool, bool, int, int], int, SteamInfo | None]] = []
        for order, (appid, mention) in enumerate(list(self._by_appid.items())):
            skipped = skip.get(str(appid))
            aliased = skipped is not None and str(skipped.get("why", "")).startswith("alias:")
            if skipped is not None and not aliased:
                self._drop(appid)
                continue
            known = self._known_info(appid)
            if known is not None and (aliased or self._is_fresh(known)):
                self._apply(appid, known)
                continue
            if aliased:  # appdetails answers with another app; don't ask again this week
                continue
            # ranked apps first, then never-seen apps in discovery order, then stale ones
            priority = (mention.rank is None, known is not None, mention.rank or 0, order)
            pending.append((priority, appid, known))
        pending.sort(key=lambda item: item[0])

        stop: BudgetExceeded | None = None
        calls = 0
        for _, appid, known in pending:
            info: SteamInfo | None = None
            if fetch and stop is None and not self.throttled and calls < self.settings.max_new_apps_per_run:
                calls += 1
                try:
                    with self.guard(f"appdetails {appid}"):
                        try:
                            info = self._appdetails(appid)
                        except AppUnavailable as exc:
                            if exc.alias is None:
                                self._remember_skip(appid, "unavailable")
                                self._drop(appid)
                                continue
                            self._log_alias(appid, exc.alias)
                            self._remember_skip(appid, f"alias:{exc.alias}")
                            self._by_appid[appid].extra["appdetails_alias"] = exc.alias
                except BudgetExceeded as exc:
                    stop = exc
            info = info or known  # stale details beat none; the pipeline refreshes later
            if info is not None:
                self._apply(appid, info)
        if stop is not None:
            raise stop

    def _known_info(self, appid: int) -> SteamInfo | None:
        """The freshest stored details for ``appid`` (from its Steam mention or its game)."""
        state = self.ctx.state
        if state is None:
            return None
        infos: list[SteamInfo] = []
        stored = state.mentions.get(f"steam:{appid}")
        if stored is not None and isinstance(stored.extra.get("steam"), dict):
            with contextlib.suppress(ValueError):  # pydantic.ValidationError: ignore a corrupt record
                infos.append(SteamInfo.model_validate(stored.extra["steam"]))
        game = state.games.get(f"steam:{appid}")
        if game is not None and game.steam is not None:
            infos.append(game.steam)
        if not infos:
            return None
        return max(infos, key=lambda info: info.fetched_at or datetime.min.replace(tzinfo=UTC))

    def _is_fresh(self, info: SteamInfo) -> bool:
        if info.fetched_at is None:
            return False
        return self.now - info.fetched_at < timedelta(hours=self.settings.appdetails_refresh_hours)

    def _apply(self, appid: int, info: SteamInfo) -> None:
        reason = _reject_reason(info)
        if reason is not None:
            self._remember_skip(appid, reason)
            self._drop(appid)
            return
        mention = self._by_appid[appid]
        if not info.tags:
            info = info.model_copy(update={"tags": self._tag_names(mention.extra["tag_ids"])})
        mention.title = info.name or mention.title
        mention.text = info.short_description or mention.text or info.release_date_text or ""
        mention.author = info.developers[0] if info.developers else mention.author
        mention.raw_tags = _union([*info.categories, *info.genres, *info.tags], mention.raw_tags)
        mention.media_thumb = info.header_image or mention.media_thumb
        mention.extra["release_text"] = mention.extra["release_text"] or info.release_date_text
        mention.extra["steam"] = info.model_dump(mode="json")

    def _drop(self, appid: int) -> None:
        mention = self._by_appid.pop(appid)
        self.found[:] = [m for m in self.found if m is not mention]

    # ---- apps not worth asking about again (scratch, pruned after 7 days) ----
    def _skip_map(self) -> dict[str, dict[str, str]]:
        scratch = self.scratch()
        raw = scratch.get("skip")
        cutoff = self.now - UNAVAILABLE_RETRY
        fresh: dict[str, dict[str, str]] = {}
        for key, entry in (raw if isinstance(raw, dict) else {}).items():
            at = _parse_at(entry.get("at")) if isinstance(entry, dict) else None
            if at is not None and at > cutoff:
                fresh[key] = entry
        scratch["skip"] = fresh
        return fresh

    def _remember_skip(self, appid: int, why: str) -> None:
        skip = self.scratch().setdefault("skip", {})
        skip[str(appid)] = {"at": self.now.isoformat(), "why": why}
        if len(skip) > MAX_SKIP_ENTRIES:
            newest = sorted(skip.items(), key=lambda item: item[1]["at"], reverse=True)[:MAX_SKIP_ENTRIES]
            skip.clear()
            skip.update(newest)
