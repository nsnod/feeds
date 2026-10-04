"""Game resolution: link canonicalisation, title extraction and clustering mentions into games.

Pipeline position: collectors -> **Resolver** -> prefilter -> enrichment -> scoring.

How a mention finds its game (BUILD_SPEC section 3):

1. **Hard ids.** Steam app URLs (every variant: store, community, agecheck, ``s.team``,
   ``steam://``, redirect wrappers) become ``steam:<appid>``; itch.io game pages become
   ``itch:<dev>/<slug>``. Shortened links are followed by :class:`LinkExpander` (budgeted,
   cached, never raises). Mentions that share a hard id share a game.
2. **Fuzzy titles.** Candidate titles are pulled out of the post (quoted names, "my game X",
   "X is a co-op game", "X - Official Trailer", "wishlist X on Steam", Title Case runs near
   game words, ...) and compared with ``rapidfuzz.fuzz.token_set_ratio`` against the
   titles/aliases of games seen in the last ``lookback_days``. ``token_set_ratio`` scores a
   subset as 100 ("Pizza Panic" vs "Gorilla Pizza Panic"), so a match also needs the same
   developer or - when a developer is unknown - a high plain ``fuzz.ratio`` too. When both
   developers are known and differ, only an *exact* normalised title of 2+ words merges
   (a Reddit username rarely equals the Steam studio name).
3. **LLM titles** (optional) are tried before the heuristic candidates.
4. A mention with a plausible title but no match starts a title game ``t:<slug>-<hash>``.
5. Everything else is dropped and never becomes a game.

Game ids are stable forever. A title game that later gains a hard id keeps its id and
records the hard id in ``Game.hard_ids``; two games that turn out to share a hard id are
merged (the newer one is absorbed by the older one and reported in ``ResolveResult.merged``).
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from rapidfuzz import fuzz, process

from gembot.config import ResolverSource
from gembot.http import Budget, BudgetExceeded, HttpClient, HttpError
from gembot.models import Game, Mention, SteamInfo

log = logging.getLogger(__name__)

STRICT_TITLE_RATIO = 90  # plain fuzz.ratio needed when a developer is unknown
DEV_MATCH_RATIO = 90  # fuzz.ratio between compacted developer names
MAX_ALIASES = 12
MAX_CANDIDATES = 8
MAX_FUZZY_CANDIDATES = 3  # only the best few candidates are used to *join* an existing game
MAX_TEXT_CHARS = 1500  # candidate extraction only looks at the start of long posts
MAX_TITLE_CHARS = 300  # ... and of absurdly long titles (the Title Case regexes are quadratic in run length)
_TEXT_OFFSET = 100_000  # positions in the body sort after positions in the title

# --------------------------------------------------------------------------------------
# Hard ids
# --------------------------------------------------------------------------------------

_STEAM_STORE_HOSTS = frozenset({"store.steampowered.com", "steampowered.com"})
_STEAM_STORE_PATH = re.compile(r"^/(?:(?:agecheck|ageverify|news)/)?app/(\d+)(?:/|$)", re.I)
_STEAM_WIDGET_PATH = re.compile(r"^/widget/(\d+)(?:/|$)", re.I)
_STEAM_COMMUNITY_PATH = re.compile(r"^/(?:app|games|ogg)/(\d+)(?:/|$)", re.I)
_STEAM_SHORT_PATH = re.compile(r"^/a/(\d+)(?:/|$)", re.I)
_STEAM_PROTOCOL = re.compile(r"^steam://(?:store|advertise|install|run|nav/games/details)/(\d+)", re.I)
_ITCH_DEV = re.compile(r"^[a-z0-9][a-z0-9\-]*$")
_ITCH_SLUG = re.compile(r"^[a-z0-9][a-z0-9_\-]*$")
_ITCH_SOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9\-]*/[a-z0-9][a-z0-9_\-]*$")
_ITCH_RESERVED_SUBDOMAINS = frozenset(
    {"www", "api", "static", "img", "itch", "assets", "html", "blog", "status"}
)
# host -> (path prefix, query parameters that carry the real URL)
_REDIRECT_WRAPPERS: dict[str, tuple[str, tuple[str, ...]]] = {
    "steamcommunity.com": ("/linkfilter", ("url", "u")),
    "google.com": ("/url", ("q", "url")),
    "l.facebook.com": ("/l.php", ("u",)),
    "lm.facebook.com": ("/l.php", ("u",)),
    "l.instagram.com": ("/", ("u",)),
    "youtube.com": ("/redirect", ("q",)),
    "m.youtube.com": ("/redirect", ("q",)),
    "out.reddit.com": ("/", ("url",)),
}


def _split_url(url: str) -> tuple[str, str, str, str] | None:
    url = url.strip().strip("<>")
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    elif "://" not in url:
        url = "https://" + url
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    return parts.scheme.lower(), host.removeprefix("www."), parts.path or "/", parts.query


def _host_of(url: str) -> str:
    split = _split_url(url)
    return split[1] if split else ""


def canonical_hard_id(url: str, _depth: int = 0) -> str | None:
    """``steam:<appid>`` / ``itch:<dev>/<slug>`` for a store page URL, else ``None``."""
    if not url or not isinstance(url, str) or _depth > 3:
        return None
    raw = url.strip()
    match = _STEAM_PROTOCOL.match(raw)
    if match:
        return _steam_id(match.group(1))
    if raw.lower().startswith("steam://openurl/"):
        return canonical_hard_id(raw[len("steam://openurl/") :], _depth + 1)
    split = _split_url(raw)
    if split is None:
        return None
    scheme, host, path, query = split
    if scheme not in ("http", "https"):
        return None
    path = unquote(path)
    if host in _STEAM_STORE_HOSTS:
        match = _STEAM_STORE_PATH.match(path) or _STEAM_WIDGET_PATH.match(path)
    elif host == "steamcommunity.com":
        match = _STEAM_COMMUNITY_PATH.match(path)
    elif host == "s.team":
        match = _STEAM_SHORT_PATH.match(path)
    else:
        match = None
    if match:
        return _steam_id(match.group(1))
    wrapper = _REDIRECT_WRAPPERS.get(host)
    if wrapper and path.lower().startswith(wrapper[0]):
        params = parse_qs(query)
        for name in wrapper[1]:
            for target in params.get(name, []):
                found = canonical_hard_id(target, _depth + 1)
                if found:
                    return found
        return None
    if host.endswith(".itch.io"):
        dev = host[: -len(".itch.io")]
        if not _ITCH_DEV.match(dev) or dev in _ITCH_RESERVED_SUBDOMAINS:
            return None
        segments = [seg for seg in path.lower().split("/") if seg]
        if not segments or not _ITCH_SLUG.match(segments[0]):
            return None  # a developer profile, not a game page
        return f"itch:{dev}/{segments[0]}"
    return None


def _steam_id(digits: str) -> str | None:
    appid = int(digits)
    return f"steam:{appid}" if appid > 0 else None


_URL_CHARS = r"[^\s<>\"'`\[\]{}|\\^“”‘’«»「」【】（）]"
_SCHEME_URL = re.compile(rf"(?i)\b(?:https?://|steam://){_URL_CHARS}+")
_BARE_URL = re.compile(
    r"(?i)(?<![\w.@/:\-])(?:www\.)?"
    r"(?:store\.steampowered\.com|steampowered\.com|steamcommunity\.com|s\.team|"
    r"[a-z0-9][a-z0-9\-]*\.itch\.io|bit\.ly|tinyurl\.com|buff\.ly|ow\.ly)"
    rf"(?=[/\s.,!?;:)\]]|$)(?:/{_URL_CHARS}*)?"
)
_TRAILING = ".,!?;:'\"*~`…»”’>"
# Display-truncated links ("gorilladev.itch.io/gorilla-pizz...", "store.steampowered.com/app/35272…")
# point at a different page than the real one: ignore them.
_TRUNCATED = re.compile(r"(?:\.\.\.|…)[)\]\"'”’*>,;:!?]*$")


def _trim_url(url: str) -> str:
    while url:
        last = url[-1]
        if last in _TRAILING or (last == ")" and url.count(")") > url.count("(")):
            url = url[:-1]
            continue
        break
    return url


def _url_text(text: str) -> str:
    return (text or "").replace("\\_", "_").replace("&amp;", "&")


def _url_spans(text: str) -> list[tuple[int, str]]:
    """(start, url) for every URL in already-:func:`_url_text`-normalised text, in order."""
    found: list[tuple[int, str]] = []
    spans: list[tuple[int, int]] = []
    for match in _SCHEME_URL.finditer(text):
        spans.append(match.span())
        if _TRUNCATED.search(match.group(0)):
            continue
        url = _trim_url(match.group(0))
        if "://" in url and len(url.split("://", 1)[1]) > 0:
            found.append((match.start(), url))
    for match in _BARE_URL.finditer(text):
        if any(start <= match.start() < end for start, end in spans) or _TRUNCATED.search(match.group(0)):
            continue
        url = _trim_url(match.group(0))
        if url:
            found.append((match.start(), "https://" + url))
    found.sort(key=lambda item: item[0])
    return found


def extract_urls(text: str) -> list[str]:
    """Every URL in ``text`` (markdown links, trailing punctuation and bare store links handled).

    Links truncated for display ("...", "…") are skipped: they would name a different page.
    """
    if not text:
        return []
    return _unique(url for _, url in _url_spans(_url_text(text)))


def _unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


_REDIRECT_EXPECT = (200, 201, 202, 203, 204, 206, 301, 302, 303, 307, 308)
_META_REFRESH = re.compile(
    r"""(?is)<meta[^>]+http-equiv\s*=\s*["']?refresh["']?[^>]*content\s*=\s*["']?[^"'>]*?url\s*=\s*['"]?([^"'>\s]+)"""
)


class LinkExpander:
    """Follows shortened links (bit.ly, t.co, ...) to their destination, budgeted and cached.

    Only URLs whose host is a known shortener are touched. Each hop is one ``HEAD`` request
    without automatic redirects (``GET`` when the server answers 405), at most ``max_hops``
    hops, charged to ``budget``. :meth:`expand` never raises: on an error it returns the
    furthest URL it reached (usually the original), and when the budget runs out mid-chain
    it returns the partial result without caching it.
    """

    def __init__(self, http: HttpClient, budget: Budget, shortener_hosts: list[str], max_hops: int = 3):
        self.http = http
        self.budget = budget
        self.hosts = frozenset(h.strip().lower().removeprefix("www.") for h in shortener_hosts if h.strip())
        self.max_hops = max_hops
        self.cache: dict[str, str] = {}
        self.errors: list[str] = []

    def is_shortened(self, url: str) -> bool:
        return bool(url) and _host_of(url) in self.hosts

    def expand(self, url: str) -> str:
        try:
            if not self.is_shortened(url):
                return url
            if url in self.cache:
                return self.cache[url]
        except Exception:  # pragma: no cover - defensive: odd input types
            return url
        current = url
        cacheable = True
        try:
            for _ in range(self.max_hops):
                if not self.is_shortened(current):
                    break
                if self.budget.exhausted:
                    cacheable = False
                    break
                target = self._hop(current)
                if not target or target == current:
                    break
                current = target
        except BudgetExceeded:
            cacheable = False
        except Exception as exc:  # HttpError, RateLimited, malformed Location, ...
            self.errors.append(f"{url}: {type(exc).__name__}: {exc}")
            log.info("link expansion failed for %s: %s", url, exc)
        if cacheable:
            self.cache[url] = current
        return current

    def _hop(self, url: str) -> str | None:
        try:
            response = self.http.request(
                "HEAD", url, budget=self.budget, follow_redirects=False, expect=_REDIRECT_EXPECT
            )
        except HttpError as exc:
            if exc.status != 405:
                raise
            response = self.http.request(
                "GET", url, budget=self.budget, follow_redirects=False, expect=_REDIRECT_EXPECT
            )
        if 300 <= response.status_code < 400:
            location = response.headers.get("location")
            return urljoin(url, location.strip()) if location else None
        if response.request.method == "GET":
            match = _META_REFRESH.search(response.text[:20_000])
            if match:
                return urljoin(url, html.unescape(match.group(1)))
        return None


def _own_hard_id(mention: Mention) -> str | None:
    if mention.source == "steam":
        info = mention.extra.get("steam")
        appid = info.get("appid") if isinstance(info, dict) else getattr(info, "appid", None)
        if appid:
            return f"steam:{int(appid)}"
        source_id = mention.source_id.strip()
        if source_id.isdigit():
            return _steam_id(source_id)
        found = canonical_hard_id(mention.url)
        return found if found and found.startswith("steam:") else None
    if mention.source == "itch":
        source_id = mention.source_id.strip().lower()
        if _ITCH_SOURCE_ID.match(source_id):
            return f"itch:{source_id}"
        found = canonical_hard_id(mention.url)
        return found if found and found.startswith("itch:") else None
    return None


def _mention_urls(mention: Mention) -> list[str]:
    return _unique([mention.url, *mention.links, *extract_urls(mention.title), *extract_urls(mention.text)])


# "If you liked <link>", "heavily inspired by <link>", "fans of <link>": a reference, not this game.
_REFERENCE_PHRASE = re.compile(
    r"(?i)\b(?:if\s+you\s+(?:liked?|loved?|enjoy(?:ed)?|played|dig)|fans?\s+of|inspired\s+by|similar\s+to|"
    r"reminds?\s+(?:me\s+|you\s+|us\s+)?of|in\s+the\s+(?:style|vein|spirit)\s+of|successor\s+to|"
    r"(?:clone|rip-?off|knock-?off)\s+of|versus|vs\.?|than|a\s+la|à\s+la)(?!\w)"
)
_LIKE_WORD = re.compile(r"(?i)\blike\b")
_SENTENCE_BREAK = re.compile(r"(?<=[a-z0-9)])[.!?](?=\s)|[!?;\n]|(?:https?://|steam://)\S+")
_WRAPPING = "[](){}<>\"'“”‘’*"


def _is_reference_link(text: str, start: int) -> bool:
    """True when the URL at ``start`` follows a comparison ("If you liked X (<url>)", "inspired by <url>")."""
    clause = _SENTENCE_BREAK.split(text[max(0, start - 120) : start])[-1]
    for pattern, names_only in ((_REFERENCE_PHRASE, False), (_LIKE_WORD, True)):
        matches = list(pattern.finditer(clause))
        if not matches:
            continue
        words = [w.strip(_WRAPPING) for w in clause[matches[-1].end() :].split()]
        words = [w for w in words if w]
        if len(words) > 5:
            continue
        if names_only and not all(w[0].isupper() or w[0].isdigit() for w in words):
            continue  # "I'd like you to try <url>" is not a comparison; "like Lethal Company (<url>)" is
        return True
    return False


def _names_agree(a: str | None, b: str | None) -> bool:
    na, nb = normalize_title(a or ""), normalize_title(b or "")
    if not na or not nb:
        return False
    if fuzz.ratio(na, nb) >= 85:
        return True
    return fuzz.token_set_ratio(na, nb) == 100 and min(len(na.split()), len(nb.split())) >= 2


def hard_ids_for(mention: Mention, expander: LinkExpander | None = None) -> list[str]:
    """Hard ids of the game(s) a mention points at, best first.

    The mention's own Steam/itch id leads. Links introduced as a comparison ("If you liked
    <link>", "inspired by <link>") are ignored. Among the rest, links whose URL slug names one
    of the post's title candidates come first and links whose slug names something else last;
    ties keep Steam before itch.
    """
    expanded_ids: dict[str, str | None] = {}

    def hard_of(url: str) -> str | None:
        if url not in expanded_ids:
            hard = canonical_hard_id(url)
            if hard is None and expander is not None:
                expanded = expander.expand(url)
                if expanded != url:
                    hard = canonical_hard_id(expanded)
            expanded_ids[url] = hard
        return expanded_ids[url]

    text_urls: list[str] = []
    reference_urls: list[str] = []
    for raw in (mention.title, mention.text):
        normalised = _url_text(raw)
        for start, url in _url_spans(normalised):
            (reference_urls if _is_reference_link(normalised, start) else text_urls).append(url)
    references = {h for h in map(hard_of, reference_urls) if h}
    explicit = {h for h in map(hard_of, [mention.url, *text_urls]) if h}
    own = _own_hard_id(mention)
    entries: list[tuple[str, str]] = []  # (hard id, url) in order of appearance
    for url in _unique([mention.url, *mention.links, *text_urls]):
        hard = hard_of(url)
        if hard and hard != own and (hard not in references or hard in explicit):
            entries.append((hard, url))
    ordered = _unique(h for h, _ in entries)
    if len(ordered) >= 2:
        candidates = title_candidates(mention)

        def slug_fit(hard_id: str) -> int:
            """0: the URL slug names one of the post's titles, 1: unknown, 2: it names something else."""
            slugs = [t for h, url in entries if h == hard_id and (t := _title_from_url(url))]
            if not candidates or not slugs:
                return 1
            return 0 if any(_names_agree(s, c) for s in slugs for c in candidates) else 2

        rank = {h: (slug_fit(h), 0 if h.startswith("steam:") else 1, i) for i, h in enumerate(ordered)}
        ordered.sort(key=rank.__getitem__)
    else:
        ordered.sort(key=lambda h: 0 if h.startswith("steam:") else 1)
    return ([own] if own else []) + ordered


# --------------------------------------------------------------------------------------
# Titles
# --------------------------------------------------------------------------------------


def _wordset(text: str) -> frozenset[str]:
    return frozenset(text.split())


_GENERIC_WORDS = _wordset(
    """
    game games gaming gamer gamers my our your their his her the a an new first last next indie dev devs
    developer developers gamedev indiedev indiegame indiegames indiegaming solo solodev solodevelopment studio
    co op coop online local multiplayer singleplayer demo demos trailer trailers teaser steam page wishlist
    wishlists wishlisted wishlisting playtest playtesting playtesters playtester official announce announcement
    announced reveal revealed gameplay early access devlog devlogs update updates progress horror party physics
    free out now available today tonight week weekend weekly monday tuesday wednesday thursday friday saturday
    sunday january february march april may june july august september october november december screenshot
    screenshots screenshotsaturday unity unity3d godot unreal engine ue4 ue5 gamemaker blender made with in on
    for of and or to is it its i we you this that these those check please feedback help question questions
    release released date launch launching launched coming soon pc console consoles xbox playstation ps4 ps5
    switch nintendo mac linux windows steamdeck deck beta alpha prototype version friends friend boys chat voice
    proximity play playing played team project projects reddit bluesky twitter youtube tiktok instagram discord
    kickstarter fest nextfest edition hi hello hey thanks thank what how why players player vr ai fps rpg mmo
    pvp pve npc dlc ost wip til psa ama imo lol omg sim simulator video clip art music sound level levels map
    maps character characters boss enemy enemies weapon weapons lobby menu ui shader shaders lighting
    animation animations model models asset assets vs day days year years month months hour hours update
    finally just still also really very so but not some any all every more most best good great cool fun
    funny chaotic cozy short shorts live stream streamer streamers twitch vod episode part
    destroymygame playmygame coopgaming unrealengine pov
    upcoming hidden gem gems top postmortem post mortem result results learned lessons lesson stats numbers
    sales revenue data tips advice guide tutorial breakdown analysis review reviews recap highlights festival
    expo convention booth pax gdc gamescom summer west east hiring hire job jobs shoutout shout huge cross
    posting crossposting crosspost come say ragdoll ragdolls opinion opinions inspired clone clones
    """
)
_GENERIC_PHRASES = frozenset(
    {
        "my game",
        "our game",
        "the game",
        "this game",
        "indie game",
        "screenshot saturday",
        "steam next fest",
        "next fest",
        "game dev",
        "unreal engine",
        "game maker",
        "early access",
        "co op",
        "hidden gem",
        "summer game fest",
        "game fest",
        "post mortem",
        "early access launch",
    }
)
_REFERENCE_GAMES = frozenset(
    """lethal company|content warning|r e p o|repo|peak|phasmophobia|among us|fall guys|minecraft|roblox|
    fortnite|valheim|deep rock galactic|overcooked|overcooked 2|human fall flat|gang beasts|it takes two|
    split fiction|lockdown protocol|buckshot roulette|schedule i|mage arena|chained together|only up|
    supermarket simulator|golf with your friends|pummel party|escape the backrooms|dead by daylight|
    left 4 dead|left 4 dead 2|gtfo|sea of thieves|a way out|stardew valley|terraria|helldivers 2|palworld|
    garrys mod|gmod|people playground|totally accurate battle simulator|lethal league|risk of rain 2|
    the forest|sons of the forest|rust|dayz|gorilla tag|vrchat|rec room|webfishing|bread and fred|
    pico park|devour|the outlast trials|we were here|liars bar|schedule 1|schedule one|supermarket together|
    peak game|lethal company 2|bodycam|r e p o""".replace("\n", "").split("|")
)
_REFERENCE_GAMES = frozenset(name.strip() for name in _REFERENCE_GAMES if name.strip())
_REFERENCE_COMPACT = frozenset(name.replace(" ", "") for name in _REFERENCE_GAMES)
# Words that make a Title Case run a sentence or a listicle rather than a name.
_VERB_WORDS = _wordset(
    "added adds made makes got gets reached reaches built builds created creates turned turns became becomes "
    "tried tries explains explained learned learnt sold sells earned earns spent spends finished finishes "
    "started starts quit switched switches rewrote rewrites fixed fixes implemented implements redesigned "
    "changed changes improved improves completed shipped ships published ported remade revamped updated "
    "reviewed ranks ranked rates rated finds takes took gives gave goes went comes came does did needs wants "
    "shows showed hired hits"
)
_LISTICLE_NEXT = _wordset(
    "best top upcoming hidden new indie games things tips ways reasons most free co-op coop horror "
    "multiplayer great underrated cozy must"
)
_GERUND_NOUNS = _wordset(
    "viking vikings spring springs string strings morning evening ceiling wedding pudding something nothing "
    "everything anything thing things king kings sterling darling duckling dumpling dumplings sibling siblings "
    "ring rings wing wings swing sling sting bling lightning feeling ending endings beginning herring blessing "
    "offering earring"
)
_FUNCTION_WORDS = _wordset("a an the to in into for of with my our your on at from about")
# what a devlog adds ("Adding Proximity Chat"), as opposed to genre nouns ("Cooking Simulator")
_FEATURE_WORDS = _wordset(
    "multiplayer coop co op online proximity chat voice ragdoll ragdolls physics lighting shader shaders "
    "animation animations ai npc npcs enemy enemies ui menu lobby netcode networking steam demo trailer devlog "
    "update game games wishlist wishlists level levels map maps boss weapon weapons inventory crafting"
)
_COMPARISON_LEAD = _wordset("like unlike liked loved enjoyed")
_PRONOUN_START = _wordset(
    "i i'm im i've ive i'd we we're we've you you're it it's its this that he she they what how why when "
    "where who which does do did is are can could should would will any anyone anybody help looking need "
    "thoughts thanks"
)
# words that strip from the start of an extracted Title Case run
_LEAD = _wordset(
    "so and but or hey hi hello meet introducing announcing presenting check out this that today finally "
    "just our my here here's heres its it's we i i've we've i'm we're after before play playing wishlist "
    "trailer demo new official steam free behold welcome to for in on at of with a an is are was "
    "now ok okay yes no well guys everyone everybody update devlog teaser game games"
)
# words that end an extracted run (never part of a title when they follow it)
_CUT = _wordset(
    "is are was were has have had will can just now got gets comes coming launches launching launched "
    "releases releasing released out demo trailer teaser playtest wishlist available official announce "
    "announcement announced gameplay steam update devlog early free my our your its it's this that i we "
    "you they made making built co-op coop multiplayer game games with for by from about where when which "
    "who after finally but meets like clone style inspired esque vibes ripoff killer"
)
_TRAIL = _wordset(
    "is on and the a an of to for in at with or by from demo trailer official steam game games devlog devlogs "
    "teaser playtest gameplay update announcement reveal"
)

_APOSTROPHES = re.compile(r"['’ʼ`´]")
_NON_WORD = re.compile(r"[^\w\s]|_")
_BRACKETS = re.compile(r"[\[\(\{【][^\]\)\}】]*[\]\)\}】]")
_PIPE_SPLIT = re.compile(r"\s*[|｜]\s*")
_DASH_SPLIT = re.compile(r"\s+[-~]\s+|\s*[–—]\s*|\s*:\s+|\s+//\s+")
_SUFFIX_NUMBERS = re.compile(
    r"(?:\s+(?:devlog|dev log|week|day|update|part|episode|ep|v)\s*\d+(?:\s+\d+)*)+$"
)
_PREFIX_NUMBERS = re.compile(r"^(?:devlog|dev log|week|day|update)\s*\d*\s+")
_NOISE_SUFFIXES: tuple[tuple[str, ...], ...] = tuple(
    sorted(
        (
            tuple(phrase.split())
            for phrase in (
                "official trailer",
                "official announce trailer",
                "official announcement trailer",
                "official gameplay trailer",
                "official teaser",
                "announce trailer",
                "announcement trailer",
                "reveal trailer",
                "launch trailer",
                "release trailer",
                "release date trailer",
                "gameplay trailer",
                "teaser trailer",
                "cinematic trailer",
                "early access trailer",
                "demo trailer",
                "trailer",
                "teaser",
                "official",
                "free demo",
                "demo",
                "playtest",
                "open playtest",
                "early access",
                "on steam",
                "now on steam",
                "steam",
                "steam page",
                "out now",
                "is out",
                "now available",
                "coming soon",
                "devlog",
                "dev log",
                "gameplay",
                "wishlist now",
                "wishlist",
                "announcement",
                "reveal",
                "pre alpha",
                "alpha",
                "beta",
                "next fest",
                "steam next fest",
            )
        ),
        key=len,
        reverse=True,
    )
)
_NOISE_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("announcing",),
    ("introducing",),
    ("presenting",),
    ("devlog",),
    ("official", "trailer"),
    ("new", "trailer"),
)


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _plain_tokens(text: str) -> list[str]:
    text = _APOSTROPHES.sub("", _fold(text).lower().replace("&", " and "))
    return _NON_WORD.sub(" ", text).split()


def _is_noise_segment(segment: str) -> bool:
    """True when a title segment carries no name ("Official Trailer", "Steam Next Fest Demo")."""
    tokens = _strip_noise_tokens(_plain_tokens(segment))
    return all(t in _GENERIC_WORDS or t.isdigit() for t in tokens)


def _is_tagline(segment: str) -> bool:
    tokens = _plain_tokens(segment)
    return bool(tokens) and (
        tokens[0] in ("a", "an") or any(t in ("game", "games", "coop", "multiplayer") for t in tokens)
    )


def _strip_noise_tokens(tokens: list[str]) -> list[str]:
    tokens = list(tokens)
    changed = True
    while changed and tokens:
        changed = False
        for phrase in _NOISE_SUFFIXES:
            if len(tokens) > len(phrase) and tuple(tokens[-len(phrase) :]) == phrase:
                del tokens[-len(phrase) :]
                changed = True
                break
        for phrase in _NOISE_PREFIXES:
            if len(tokens) > len(phrase) and tuple(tokens[: len(phrase)]) == phrase:
                del tokens[: len(phrase)]
                changed = True
                break
    return tokens


def _main_segment(title: str) -> str:
    """Drop "| ..." tails and noise / tagline segments after dashes and colons."""
    pipe_parts = [p for p in _PIPE_SPLIT.split(title) if p.strip()]
    if not pipe_parts:
        return title
    title = next((p for p in pipe_parts if not _is_noise_segment(p)), pipe_parts[0])
    parts = [p for p in _DASH_SPLIT.split(title) if p.strip()]
    if len(parts) <= 1:
        return title
    named = [p for p in parts if not _is_noise_segment(p)]
    if not named:
        return parts[0]
    keep = [named[0]] + [p for p in named[1:] if not _is_tagline(p)]
    return " ".join(keep)


def normalize_title(title: str) -> str:
    """Comparable form of a title: lowercase, no accents/emoji/punctuation/"(demo)"/"- trailer"."""
    if not title:
        return ""
    text = _fold(str(title)).replace("\\_", "_")
    text = _main_segment(text)
    without_brackets = _BRACKETS.sub(" ", text)
    if _plain_tokens(without_brackets):
        text = without_brackets
    tokens = _plain_tokens(text)
    if not tokens:
        return ""
    joined = _PREFIX_NUMBERS.sub("", _SUFFIX_NUMBERS.sub("", " ".join(tokens)))
    stripped = _strip_noise_tokens(joined.split())
    if not stripped or all(t in _GENERIC_WORDS or t.isdigit() for t in stripped):
        return " ".join(tokens)  # nothing but noise words ("Official Trailer"): keep them all
    return " ".join(stripped)


_CAP = r"[A-Z0-9][\w'’\-]*(?:\.[A-Za-z0-9]+)*"
_CONNECT = r"(?:of|the|and|a|an|in|to|on|at|vs\.?|&|n['’]|or|de|la|le|el|da|du|von|van|der)"
_RUN = rf"(?<![\w'’.\-]){_CAP}(?:[ \t]+(?:{_CONNECT}[ \t]+){{0,2}}{_CAP})*"
# Inside the anchored patterns a run is capped at 10 words: titles are <= 6 words anyway, and an
# unbounded run makes "<run> is a ... game" quadratic on long Title Case text (backtracking per start).
_T = rf"(?P<t>(?<![\w'’.\-]){_CAP}(?:[ \t]+(?:{_CONNECT}[ \t]+){{0,2}}{_CAP}){{0,9}})"
_OPEN_Q = r"[\"“'‘]?"
_GAME_NOUN = (
    r"(?:game|experience|sim(?:ulator)?|roguelike|roguelite|shooter|platformer|adventure|puzzler|brawler|"
    r"racer|metroidvania|project)s?"
)
_RUN_RX = re.compile(_RUN)
# (priority, pattern, kind): lower priority = stronger evidence; kind "run" gets trimmed.
_PATTERNS: tuple[tuple[int, re.Pattern[str], str], ...] = (
    (
        1,
        re.compile(
            r"(?i:\b(?:my|our)\s+(?!(?:fav(?:ou?rite)?|all[\s-]time|current)\b)(?:[\w\-]+\s+){0,3}?"
            r"(?:game|project)\b\s*[,:\-–—]?\s*(?:(?:is\s+)?(?:called|named|titled)\s+)?)" + _OPEN_Q + _T
        ),
        "run",
    ),
    (2, re.compile(r"(?i:\b(?:called|named|titled)\s+)" + _OPEN_Q + _T), "run"),
    (2, re.compile(r"[\"“](?P<t>[^\"“”\n]{2,60})[\"”]"), "quoted"),
    (2, re.compile(r"(?:^|(?<=[\s(\[]))['‘](?P<t>[A-Z0-9][^'‘’\n]{1,58})['’](?=[\s.,!?:;)\]]|$)"), "quoted"),
    (
        2,
        re.compile(
            _T + r"(?i:\s+(?:is|['’]s)\s+(?:a|an|my|our|the)\s+(?:[\w\-/&+]+\s+){0,6}?" + _GAME_NOUN + r"\b)"
        ),
        "run",
    ),
    (
        2,
        re.compile(_T + r"(?i:\s*,\s+(?:a|an|my|our|the)\s+(?:[\w\-/&+]+\s+){0,6}?" + _GAME_NOUN + r"\b)"),
        "run",
    ),
    (2, re.compile(r"(?i:\bwish[\s-]?list(?:ing)?\s+)" + _OPEN_Q + _T), "run"),
    (
        3,
        re.compile(
            _T + r"(?i:(?:\s+(?:finally|now|just|officially))?\s+(?:(?:is|has)\s+)?"
            r"(?:(?:free|playable|steam|public|open|closed|first)\s+)?"
            r"(?:demo|playtest|trailer|teaser|beta|early\s+access|out\s+now|now\s+(?:available|live|out)|"
            r"coming\s+(?:to|on)\s+steam|launch(?:es|ed|ing)?|releas(?:es|ed|ing)|devlog|dev\s+log|"
            r"(?:got|gets|has)\s+a\s+steam\s+page|on\s+steam|steam\s+page|is\s+(?:out|live|coming))\b)"
        ),
        "run",
    ),
    (
        3,
        re.compile(
            r"(?i:\b(?:introducing|announcing|presenting|meet|check\s+out|say\s+hello\s+to|welcome\s+to|"
            r"play|try)\s+)" + _OPEN_Q + _T
        ),
        "run",
    ),
    (
        3,
        re.compile(
            r"(?i:\b(?:trailer|teaser|demo|playtest|(?:steam|store)\s+page|gameplay|footage|devlog|update|"
            r"key\s*art|capsule(?:\s+art)?|soundtrack|ost|logo|screenshots?|booth|stand|panel|talk|"
            r"post-?mortem)\s+(?:for|of|from)\s+(?:(?:my|our)\s+game\s+)?)" + _OPEN_Q + _T
        ),
        "run",
    ),
    (
        # "Devlog #7 - Adding Proximity Chat to Spooky Shift": the object after "to" is the game
        2,
        re.compile(
            r"(?i:\b(?:add(?:ing|ed|s)?|bring(?:ing|s)?|brought|put(?:ting|s)?|implement(?:ing|ed|s)?)\s+"
            r"(?:[\w\-]+\s+){1,5}?(?:to|in|into)\s+)" + _OPEN_Q + _T
        ),
        "run",
    ),
)
_KEYWORD = re.compile(
    r"(?i)\b(?:game|demo|trailer|teaser|steam|wish[\s-]?list\w*|playtest\w*|co-?op|multiplayer|early\s+access|"
    r"devlogs?|dev\s+log)\b"
)
_COMPARISON_BEFORE = re.compile(
    r"(?i)(?:\blike|\binspired\s+by|\bmeets|\bsimilar\s+to|\bfans?\s+of|\bif\s+you\s+(?:liked?|enjoy(?:ed)?|"
    r"love[ds]?|played)|\bthan|\bvs\.?|\bversus|\bmix\s+of|\bcross\s+between|\breminds?\s+(?:me\s+|you\s+)?of|"
    r"\bin\s+the\s+(?:style|vein|spirit)\s+of|\bsuccessor\s+to|\b(?:clone|rip-?off|knock-?off)\s+of|\bthink)"
    r"\s+[\"“'‘]?$"
)
# "<Reference> but it's a pizza shop", "<Reference> meets Overcooked", "<Reference> clone / style / inspired"
_COMPARISON_AFTER = re.compile(
    r"(?i)^\s*[-–]?\s*(?:but|meets|clone|style|inspired|esque|vibes?|rip-?off|knock-?off|killer|wannabe)\b"
)
_STRIP_CHARS = " \t\r\n\"'“”‘’«»`*_~.,;:!?-–—|/#()[]{}<>"
_SYMBOL_CATEGORIES = frozenset({"So", "Sk", "Cs", "Co", "Cf"})
_URLISH = re.compile(
    r"(?i)(?:https?://|steam://|www\.)\S+|\b[\w.-]+\.(?:com|io|ly|co|gg|net|org|app|team)/\S*"
)
_TAGS = re.compile(r"(?<![\w/])(?:[#@][\w.]+|/?[ru]/[\w\-]+)")  # #tags, @handles, r/Subreddit, u/user
_COMPARISON_WORD = re.compile(r"(?i)-(?:like|inspired|esque|style)$")


def _clean_display(text: str) -> str:
    text = html.unescape(text)
    without = _BRACKETS.sub(" ", text)
    if without.strip(_STRIP_CHARS):
        text = without
    text = "".join(
        ch for ch in text if unicodedata.category(ch) not in _SYMBOL_CATEGORIES and ch not in "\ufe0f\u20e3"
    )
    text = re.sub(r"\s+", " ", text).strip(_STRIP_CHARS)
    text = re.sub(r"['’]s$", "", text)
    return text.strip(_STRIP_CHARS)


def _bare(word: str) -> str:
    return word.lower().strip(",.:;!?\"'“”‘’")


def _trim_run(text: str, *, cut: bool = True) -> str:
    words = text.split()
    while words and _bare(words[0]) in _LEAD:
        words.pop(0)
    for index, word in enumerate(words):
        if cut and index > 0 and _bare(word) in _CUT:
            words = words[:index]
            break
    while words and _bare(words[-1]) in _TRAIL:
        words.pop()
    return " ".join(words)


def _is_allcaps(word: str) -> bool:
    letters = [c for c in word if c.isalpha()]
    return len(letters) >= 3 and all(c.isupper() for c in letters)


def _is_camel(word: str) -> bool:
    return bool(re.match(r"^[A-Z][a-z]+[A-Z]", word))


def _is_gerund(word: str) -> bool:
    return len(word) >= 6 and word.endswith("ing") and word.isalpha() and word not in _GERUND_NOUNS


def _plausible(candidate: str, kind: str = "run") -> bool:
    """2-60 chars, <= 6 words, starts like a name, and is not a generic/reference/sentence phrase.

    ``kind`` is where the candidate came from: "run" (a Title Case run), "segment" (a title
    segment between separators), "quoted", "llm" or "url". Runs and segments additionally
    reject sentence shapes: "Added Ragdolls", "Solo Dev Makes", "Making a Multiplayer Game",
    "Adding Multiplayer to My", and runs reject "Top 10 ..." / "10 Upcoming ..." listicles.
    """
    if not (2 <= len(candidate) <= 60) or not candidate[0].isalnum():
        return False
    words = candidate.split()
    lowered = [_bare(w) for w in words]
    if len(words) > 6 or lowered[0] in _PRONOUN_START or lowered[0] in _COMPARISON_LEAD:
        return False
    if any(_COMPARISON_WORD.search(word) for word in words):
        return False
    norm = normalize_title(candidate)
    if len(norm) < 2 or norm in _GENERIC_PHRASES or norm in _REFERENCE_GAMES:
        return False
    if norm.replace(" ", "") in _REFERENCE_COMPACT:
        return False
    tokens = norm.split()
    if all(t in _GENERIC_WORDS or t.isdigit() for t in tokens):
        return False
    if kind in ("run", "segment", "quoted"):
        listicle = len(lowered) > 1 and lowered[1] in _LISTICLE_NEXT
        if lowered[0] == "top" or (lowered[0][:1].isdigit() and (kind == "run" or listicle)):
            return False
    if kind in ("run", "segment"):
        if any(w in ("my", "our", "your") for w in lowered[1:]):
            return False
        if lowered[0] in _VERB_WORDS or lowered[-1] in _VERB_WORDS:
            return False
        if (
            _is_gerund(lowered[0])
            and (
                len(words) == 1  # "Making", left over from "Making a Multiplayer Game"
                or len(words) >= 4
                or any(w in _FUNCTION_WORDS for w in words[1:])
                or any(t in _FEATURE_WORDS for t in tokens[1:])
            )
        ):
            return False
    return True


def _junky_title(norm: str) -> bool:
    """A normalised title shaped like a sentence/listicle ("making a multiplayer game", "top 10 ...")."""
    tokens = norm.split()
    if not tokens:
        return True
    first = tokens[0]
    if first.isdigit() or first in _VERB_WORDS or first in _GENERIC_WORDS or _is_gerund(first):
        return True
    return sum(t in _GENERIC_WORDS or t.isdigit() for t in tokens) * 2 >= len(tokens)


def _clean_candidate(raw: str, kind: str) -> str | None:
    """kind: "run" (Title Case run: trimmed and cut), "segment" (trimmed only), "quoted" (as is)."""
    text = _clean_display(raw)
    if kind in ("run", "segment"):
        text = _trim_run(text, cut=kind == "run")
    if not text or not (text[0].isupper() or text[0].isdigit()):
        return None
    text = text.strip(_STRIP_CHARS)
    return text if text and _plausible(text, kind) else None


def _candidate_at(text: str, start: int, raw: str, kind: str) -> str | None:
    """Clean a raw candidate found at ``start`` and reject it when the context makes it a comparison."""
    if _COMPARISON_BEFORE.search(text[max(0, start - 40) : start]):
        return None
    cand = _clean_candidate(raw, kind)
    if not cand:
        return None
    index = text.find(cand, start)
    if index != -1 and (
        _COMPARISON_BEFORE.search(text[max(0, index - 40) : index])
        or _COMPARISON_AFTER.match(text[index + len(cand) :])
    ):
        return None
    return cand


def _prep_text(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(text.replace("\\_", "_"))
    text = _URLISH.sub(" ", text)
    return _TAGS.sub(" ", text)


def _segment_candidates(title: str) -> list[tuple[int, int, str]]:
    """``X - Official Trailer`` / ``Devlog #3 | X`` / ``X | Steam``: the named segment is the game."""
    if not (_PIPE_SPLIT.search(title) or _DASH_SPLIT.search(title)):
        return []
    pieces: list[tuple[int, str]] = []
    position = 0
    for chunk in re.split(r"(\s*[|｜]\s*|\s+[-~]\s+|\s*[–—]\s*|\s*:\s+|\s+//\s+)", title):
        if chunk and not re.fullmatch(r"\s*[|｜]\s*|\s+[-~]\s+|\s*[–—]\s*|\s*:\s+|\s+//\s+", chunk):
            pieces.append((position, chunk))
        position += len(chunk)
    if len(pieces) < 2:
        return []
    noise = [_is_noise_segment(chunk) for _, chunk in pieces]
    has_pipe = bool(_PIPE_SPLIT.search(title))
    out: list[tuple[int, int, str]] = []
    for (pos, chunk), is_noise in zip(pieces, noise, strict=True):
        if is_noise:
            continue
        priority = 2 if any(noise) else (3 if has_pipe and pos == 0 else 0)
        if priority == 0:
            continue
        whole = _candidate_at(title, pos, chunk, "segment")
        if whole:
            out.append((priority, pos, whole))
            continue
        runs = list(_RUN_RX.finditer(chunk))
        if (
            runs
        ):  # e.g. "After 3 years, Gorilla Pizza Panic: Official Trailer" -> the run nearest the separator
            last = runs[-1]
            cand = _candidate_at(title, pos + last.start(), last.group(0), "run")
            if cand:
                out.append((priority + 1, pos + last.start(), cand))
    return out


def _near_keyword_candidates(text: str, offset: int, context: str = "") -> list[tuple[int, int, str]]:
    """Title Case runs within 60 chars of a game word; ``context`` (the body) can supply the word."""
    keywords = [m.span() for m in _KEYWORD.finditer(f"{text}\n{context}" if context else text)]
    if not keywords:
        return []
    out: list[tuple[int, int, str]] = []
    for match in _RUN_RX.finditer(text):
        start, end = match.span()
        distance = min(
            0 if (k_start < end and k_end > start) else min(abs(k_start - end), abs(start - k_end))
            for k_start, k_end in keywords
        )
        if distance > 60:
            continue
        cand = _candidate_at(text, start, match.group(0), "run")
        if not cand:
            continue
        words = cand.split()
        if len(words) >= 2 or _is_allcaps(words[0]) or _is_camel(words[0]):
            out.append((5, offset + start, cand))
    return out


def _source_title(mention: Mention) -> str:
    if mention.source == "steam":
        info = mention.extra.get("steam")
        name = info.get("name") if isinstance(info, dict) else getattr(info, "name", None)
        if name:
            return str(name).strip()
    return _clean_display(mention.title) if mention.title else ""


def _scored_candidates(mention: Mention) -> list[tuple[int, int, str]]:
    if mention.source in ("steam", "itch"):
        title = _source_title(mention)
        return [(0, 0, title)] if title and len(title) <= 100 and normalize_title(title) else []
    out: list[tuple[int, int, str]] = []
    sources = (
        (0, _prep_text(mention.title[:MAX_TITLE_CHARS]), True),
        (_TEXT_OFFSET, _prep_text(mention.text[:MAX_TEXT_CHARS]), False),
    )
    for offset, text, is_title in sources:
        if not text.strip():
            continue
        if is_title:
            out.extend((p, offset + pos, c) for p, pos, c in _segment_candidates(text))
        for priority, pattern, kind in _PATTERNS:
            for match in pattern.finditer(text):
                start = match.start("t")
                cand = _candidate_at(text, start, match.group("t"), kind)
                if cand:
                    out.append((priority, offset + start, cand))
        context = _prep_text(mention.text[:120]) if is_title else ""
        out.extend(_near_keyword_candidates(text, offset, context))
    return out


def title_candidates(mention: Mention) -> list[str]:
    """Candidate game titles for a mention, best first (deduplicated by normalised form)."""
    seen: set[str] = set()
    out: list[str] = []
    for _, _, cand in sorted(_scored_candidates(mention), key=lambda item: (item[0], item[1])):
        key = normalize_title(cand)
        if key and key not in seen:
            seen.add(key)
            out.append(cand)
    return out[:MAX_CANDIDATES]


_FIRST_PERSON = re.compile(
    r"(?i)\b(?:my|our)\s+(?!(?:fav(?:ou?rite)?|all[\s-]time)\b)(?:[\w\-]+\s+){0,3}?"
    r"(?:game|project|studio|team|demo|steam\s+page|trailer|devlog)\b"
    r"|\bi\s*(?:'|’)?ve\s+been\s+(?:working|making|building|developing)"
    r"|\bi\s+(?:just\s+)?(?:made|built|created|developed|released|launched|am\s+(?:making|building|developing))\b"
    r"|\bi\s*(?:'|’)?m\s+(?:making|building|developing|working\s+on)"
    r"|\bwe\s*(?:'|’)?re\s+(?:making|building|developing|working\s+on|a\s+(?:small|tiny|two|2|three|3|solo|indie))"
    r"|\bwe\s+(?:just\s+)?(?:made|built|created|released|launched|announced|dropped)\b"
    r"|\bwe\s+are\s+(?:making|building|developing)"
    r"|\bwe\s*(?:'|’)?ve\s+been\s+(?:working|making|building|developing)"
    r"|\bsolo\s+dev\b|\bme\s+and\s+my\s+(?:friend|friends|brother|sister|wife|husband|partner|buddy|team)\s+"
    r"(?:made|are\s+making|built)"
)


def developer_hint(mention: Mention) -> str | None:
    """Steam developer / itch subdomain / the author of a first-person post ("my game", "we made")."""
    if mention.source == "steam":
        info = mention.extra.get("steam")
        devs = info.get("developers") if isinstance(info, dict) else getattr(info, "developers", None)
        if devs:
            return str(devs[0]).strip() or None
        return None
    if mention.source == "itch":
        own = _own_hard_id(mention)
        return own.split(":", 1)[1].split("/", 1)[0] if own else (mention.author or None)
    if mention.author and _FIRST_PERSON.search(
        f"{mention.title[:MAX_TITLE_CHARS]}\n{mention.text[:MAX_TEXT_CHARS]}"
    ):
        return mention.author.strip() or None
    return None


_DEV_NOISE_WORDS = _wordset(
    "games game studio studios dev devs developer developers team interactive entertainment llc ltd inc "
    "official hq the co gamedev indiedev"
)
_DEV_NOISE_SUFFIXES = (
    "gamedev",
    "games",
    "game",
    "studios",
    "studio",
    "devs",
    "dev",
    "team",
    "official",
    "hq",
)


def _dev_key(name: str | None) -> str:
    """Compact developer identity: "gorilla_dev", "Gorilla Games", "gorilladev.bsky.social" -> "gorilla"."""
    if not name:
        return ""
    text = _fold(str(name)).strip().lstrip("@")
    text = re.sub(r"^/?u/", "", text, flags=re.I)
    text = re.sub(r"\.bsky\.social$", "", text, flags=re.I)
    if "." in text and " " not in text:
        text = text.split(".")[0]
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text).lower()
    words = [w for w in re.split(r"[^a-z0-9]+", text) if w]
    kept = [w for w in words if w not in _DEV_NOISE_WORDS] or words
    compact = "".join(kept)
    for suffix in _DEV_NOISE_SUFFIXES:
        if compact.endswith(suffix) and len(compact) - len(suffix) >= 3:
            compact = compact[: -len(suffix)]
            break
    return compact


def _dev_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return a == b or (min(len(a), len(b)) >= 4 and fuzz.ratio(a, b) >= DEV_MATCH_RATIO)


def _title_from_url(url: str) -> str | None:
    """ "/app/123/Gorilla_Pizza_Panic/" or "dev.itch.io/gorilla-pizza-panic" -> "Gorilla Pizza Panic"."""
    split = _split_url(url)
    if not split:
        return None
    _, host, path, _ = split
    segments = [unquote(s) for s in path.split("/") if s]
    lowered = [s.lower() for s in segments]
    slug = None
    if host in _STEAM_STORE_HOSTS and "app" in lowered:
        index = lowered.index("app")
        if len(segments) > index + 2 and segments[index + 1].isdigit():
            slug = segments[index + 2]
    elif host.endswith(".itch.io") and segments:
        slug = segments[0]
    if not slug or slug.isdigit():
        return None
    words = [w for w in re.split(r"[_\-\s]+", slug) if w]
    title = " ".join(w if any(c.isupper() for c in w) else w.capitalize() for w in words)
    return title if 2 <= len(title) <= 60 else None


# --------------------------------------------------------------------------------------
# Resolver
# --------------------------------------------------------------------------------------


@dataclass
class ResolveResult:
    assignments: dict[str, str] = field(default_factory=dict)  # mention key -> game_id
    new_games: list[str] = field(default_factory=list)
    merged: dict[str, str] = field(default_factory=dict)  # absorbed game_id -> surviving game_id
    dropped: list[str] = field(default_factory=list)  # mention keys that resolved to no game


def _kind(hard_id: str) -> str:
    return hard_id.split(":", 1)[0]


def _attachable(hard: list[str], same_game: Callable[[str, str], bool] | None = None) -> list[str]:
    """Hard ids a mention may attach to its game.

    Only "one Steam + one itch link" can both belong to the post's game, and only when their
    names agree (``same_game``); otherwise just the leading id counts (e.g. a list of games,
    or the dev's itch page next to a Steam game it is compared with).
    """
    if len(hard) <= 1:
        return list(hard)
    steam = [h for h in hard if h.startswith("steam:")]
    itch = [h for h in hard if h.startswith("itch:")]
    if len(steam) == 1 and len(itch) == 1 and (same_game is None or same_game(steam[0], itch[0])):
        return [hard[0], hard[1]]
    return [hard[0]]


def _steam_info(mention: Mention) -> SteamInfo | None:
    info = mention.extra.get("steam")
    if isinstance(info, SteamInfo):
        return info
    if isinstance(info, dict):
        try:
            return SteamInfo.model_validate(info)
        except Exception:
            return None
    return None


def _mention_time(mention: Mention) -> datetime:
    """When GemBot first saw the mention (``first_seen``, set by the pipeline); else when it was posted."""
    return mention.first_seen if mention.first_seen is not None else mention.created_at


def _fallback_title(hard_id: str) -> str:
    kind, value = hard_id.split(":", 1)
    if kind == "itch":
        return " ".join(w.capitalize() for w in re.split(r"[_\-]+", value.split("/", 1)[1]) if w) or value
    return f"Steam app {value}"


def _retitle(game: Game, title: str) -> None:
    """Change the display title; the old one is kept as an alias for fuzzy matching."""
    title = title.strip()
    if not title or title == game.title:
        return
    old = game.title
    game.title = title
    game.aliases = [a for a in game.aliases if normalize_title(a) != normalize_title(title)]
    Resolver._add_alias(game, old)


def apply_steam_info(game: Game, info: SteamInfo) -> None:
    """Store Steam appdetails on a game: Steam name becomes the title, plus developer/publisher/thumb.

    Used by the resolver for Steam mentions and by the enrichment stage after
    ``SteamCollector.fetch_appdetails`` for games that were only *linked* from other posts.
    """
    if game.steam_appid not in (None, info.appid):
        return
    game.steam_appid = info.appid
    if f"steam:{info.appid}" not in game.hard_ids:
        game.hard_ids.append(f"steam:{info.appid}")
    game.steam = info
    if info.name:
        _retitle(game, info.name)
    if info.developers:
        game.developer = info.developers[0]
    if info.publishers:
        game.publisher = info.publishers[0]
    if info.header_image:
        game.thumb = info.header_image


def apply_llm_title(game: Game, title: str | None) -> bool:
    """Retitle a game from an LLM verdict's ``game_title``; returns True when something changed.

    Only games without an authoritative name are touched (no Steam details, no itch page),
    and only with a plausible title (not generic, not a reference game). The old title is
    kept as an alias so later mentions still match.
    """
    if (
        not title
        or game.steam is not None
        or game.itch_url
        or any(h.startswith("itch:") for h in game.hard_ids)
    ):
        return False
    clean = _clean_display(title)
    if not clean or not _plausible(clean, "llm"):
        return False
    before = (game.title, list(game.aliases))
    _retitle(game, clean)
    return (game.title, game.aliases) != before


def title_game_id(title: str, developer: str | None = None) -> str:
    """``t:<slug>-<6 hex>``: deterministic from the normalised title and developer."""
    norm = normalize_title(title) or _clean_display(title).lower() or "game"
    slug = "-".join(norm.split())[:40].strip("-") or "game"
    digest = hashlib.sha1(f"{norm}|{_dev_key(developer)}".encode()).hexdigest()[:6]
    return f"t:{slug}-{digest}"


class Resolver:
    """Clusters mentions into :class:`Game` records (mutates ``games``, sets ``mention.game_id``)."""

    def __init__(
        self,
        games: dict[str, Game],
        mentions: dict[str, Mention],
        *,
        now: datetime,
        settings: ResolverSource,
        expander: LinkExpander | None = None,
        llm_titles: dict[str, str] | None = None,
    ):
        self.games = games
        self.mentions = mentions
        self.now = now
        self.settings = settings
        self.expander = expander
        self.llm_titles = dict(llm_titles or {})
        self.result = ResolveResult()
        self._by_hard: dict[str, str] = {}
        self._by_key: dict[str, str] = {}
        self._names: list[str] = []
        self._name_gids: list[str] = []
        self._name_seen: set[tuple[str, str]] = set()
        self._devs: dict[str, set[str]] = {}

    # ---- public -------------------------------------------------------
    def resolve(self, new_mentions: list[Mention]) -> ResolveResult:
        self.result = ResolveResult()
        self._rebuild_indexes()
        ordered = sorted(new_mentions, key=lambda m: (m.created_at, m.key))
        prepared = [(m, hard_ids_for(m, self.expander)) for m in ordered]
        prepared.sort(key=lambda item: 0 if item[1] else 1)  # stable: hard-id mentions first
        for mention, hard in prepared:
            self._resolve_one(mention, hard)
        result = self.result
        result.assignments = {key: self._final(gid) for key, gid in result.assignments.items()}
        result.new_games = _unique(g for g in result.new_games if g in self.games)
        result.dropped = _unique(k for k in result.dropped if k not in result.assignments)
        return result

    # ---- indexes ------------------------------------------------------
    def _rebuild_indexes(self) -> None:
        self._by_hard.clear()
        self._by_key.clear()
        self._names.clear()
        self._name_gids.clear()
        self._name_seen.clear()
        self._devs.clear()
        for game in sorted(self.games.values(), key=lambda g: (g.first_seen, g.game_id)):
            if self.games.get(game.game_id) is not game:
                continue  # absorbed by an earlier merge in this loop
            for hard in self._game_hard_ids(game):
                owner = self._by_hard.get(hard)
                if owner == game.game_id:
                    continue
                if owner and owner in self.games:
                    game = self._merge(self.games[owner], game)
                else:
                    self._add_hard_id(game, hard)
            for key in game.mention_keys:
                self._by_key.setdefault(key, game.game_id)
        cutoff = self.now - timedelta(days=self.settings.lookback_days)
        for gid in sorted(self.games):
            if self.games[gid].last_seen >= cutoff:
                self._register_names(self.games[gid])

    @staticmethod
    def _game_hard_ids(game: Game) -> list[str]:
        ids = list(game.hard_ids)
        if game.game_id.startswith(("steam:", "itch:")):
            ids.insert(0, game.game_id)
        if game.steam_appid:
            ids.append(f"steam:{game.steam_appid}")
        if game.itch_url:
            itch = canonical_hard_id(game.itch_url)
            if itch:
                ids.append(itch)
        return _unique(ids)

    def _register_names(self, game: Game) -> None:
        for name in (game.title, *game.aliases):
            norm = normalize_title(name)
            if norm and (game.game_id, norm) not in self._name_seen:
                self._name_seen.add((game.game_id, norm))
                self._names.append(norm)
                self._name_gids.append(game.game_id)

    def _game_devs(self, game: Game) -> set[str]:
        devs = self._devs.get(game.game_id)
        if devs is None:
            devs = {_dev_key(game.developer)} if game.developer else set()
            for key in game.mention_keys:
                stored = self.mentions.get(key)
                if stored is not None:
                    devs.add(_dev_key(developer_hint(stored)))
            devs.discard("")
            self._devs[game.game_id] = devs
        return devs

    def _final(self, gid: str | None) -> str | None:
        seen: set[str] = set()
        while gid in self.result.merged and gid not in seen:
            seen.add(gid)
            gid = self.result.merged[gid]
        return gid

    # ---- resolution ---------------------------------------------------
    def _candidates(self, mention: Mention, hard: list[str]) -> list[str]:
        cands: list[str] = []
        llm = self.llm_titles.get(mention.key)
        if llm:
            llm_clean = _clean_display(llm)
            if llm_clean and (mention.source in ("steam", "itch") or _plausible(llm_clean, "llm")):
                cands.append(llm_clean)
        cands.extend(title_candidates(mention))
        if hard:
            for url in _mention_urls(mention):
                if canonical_hard_id(url) == hard[0]:
                    derived = _title_from_url(url)
                    if derived and _plausible(derived, "url"):
                        cands.append(derived)
        seen: set[str] = set()
        out: list[str] = []
        for cand in cands:
            key = normalize_title(cand)
            if key and key not in seen:
                seen.add(key)
                out.append(cand)
        return out

    def _existing_game(self, mention: Mention) -> Game | None:
        gid = mention.game_id
        stored = self.mentions.get(mention.key)
        if not gid and stored is not None:
            gid = stored.game_id
        if not gid:
            gid = self._by_key.get(mention.key)
        gid = self._final(gid)
        return self.games.get(gid) if gid else None

    def _id_names(self, hard_id: str, mention: Mention) -> list[str]:
        """What a hard id is called: URL slugs in the post, the listing's own title, the known game."""
        names = [
            t
            for url in _mention_urls(mention)
            if canonical_hard_id(url) == hard_id and (t := _title_from_url(url))
        ]
        if hard_id == _own_hard_id(mention):
            names.append(_source_title(mention))
        owner = self.games.get(self._by_hard.get(hard_id, ""))
        if owner is not None:
            names.extend([owner.title, *owner.aliases])
        return [n for n in names if n]

    def _same_game(self, mention: Mention, cands: list[str]) -> Callable[[str, str], bool]:
        def check(steam_id: str, itch_id: str) -> bool:
            steam_names = self._id_names(steam_id, mention) or cands[:1]
            itch_names = self._id_names(itch_id, mention) or cands[:1]
            return any(_names_agree(a, b) for a in steam_names for b in itch_names)

        return check

    def _resolve_one(self, mention: Mention, hard: list[str]) -> None:
        dev = developer_hint(mention)
        cands = self._candidates(mention, hard)
        attach = _attachable(hard, self._same_game(mention, cands))
        game = self._existing_game(mention)
        if game is None:
            for hard_id in attach:
                owner = self._by_hard.get(hard_id)
                if owner and owner in self.games:
                    game = self.games[owner]
                    break
        if game is None:
            game = self._fuzzy_match(cands[:MAX_FUZZY_CANDIDATES], dev, {_kind(h) for h in attach})
        if game is None:
            if attach:
                title = cands[0] if cands else _fallback_title(attach[0])
                game = self._create(attach[0], title, mention)
            elif cands:
                game = self._create(title_game_id(cands[0], dev), cands[0], mention)
            else:
                self.result.dropped.append(mention.key)
                return
        self._add_mention(game, mention, attach, cands, dev)

    def _fuzzy_match(self, cands: list[str], dev: str | None, exclude_kinds: set[str]) -> Game | None:
        if not self._names:
            return None
        dev_key = _dev_key(dev)
        threshold = self.settings.fuzzy_threshold
        for index, cand in enumerate(cands):
            norm = normalize_title(cand)
            if not norm:
                continue
            best: tuple[tuple[int, int, int, str], Game] | None = None
            matches = process.extract(
                norm, self._names, scorer=fuzz.token_set_ratio, score_cutoff=threshold, limit=None
            )
            for name, set_score, position in matches:
                game = self.games.get(self._final(self._name_gids[position]) or "")
                if game is None:
                    continue
                if exclude_kinds and any(_kind(h) in exclude_kinds for h in self._game_hard_ids(game)):
                    continue  # it already has a different Steam app / itch page
                ratio = fuzz.ratio(norm, name)
                if not self._compatible(game, dev_key, norm, name, ratio):
                    continue
                rank = (index, -int(ratio), -int(set_score), game.game_id)
                if best is None or rank < best[0]:
                    best = (rank, game)
            if best is not None:
                return best[1]
        return None

    def _compatible(self, game: Game, dev_key: str, norm: str, name: str, ratio: float) -> bool:
        devs = self._game_devs(game)
        if dev_key and devs:
            if any(_dev_match(dev_key, other) for other in devs):
                return True
            # different known developers: only an identical, distinctive (2+ word) name merges
            return norm == name and len(norm.split()) >= 2 and len(norm) >= 8 and not _junky_title(norm)
        return ratio >= STRICT_TITLE_RATIO

    def _create(self, game_id: str, title: str, mention: Mention) -> Game:
        existing = self.games.get(game_id)
        if existing is not None:  # same title + developer seen long ago: revive it
            return existing
        game = Game(game_id=game_id, title=title, first_seen=_mention_time(mention), last_seen=self.now)
        self.games[game_id] = game
        self.result.new_games.append(game_id)
        return game

    def _add_hard_id(self, game: Game, hard_id: str) -> None:
        if hard_id not in game.hard_ids:
            game.hard_ids.append(hard_id)
        kind, value = hard_id.split(":", 1)
        if kind == "steam" and game.steam_appid is None:
            game.steam_appid = int(value)
        elif kind == "itch" and game.itch_url is None:
            dev, slug = value.split("/", 1)
            game.itch_url = f"https://{dev}.itch.io/{slug}"
        self._by_hard[hard_id] = game.game_id

    def _attach(self, game: Game, hard_ids: list[str]) -> Game:
        for hard_id in hard_ids:
            owner = self._by_hard.get(hard_id)
            if owner == game.game_id:
                continue
            if owner and owner in self.games:
                game = self._merge(self.games[owner], game)
                self._add_hard_id(game, hard_id)
                continue
            if any(_kind(h) == _kind(hard_id) and h != hard_id for h in self._game_hard_ids(game)):
                continue  # never give one game two different Steam apps / itch pages
            self._add_hard_id(game, hard_id)
        return game

    def _set_title(self, game: Game, title: str) -> None:
        _retitle(game, title)

    @staticmethod
    def _add_alias(game: Game, alias: str) -> None:
        norm = normalize_title(alias)
        if not norm or norm == normalize_title(game.title):
            return
        if any(normalize_title(a) == norm for a in game.aliases) or len(game.aliases) >= MAX_ALIASES:
            return
        game.aliases.append(alias.strip())

    def _add_mention(
        self, game: Game, mention: Mention, attach: list[str], cands: list[str], dev: str | None
    ) -> None:
        game = self._attach(game, attach)
        info = _steam_info(mention)
        if info is not None and game.steam_appid not in (None, info.appid):
            info = None  # details of another app (e.g. a linked sequel): not this game's
        if info is not None:
            apply_steam_info(game, info)
        elif mention.source == "itch" and mention.title and game.steam is None:
            self._set_title(game, _clean_display(mention.title) or mention.title)
        if dev and not game.developer:
            game.developer = dev
        if not game.thumb and mention.media_thumb:
            game.thumb = mention.media_thumb
        for cand in cands[:1]:
            self._add_alias(game, cand)
        if mention.key not in game.mention_keys:
            game.mention_keys.append(mention.key)
        game.first_seen = min(game.first_seen, _mention_time(mention))
        game.last_seen = max(game.last_seen, self.now)
        mention.game_id = game.game_id
        stored = self.mentions.get(mention.key)
        if stored is not None and stored is not mention:
            stored.game_id = game.game_id
        self._by_key[mention.key] = game.game_id
        devs = self._game_devs(game)
        for name in (dev, game.developer):
            key = _dev_key(name)
            if key:
                devs.add(key)
        self._register_names(game)
        self.result.assignments[mention.key] = game.game_id

    def _merge(self, a: Game, b: Game) -> Game:
        """Absorb the newer game into the older one; returns the survivor."""
        older, newer = sorted((a, b), key=lambda g: (g.first_seen, g.game_id))
        for key in newer.mention_keys:
            if key not in older.mention_keys:
                older.mention_keys.append(key)
            self._by_key[key] = older.game_id
            stored = self.mentions.get(key)
            if stored is not None:
                stored.game_id = older.game_id
        older.first_seen = min(older.first_seen, newer.first_seen)
        older.last_seen = max(older.last_seen, newer.last_seen)
        for field_name in ("developer", "publisher", "pitch", "thumb", "llm", "last_score", "last_scored_at"):
            if getattr(older, field_name) is None and getattr(newer, field_name) is not None:
                setattr(older, field_name, getattr(newer, field_name))
        if older.last_features is None and newer.last_features is not None:
            older.last_features = newer.last_features
            older.last_reasons = list(newer.last_reasons)
        if older.steam is None and newer.steam is not None:
            older.steam = newer.steam
            if newer.steam.name:
                self._set_title(older, newer.steam.name)
        for alias in (newer.title, *newer.aliases):
            self._add_alias(older, alias)
        del self.games[newer.game_id]
        for hard_id in self._game_hard_ids(newer):
            self._add_hard_id(older, hard_id)  # union; steam_appid / itch_url keep the older's values
        self.result.merged[newer.game_id] = older.game_id
        for absorbed, survivor in list(self.result.merged.items()):
            if survivor == newer.game_id:
                self.result.merged[absorbed] = older.game_id
        self._name_gids = [older.game_id if gid == newer.game_id else gid for gid in self._name_gids]
        self._devs.pop(newer.game_id, None)
        self._devs.pop(older.game_id, None)  # recomputed lazily from the merged mention list
        self._register_names(older)
        return older
