"""Bluesky collector: post search, reply threads and follower counts.

What the live service allows (checked 2026-10 against the @atproto source and GitHub
issues; see docs/VERIFICATION.md):

* ``app.bsky.feed.searchPosts`` needs a login. Logged out, the CDN in front of
  ``api.bsky.app`` and ``public.api.bsky.app`` answers ``403`` with an HTML page.
* Logged in, we search through the account's PDS (``didDoc`` service ``#atproto_pds``)
  with ``Authorization: Bearer`` and ``atproto-proxy: did:web:api.bsky.app#bsky_appview``.
  ``https://api.bsky.app`` with the same token is the fallback route. A token is never
  sent to ``public.api.bsky.app``.
* bsky.social allows only ~10 ``createSession`` calls per account per day, and GemBot
  runs 48 times a day. So the session is kept between runs and renewed with
  ``refreshSession`` (which rotates the refresh token). The state branch may be public,
  so the session is stored **encrypted** with a key derived from the app password:
  reading the state without the password reveals nothing, and revoking the app password
  kills the stored session. New logins are capped per 24 hours (``max_sessions_per_day``).
* ``createSession`` answers the same ``401`` for a wrong handle and a wrong password. After
  one, GemBot asks the public AppView (``resolveHandle``, no token) whether the handle exists,
  once per set of credentials (again with the daily login retry only if the check itself
  failed), so the error can say which secret to fix. A handle that may hold a password (an
  App Password pasted into it, the two secrets swapped, or extra text after ``.bsky.social``)
  is never looked up: the lookup is a GET, so the value would end up in a URL.
* Without credentials we try public search at most once every ``unauth_probe_hours``
  and otherwise skip quietly (a skip is not a failure).
* Replies (``getPostThread``) and follower counts (``getProfile(s)``) work logged out on
  ``public.api.bsky.app``, so enrichment never needs the session.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken

from gembot.collectors.base import CollectContext, Collector
from gembot.http import Budget, BudgetExceeded, HttpError, RateLimited
from gembot.models import Comment, Engagement, Mention, ensure_utc

__all__ = [
    "LOGIN_LIMIT_WARNING",
    "PUBLIC_BLOCKED_REASON",
    "BlueskyCollector",
    "BlueskySession",
    "LoginError",
    "LoginLimitReached",
    "SearchRefused",
    "SessionBox",
    "XrpcError",
    "parse_at_uri",
    "parse_post",
    "post_url",
]

SEARCH_POSTS = "/xrpc/app.bsky.feed.searchPosts"
CREATE_SESSION = "/xrpc/com.atproto.server.createSession"
REFRESH_SESSION = "/xrpc/com.atproto.server.refreshSession"
GET_POST_THREAD = "/xrpc/app.bsky.feed.getPostThread"
GET_PROFILE = "/xrpc/app.bsky.actor.getProfile"
GET_PROFILES = "/xrpc/app.bsky.actor.getProfiles"
RESOLVE_HANDLE = "/xrpc/com.atproto.identity.resolveHandle"

POST_COLLECTION = "app.bsky.feed.post"
THREAD_VIEW = "app.bsky.feed.defs#threadViewPost"
LINK_FACET = "app.bsky.richtext.facet#link"
TAG_FACET = "app.bsky.richtext.facet#tag"

ACCESS_TTL = timedelta(minutes=110)  # access tokens live 120 minutes
ACCESS_MARGIN = timedelta(minutes=5)  # renew when less than this is left
LOGIN_WINDOW = timedelta(hours=24)
KDF_SALT_PREFIX = b"gembot-bluesky-v1:"
KDF_ITERATIONS = 200_000
TOKEN_ERRORS = frozenset({"ExpiredToken", "InvalidToken"})
ROUTES = ("pds", "appview")
PROFILES_BATCH = 25  # getProfiles accepts at most 25 actors
TITLE_MAX = 120
# Authors who opted out of logged-out visibility (we republish to Discord), moderation
# hides, and adult / graphic content.
SKIP_LABELS = frozenset(
    {"!no-unauthenticated", "!hide", "!takedown", "porn", "sexual", "nudity", "graphic-media"}
)

PUBLIC_BLOCKED_REASON = (
    "Bluesky search needs BLUESKY_HANDLE + BLUESKY_APP_PASSWORD (public search is blocked)"
)
LOGIN_LIMIT_WARNING = "Bluesky login limit reached; will retry later"

# What the handle check said about BLUESKY_HANDLE after a rejected login (stored in scratch).
HANDLE_FOUND = "found"  # the account exists: probably the App Password is wrong
HANDLE_MISSING = "missing"  # no such handle on Bluesky (or not even handle syntax)
HANDLE_UNKNOWN = "unknown"  # the check itself failed (5xx, network, odd answer)
HANDLE_SKIPPED = "skipped"  # an email address or a DID: nothing to look up
HANDLE_MIXED_UP = "mixed_up"  # BLUESKY_HANDLE seems to hold a password: never looked up
HANDLE_EXTRA_TEXT = "extra_text"  # text after .bsky.social (often a pasted password): never looked up
# Answers only a resolveHandle request can give. The others are worked out again on every run.
LOOKUP_ANSWERS = frozenset({HANDLE_FOUND, HANDLE_MISSING, HANDLE_UNKNOWN})

APP_PASSWORDS = "Settings -> Privacy and security -> App passwords"
APP_PASSWORD_SHAPE = "xxxx-xxxx-xxxx-xxxx"
BSKY_SOCIAL = ".bsky.social"
ADDED_BSKY_SOCIAL = "added .bsky.social"  # the _clean_handle fix that guesses the domain
_APP_PASSWORD_RE = re.compile(r"[a-z0-9]{4}(?:-[a-z0-9]{4}){3}")
_APP_PASSWORD_RUNS = re.compile(r"(?=([a-z0-9]{4}(?:-[a-z0-9]{4}){3}))")  # every (overlapping) run
_PROFILE_LINK_RE = re.compile(r"(?:https?://)?(?:www\.)?bsky\.app/profile/([^/?#]+)", re.IGNORECASE)
# atproto handle syntax: dot-separated labels, the last one starting with a letter
_HANDLE_RE = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?", re.IGNORECASE
)
_DID_PREFIXES = ("did:plc:", "did:web:")
_REJECTED_PREFIX = "BLUESKY_APP_PASSWORD rejected"  # also marks entries stored before handle checks


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class XrpcError(HttpError):
    """An XRPC call answered with an error. ``error`` is the XRPC error name, if any."""

    def __init__(
        self, message: str, status: int | None = None, url: str | None = None, error: str | None = None
    ):
        super().__init__(message, status, url)
        self.error = error


class SearchRefused(HttpError):
    """Bluesky refused search in a way that will not change during this run: stop searching."""


class LoginError(RuntimeError):
    """Logging in failed. The message says what the user has to fix."""


class LoginLimitReached(LoginError):
    """``max_sessions_per_day`` new logins happened in the last 24 hours. ``retry_at``: when
    enough of them have left the window to allow the next one (``None`` when the cap allows no
    logins at all)."""

    def __init__(self, message: str, retry_at: datetime | None = None):
        super().__init__(message)
        self.retry_at = retry_at


@dataclass(frozen=True)
class _Reply:
    status: int
    data: Any  # parsed JSON body (dict for XRPC), or None
    html: bool  # an HTML page: the CDN refused the request before it reached Bluesky
    url: str

    @property
    def error(self) -> str | None:
        if isinstance(self.data, dict) and self.data.get("error"):
            return str(self.data["error"])
        return None

    def describe(self) -> str:
        if self.html:
            return f"HTTP {self.status} (HTML page from the CDN: request refused)"
        text = f"HTTP {self.status}" + (f" {self.error}" if self.error else "")
        message = self.data.get("message") if isinstance(self.data, dict) else None
        return f"{text}: {str(message)[:200]}" if message else text


# --------------------------------------------------------------------------------------
# Session + encryption
# --------------------------------------------------------------------------------------


@dataclass
class BlueskySession:
    access_jwt: str = field(repr=False)
    refresh_jwt: str = field(repr=False)
    access_exp: datetime
    did: str
    handle: str
    pds: str

    def access_valid(self, now: datetime) -> bool:
        return self.access_exp - now > ACCESS_MARGIN

    def to_json(self) -> dict[str, str]:
        return {
            "accessJwt": self.access_jwt,
            "refreshJwt": self.refresh_jwt,
            "access_exp": _iso(self.access_exp),
            "did": self.did,
            "handle": self.handle,
            "pds": self.pds,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> BlueskySession:
        exp = _parse_dt(data.get("access_exp"))
        if exp is None:
            raise ValueError("stored session has no access_exp")
        return cls(
            access_jwt=str(data["accessJwt"]),
            refresh_jwt=str(data["refreshJwt"]),
            access_exp=exp,
            did=str(data.get("did") or ""),
            handle=str(data.get("handle") or ""),
            pds=str(data.get("pds") or ""),
        )

    @classmethod
    def from_response(
        cls, data: Any, *, now: datetime, default_pds: str, previous: BlueskySession | None = None
    ) -> BlueskySession:
        """Build a session from a createSession / refreshSession response."""
        if not isinstance(data, dict):
            raise LoginError("Bluesky login response is not a JSON object")
        if data.get("active") is False:
            status = data.get("status") or "inactive"
            raise LoginError(
                f"Bluesky account is not active ({status}); reactivate it or use another account"
            )
        access, refresh = data.get("accessJwt"), data.get("refreshJwt")
        if not (isinstance(access, str) and access and isinstance(refresh, str) and refresh):
            raise LoginError("Bluesky login response has no tokens")
        pds = pds_from_did_doc(data.get("didDoc")) or (previous.pds if previous else "") or default_pds
        return cls(
            access_jwt=access,
            refresh_jwt=refresh,
            access_exp=now + ACCESS_TTL,
            did=str(data.get("did") or (previous.did if previous else "")),
            handle=str(data.get("handle") or (previous.handle if previous else "")),
            pds=pds.rstrip("/"),
        )


def _is_email(identifier: str) -> bool:
    return "@" in identifier[1:]


def _is_did(identifier: str) -> bool:
    return identifier.lower().startswith(_DID_PREFIXES)


def _is_handle(identifier: str) -> bool:
    return len(identifier) <= 253 and bool(_HANDLE_RE.fullmatch(identifier))


def _clean_handle(raw: str) -> tuple[str, list[str]]:
    """``BLUESKY_HANDLE`` the way Bluesky wants it, plus what was fixed (never the value itself).

    Fixes the usual paste mistakes: spaces, a profile link (``https://bsky.app/profile/<handle>``),
    leading ``@``s, a trailing ``/`` or ``.``, capitals, and a bare username (``name`` becomes
    ``name.bsky.social``). An email address is only trimmed (and loses leading ``@``s, as it
    always did); a DID is kept as it is.
    """
    value = raw.strip()
    fixes = ["removed spaces"] if value != raw else []
    link = _PROFILE_LINK_RE.match(value)  # stops at the next "/", so ".../post/<id>" is dropped too
    if link:
        value = link.group(1)
        fixes.append("took the handle from a profile link")
    if value.startswith("@"):  # before the email test: "@me@example.com" is an email after this
        value = value.lstrip("@")
        fixes.append("removed a leading @")
    if _is_email(value):
        return value, fixes
    if value.endswith("/"):
        value = value.rstrip("/")
        fixes.append("removed a trailing /")
    if _is_did(value):
        return value, fixes
    if value.endswith("."):  # e.g. copied from the end of a sentence; a handle never ends in a dot
        value = value.rstrip(".")
        fixes.append("removed a trailing dot")
    if not value:
        return value, fixes
    if value != value.lower():
        value = value.lower()
        fixes.append("made it lowercase")
    if "." not in value:
        value += BSKY_SOCIAL
        fixes.append(ADDED_BSKY_SOCIAL)
    return value, fixes


def _has_extra_text(identifier: str) -> bool:
    """Something follows ``.bsky.social`` (e.g. a password pasted after the handle; the spaces
    in between are gone by now). Such a value must never go into a lookup URL."""
    return BSKY_SOCIAL in identifier and not identifier.endswith(BSKY_SOCIAL)


def _may_hold_password(identifier: str, fixes: list[str], password: str) -> bool:
    """BLUESKY_HANDLE (cleaned up into ``identifier`` by ``fixes``) seems to hold a password: an
    App Password as the whole value typed, the value of BLUESKY_APP_PASSWORD anywhere in it, or
    the two secrets swapped (BLUESKY_HANDLE is no full handle, BLUESKY_APP_PASSWORD is one).
    Such a value must never go into a lookup URL.

    A full handle whose first label happens to have the App Password shape
    (``game-devs-team-blog.bsky.social``) is a handle: only a bare value (GemBot added
    ``.bsky.social``) is held against the shape."""
    password = password.lower()
    typed = identifier.split(".", 1)[0] if ADDED_BSKY_SOCIAL in fixes else identifier
    if _APP_PASSWORD_RE.fullmatch(typed) or (password and password in identifier.lower()):
        return True
    # An App Password glued to the handle (an older one, so not equal to the secret): real App
    # Passwords are random, so nearly all have a digit; four plain words between hyphens
    # (game-devs-team-blog) are a handle. A rare real handle like team-2026-coop-game is treated
    # as a password: it then gets the "mixed up" advice instead of a lookup, which leaks nothing.
    if any(any(ch.isdigit() for ch in run) for run in _APP_PASSWORD_RUNS.findall(identifier.lower())):
        return True
    if ADDED_BSKY_SOCIAL not in fixes and _is_handle(identifier):
        return False  # a full handle; a password with dots is just a wrong password
    handle, password_fixes = _clean_handle(password)  # pure: nothing is logged
    return ADDED_BSKY_SOCIAL not in password_fixes and _is_handle(handle)


def _normalize_handle(handle: str) -> str:
    return _clean_handle(handle)[0]


def _looks_like_app_password(password: str) -> bool:
    """App Passwords are four groups of four lowercase letters/digits (``xxxx-xxxx-xxxx-xxxx``)."""
    return bool(_APP_PASSWORD_RE.fullmatch(password))


class SessionBox:
    """Encrypts the session for the (possibly public) state branch.

    ``key = urlsafe_b64(PBKDF2-HMAC-SHA256(app_password, "gembot-bluesky-v1:" + handle, 200 000
    iterations, 32 bytes))`` used as a Fernet key, where ``handle`` is the normalized handle
    (see :func:`_clean_handle`), lowercased. Without the app password the stored blob is
    useless; a new app password (or handle) simply fails to decrypt it, which makes the
    collector log in again. Because the fingerprint comes from the *normalized* handle, a
    change in how handles are cleaned up also counts as new credentials (one fresh login).
    """

    def __init__(self, handle: str, app_password: str, *, iterations: int = KDF_ITERATIONS):
        salt = KDF_SALT_PREFIX + _normalize_handle(handle).lower().encode()
        raw = hashlib.pbkdf2_hmac("sha256", app_password.encode(), salt, iterations, dklen=32)
        key = base64.urlsafe_b64encode(raw)
        self._fernet = Fernet(key)
        # Identifies "the same credentials" in state (e.g. to stop retrying a rejected password).
        # Checking a guess against it costs the same PBKDF2 work as trying to decrypt the session.
        self.fingerprint = hashlib.sha256(b"gembot-bluesky-fp:" + key).hexdigest()[:16]

    def seal(self, session: BlueskySession) -> str:
        plain = json.dumps(session.to_json(), separators=(",", ":")).encode()
        return self._fernet.encrypt(plain).decode("ascii")

    def open(self, token: Any) -> BlueskySession | None:
        """Decrypt a stored session; ``None`` if missing, tampered or sealed with other credentials."""
        if not isinstance(token, str) or not token:
            return None
        try:
            data = json.loads(self._fernet.decrypt(token.encode("ascii")))
            return BlueskySession.from_json(data)
        except (InvalidToken, ValueError, TypeError, KeyError, UnicodeError):
            return None


# --------------------------------------------------------------------------------------
# Parsing (pure functions)
# --------------------------------------------------------------------------------------


def _iso(value: datetime) -> str:
    return ensure_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return ensure_utc(datetime.fromisoformat(value))
    except (ValueError, OverflowError):  # e.g. "9999-12-31T23:59:59-12:00" is past datetime.max in UTC
        return None


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def parse_at_uri(uri: Any) -> tuple[str, str] | None:
    """``at://<did>/app.bsky.feed.post/<rkey>`` -> ``(did, rkey)``."""
    if not isinstance(uri, str) or not uri.startswith("at://"):
        return None
    parts = uri[len("at://") :].split("/")
    if len(parts) != 3 or parts[1] != POST_COLLECTION or not parts[0] or not parts[2]:
        return None
    return parts[0], parts[2]


def post_url(did: str, rkey: str) -> str:
    """Web URL by DID, so it survives handle changes."""
    return f"https://bsky.app/profile/{did}/post/{rkey}"


def pds_from_did_doc(doc: Any) -> str | None:
    for service in _list(_dict(doc).get("service")):
        service = _dict(service)
        if str(service.get("id", "")).endswith("#atproto_pds"):
            endpoint = service.get("serviceEndpoint")
            if isinstance(endpoint, str) and endpoint.startswith("https://"):
                return endpoint.rstrip("/")
    return None


def _label_values(*groups: Any) -> set[str]:
    values: set[str] = set()
    for group in groups:
        for label in _list(group):
            label = _dict(label)
            if label.get("val") and not label.get("neg"):
                values.add(str(label["val"]).lower())
    return values


_URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
_URL_TRAILING = ".,;:!?)]}'\"…"


def _text_urls(text: str) -> list[str]:
    urls = []
    for match in _URL_RE.finditer(text):
        raw = match.group(0)
        core = raw.rstrip(")]}'\",;:!?")
        if core.endswith(("...", "…")):  # a shortened display link; the link facet has the real URL
            continue
        url = raw.rstrip(_URL_TRAILING)
        if len(url) > len("https://"):
            urls.append(url)
    return urls


def _external_uri(embed: Any) -> str | None:
    embed = _dict(embed)
    external = _dict(embed.get("external"))
    if external:
        uri = external.get("uri")
        return uri if isinstance(uri, str) else None
    if embed.get("media"):  # recordWithMedia (record and view)
        return _external_uri(embed.get("media"))
    return None


def _facet_features(record: dict[str, Any], kind: str) -> Iterator[dict[str, Any]]:
    for facet in _list(record.get("facets")):
        for feature in _list(_dict(facet).get("features")):
            feature = _dict(feature)
            if feature.get("$type") == kind:
                yield feature


def _dedupe(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def _links(record: dict[str, Any], view: Any) -> list[str]:
    links = [str(f.get("uri") or "") for f in _facet_features(record, LINK_FACET)]
    links += [_external_uri(record.get("embed")) or "", _external_uri(view) or ""]
    links += _text_urls(str(record.get("text") or ""))
    return _dedupe(link for link in links if link.lower().startswith(("http://", "https://")))


def _tags(record: dict[str, Any]) -> list[str]:
    tags = [str(f.get("tag") or "") for f in _facet_features(record, TAG_FACET)]
    tags += [t for t in _list(record.get("tags")) if isinstance(t, str)]
    return _dedupe(t.strip().lstrip("#").lower() for t in tags)


def _thumb(view: Any) -> str | None:
    view = _dict(view)
    kind = str(view.get("$type", ""))
    thumb: Any = None
    if kind == "app.bsky.embed.external#view":
        thumb = _dict(view.get("external")).get("thumb")
    elif kind == "app.bsky.embed.images#view":
        images = _list(view.get("images"))
        thumb = _dict(images[0]).get("thumb") if images else None
    elif kind == "app.bsky.embed.video#view":
        thumb = view.get("thumbnail")
    elif kind == "app.bsky.embed.gallery#view":
        items = _list(view.get("items"))
        thumb = _dict(items[0]).get("thumbnail") if items else None
    elif kind == "app.bsky.embed.recordWithMedia#view":
        return _thumb(view.get("media"))
    return thumb if isinstance(thumb, str) and thumb.startswith("https://") else None


def _title(text: str) -> str:
    """First non-empty line, at most ``TITLE_MAX`` characters (cut at a word when possible)."""
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if len(line) <= TITLE_MAX:
        return line
    cut = line[:TITLE_MAX]
    space = cut.rfind(" ")
    return (cut[:space] if space >= TITLE_MAX // 2 else cut).rstrip()


def _author_name(author: dict[str, Any], did: str) -> str:
    """The handle, or the DID when the handle is missing or unverified (``handle.invalid``)."""
    handle = author.get("handle")
    return handle if isinstance(handle, str) and handle and handle != "handle.invalid" else did


def parse_post(post: Any, *, now: datetime, max_age_hours: float, term: str | None = None) -> Mention | None:
    """``app.bsky.feed.defs#postView`` -> :class:`Mention`, or ``None`` when the post is skipped.

    Skipped: malformed posts, opted-out / adult / moderated posts (see ``SKIP_LABELS``) and
    posts older than ``max_age_hours``. ``created_at`` is Bluesky's own sort time,
    ``min(record.createdAt, indexedAt)``, so a future-dated ``createdAt`` cannot fake freshness.
    """
    post = _dict(post)
    parsed = parse_at_uri(post.get("uri"))
    if parsed is None:
        return None
    did, rkey = parsed
    author = _dict(post.get("author"))
    if _label_values(author.get("labels"), post.get("labels")) & SKIP_LABELS:
        return None
    record = _dict(post.get("record"))
    times = [t for t in (_parse_dt(record.get("createdAt")), _parse_dt(post.get("indexedAt"))) if t]
    if not times:
        return None
    created_at = min(times)
    if now - created_at > timedelta(hours=max_age_hours):
        return None
    text = str(record.get("text") or "")
    view = post.get("embed")
    return Mention(
        source="bluesky",
        source_id=f"{did}/{rkey}",
        url=post_url(did, rkey),
        title=_title(text),
        text=text,
        author=_author_name(author, did),
        author_audience=None,
        created_at=created_at,
        engagement=Engagement(
            likes=_count(post.get("likeCount")),
            comments=_count(post.get("replyCount")),
            shares=_count(post.get("repostCount")) + _count(post.get("quoteCount")),
        ),
        links=_links(record, view),
        media_thumb=_thumb(view),
        raw_tags=_tags(record),
        channel="bluesky",
        extra={"uri": post["uri"], "cid": post.get("cid"), "did": did, "term": term},
    )


def _reply_to_comment(item: Any) -> Comment | None:
    item = _dict(item)
    kind = item.get("$type")
    if kind and kind != THREAD_VIEW:  # notFoundPost / blockedPost
        return None
    post = _dict(item.get("post"))
    parsed = parse_at_uri(post.get("uri"))
    if parsed is None:
        return None
    author = _dict(post.get("author"))
    if _label_values(author.get("labels"), post.get("labels")) & SKIP_LABELS:
        return None
    record = _dict(post.get("record"))
    return Comment(
        id=parsed[1],
        author=_author_name(author, parsed[0]),
        text=str(record.get("text") or ""),
        score=_count(post.get("likeCount")),
        created_at=_parse_dt(record.get("createdAt")),
    )


def _followers(profile: Any) -> int | None:
    value = _dict(profile).get("followersCount")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _mention_did(mention: Mention) -> str | None:
    did = mention.extra.get("did") or mention.source_id.split("/", 1)[0]
    return did if isinstance(did, str) and did.startswith("did:") else None


def _mention_uri(mention: Mention) -> str | None:
    uri = mention.extra.get("uri")
    if isinstance(uri, str) and parse_at_uri(uri):
        return uri
    did, _, rkey = mention.source_id.partition("/")
    return f"at://{did}/{POST_COLLECTION}/{rkey}" if did.startswith("did:") and rkey else None


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


# --------------------------------------------------------------------------------------
# Collector
# --------------------------------------------------------------------------------------


class BlueskyCollector(Collector):
    """``searchPosts`` for every term in ``sources.yaml``; replies and follower counts on demand.

    Scratch (``meta.collector_state["bluesky"]``, may be public):
    ``session`` (Fernet token, see :class:`SessionBox`), ``created`` (createSession attempts in
    the last 24h), ``login_error`` (last failed login: time, message, credential fingerprint;
    after a 401 also ``rejected`` and ``handle_check``, the ``HANDLE_*`` answer of the handle
    check, kept for as long as the fingerprint stays the same), ``route`` (``pds`` or
    ``appview``: the search route that worked last), ``public_ok`` / ``public_probe_at``
    (logged-out search probe).
    """

    name = "bluesky"

    def __init__(self, ctx: CollectContext, budget: Budget | None = None):
        super().__init__(ctx, budget)
        self.bsky = self.config.sources.bluesky
        self._session: BlueskySession | None = None
        self._box: SessionBox | None = None
        self._handle: str | None = None
        self._handle_fixes: list[str] = []
        self._seen_uris: set[str] = set()

    # ---- hooks -------------------------------------------------------
    def enabled(self) -> tuple[bool, str | None]:
        if not self.bsky.enabled:
            return False, "disabled in sources.yaml"
        if not self._terms():
            return False, "no Bluesky search terms in sources.yaml"
        return True, None

    def collect(self) -> list[Mention]:
        terms = self._terms()
        if self.config.secrets.has_bluesky_login:
            self._collect_logged_in(terms)
        else:
            self._collect_logged_out(terms)
        return self.found

    def fetch_comments(self, mention: Mention, limit: int) -> list[Comment]:
        """Direct replies (logged out, no token), most liked first."""
        uri = _mention_uri(mention)
        if not self.bsky.enabled or uri is None or limit <= 0:
            return []
        url = self._public() + GET_POST_THREAD
        reply = self._xrpc("GET", url, params={"uri": uri, "depth": 1, "parentHeight": 0}, expect=(200, 400))
        if reply.status != 200:
            if reply.error == "NotFound":  # deleted post
                return []
            raise XrpcError(f"getPostThread: {reply.describe()}", reply.status, url, reply.error)
        thread = _dict(reply.data.get("thread"))
        comments: list[Comment] = []
        for item in _list(thread.get("replies")):
            try:
                comment = _reply_to_comment(item)
            except (ValueError, OverflowError, TypeError) as exc:  # one odd reply must not lose the thread
                self.log.debug("skipping malformed reply: %s", exc)
                continue
            if comment is not None:
                comments.append(comment)
        comments.sort(key=lambda c: c.score, reverse=True)
        return comments[:limit]

    def fetch_audience(self, mention: Mention) -> int | None:
        """The author's follower count (logged out, no token)."""
        did = _mention_did(mention)
        if not self.bsky.enabled or did is None:
            return None
        reply = self._xrpc("GET", self._public() + GET_PROFILE, params={"actor": did}, expect=(200,))
        return _followers(reply.data)

    def fetch_audiences(self, mentions: Iterable[Mention]) -> int:
        """Fill ``author_audience`` for many Bluesky mentions, 25 authors per ``getProfiles`` request.

        Returns how many mentions got a follower count. Never raises: failures become warnings.
        """
        if not self.bsky.enabled:
            return 0
        by_did: dict[str, list[Mention]] = {}
        for mention in mentions:
            did = _mention_did(mention) if mention.source == "bluesky" else None
            if did:
                by_did.setdefault(did, []).append(mention)
        dids = list(by_did)
        filled = 0
        for start in range(0, len(dids), PROFILES_BATCH):
            batch = dids[start : start + PROFILES_BATCH]
            try:
                reply = self._xrpc(
                    "GET", self._public() + GET_PROFILES, params={"actors": batch}, expect=(200,)
                )
            except BudgetExceeded as exc:
                self.report.warnings.append(f"audiences: {exc}")
                break
            except Exception as exc:
                self.report.warnings.append(f"audiences: {exc}")
                continue
            for profile in _list(reply.data.get("profiles")):
                followers = _followers(profile)
                if followers is None:
                    continue
                for mention in by_did.get(str(_dict(profile).get("did")), []):
                    mention.author_audience = followers
                    filled += 1
        self.report.requests = self.budget.used
        return filled

    # ---- logged out ----------------------------------------------------
    def _collect_logged_out(self, terms: list[str]) -> None:
        scratch = self.scratch()
        for key in ("session", "route", "login_error"):  # credentials were removed: forget them
            scratch.pop(key, None)
        last_probe = _parse_dt(scratch.get("public_probe_at"))
        window = timedelta(hours=self.bsky.unauth_probe_hours)
        if not scratch.get("public_ok") and last_probe and self.now - last_probe < window:
            self._skip_public(quiet=True)
            return
        first, rest = terms[0], terms[1:]
        page: dict[str, Any] | None = None
        try:  # the first term doubles as the probe
            page = self._public_search(self._params(first))
        except SearchRefused as exc:
            scratch.update(public_ok=False, public_probe_at=_iso(self.now))
            self.log.warning("public search refused (%s)", exc)
            self._skip_public(quiet=False)
            return
        except XrpcError as exc:  # a JSON error from the AppView itself: search is reachable
            self._fail(f"search {first!r}: {exc}")
        except HttpError as exc:  # 5xx / network / 429 / bad JSON: no verdict, probe again next run
            self._fail(f"search {first!r}: {exc}")
            return
        scratch.update(public_ok=True, public_probe_at=_iso(self.now))
        if page is not None:
            with self.guard(f"search {first!r}"):
                self._search_term(first, self._public_search, first_page=page)
        stopped = self._run_terms(rest, self._public_search)
        if isinstance(stopped, SearchRefused):
            scratch.update(public_ok=False, public_probe_at=_iso(self.now))

    def _public_search(self, params: dict[str, Any]) -> dict[str, Any]:
        url = self._public() + SEARCH_POSTS
        reply = self._xrpc("GET", url, params=params)  # never with a token
        if reply.status == 200:
            return reply.data
        if reply.status in (401, 403):
            raise SearchRefused(f"public search refused: {reply.describe()}", reply.status, url)
        raise XrpcError(f"search failed: {reply.describe()}", reply.status, url, reply.error)

    def _skip_public(self, *, quiet: bool) -> None:
        self.report.skipped = True
        self.report.skip_reason = PUBLIC_BLOCKED_REASON
        if quiet:
            self.log.info("skipped: %s", PUBLIC_BLOCKED_REASON)
        else:
            self.report.warnings.append(PUBLIC_BLOCKED_REASON)
            self.log.warning("%s", PUBLIC_BLOCKED_REASON)

    # ---- logged in -----------------------------------------------------
    def _collect_logged_in(self, terms: list[str]) -> None:
        self._session = self._login_for_run()
        if self._session is not None:
            self._run_terms(terms, self._auth_search)

    def _login_for_run(self) -> BlueskySession | None:
        try:
            return self._current_session()
        except LoginLimitReached as exc:
            self.report.warnings.append(str(exc))
            self.log.warning("%s", exc)
            last = _dict(self.scratch().get("login_error"))
            if last.get("message") and last.get("fp") == self._session_box().fingerprint:
                self._fail(f"login: {last['message']}")
            elif last.get("message"):  # the last error was about other secrets: these were never tried
                self._fail(f"login: {_untried_message(exc.retry_at)}")
        except (LoginError, HttpError) as exc:
            self._fail(f"login: {exc}")
        return None

    def _current_session(self) -> BlueskySession:
        """Stored access token if still valid, else refresh, else a new login (capped)."""
        scratch = self.scratch()
        stored = self._session_box().open(scratch.get("session"))
        if stored is None and scratch.get("session"):
            self.log.info("stored Bluesky session cannot be decrypted (new password or handle?); logging in")
            scratch.pop("session", None)
        if stored is not None and stored.access_valid(self.now):
            return stored
        if stored is not None:
            renewed = self._refresh(stored)
            if renewed is not None:
                return renewed
        return self._create_session()

    def _refresh(self, session: BlueskySession) -> BlueskySession | None:
        """Rotate the tokens. ``None`` if the refresh token is dead; raises on transient errors."""
        url = self._entryway() + REFRESH_SESSION
        reply = self._xrpc("POST", url, headers=_bearer(session.refresh_jwt), expect=(200, 400, 401))
        if reply.status != 200:
            self.log.info("Bluesky refresh token not accepted (%s); logging in again", reply.describe())
            self.scratch().pop("session", None)
            return None
        renewed = BlueskySession.from_response(
            reply.data, now=self.now, default_pds=self._entryway(), previous=session
        )
        self._store(renewed)  # the old refresh token is now spent: persist the new one right away
        return renewed

    def _create_session(self) -> BlueskySession:
        scratch = self.scratch()
        box = self._session_box()
        now = self.now
        last = _dict(scratch.get("login_error"))
        last_at = _parse_dt(last.get("at"))
        if (
            last.get("fatal")
            and last.get("fp") == box.fingerprint
            and last_at
            and now - last_at < LOGIN_WINDOW
        ):
            # The same credentials were rejected recently: don't spend bsky.social's daily login limit.
            if _was_rejected(last):  # checks the handle if not done yet (e.g. stored before checks existed)
                self._explain_rejection(last)
            raise LoginError(
                f"{last.get('message')} (next try {_iso(last_at + LOGIN_WINDOW)} or when the secret changes)"
            )
        recent = [s for s in _list(scratch.get("created")) if (t := _parse_dt(s)) and now - t < LOGIN_WINDOW]
        scratch["created"] = recent
        cap = self.bsky.max_sessions_per_day
        if len(recent) >= cap:
            times = sorted(t for s in recent if (t := _parse_dt(s)))
            # a login is allowed again once all but cap - 1 of them have left the window
            retry_at = times[len(times) - cap] + LOGIN_WINDOW if cap > 0 else None
            raise LoginLimitReached(LOGIN_LIMIT_WARNING, retry_at)
        if self.budget.exhausted:
            raise BudgetExceeded(
                f"{self.budget.name}: request budget of {self.budget.limit} used up for this run"
            )
        recent.append(_iso(now))  # every attempt counts, successful or not
        body = {"identifier": self._identifier(), "password": self.config.secrets.bluesky_app_password}

        def remember(message: str, *, fatal: bool) -> dict[str, Any]:
            entry = {"at": _iso(now), "message": message, "fp": box.fingerprint, "fatal": fatal}
            if last.get("fp") == box.fingerprint and last.get("handle_check"):
                # already checked for these credentials: keep it through a 503 or timeout in between
                entry["handle_check"] = last["handle_check"]
            scratch["login_error"] = entry
            return entry

        try:
            # createSession counts against a tiny daily limit: never let the HTTP layer retry it.
            reply = self._xrpc(
                "POST", self._entryway() + CREATE_SESSION, json_body=body, expect=(200, 400, 401), retries=0
            )
        except HttpError as exc:  # 429 / 5xx / network: worth retrying in a later run
            remember(f"createSession failed: {exc}", fatal=False)
            raise
        if reply.status != 200 and _rejected(reply):
            entry = remember("", fatal=True)  # the message comes from _explain_rejection
            if entry.get("handle_check") == HANDLE_UNKNOWN:
                del entry["handle_check"]  # the last check failed: try once more with this login
            self._explain_rejection(entry)
            raise LoginError(
                f"{entry['message']}. GemBot tries again on its next run after the secret changes"
            )
        if reply.status != 200:
            message = _login_error_message(reply)
            remember(message, fatal=True)
            raise LoginError(message)
        try:
            session = BlueskySession.from_response(reply.data, now=now, default_pds=self._entryway())
        except LoginError as exc:
            remember(str(exc), fatal=True)
            raise
        scratch.pop("login_error", None)
        self._store(session)
        self.log.info("logged in to Bluesky (PDS %s)", session.pds)  # never the handle: it is a secret
        return session

    def _explain_rejection(self, entry: dict[str, Any]) -> None:
        """Write the message for a 401 into ``entry`` (the ``login_error`` scratch): "wrong handle"
        or "wrong App Password" when the handle check can tell, the general advice otherwise.

        The handle is looked up at most once per set of credentials: the answer is kept in
        ``entry["handle_check"]`` next to the credential fingerprint. Only a failed lookup
        (``HANDLE_UNKNOWN``) is repeated, with the next login attempt (at most once a day).
        Answers that need no request are worked out again every time, so an answer stored by
        an older, cruder rule (a real handle taken for a password) does not stick.
        """
        check = self._handle_verdict()
        if check is None:
            kept = entry.get("handle_check")
            check = kept if kept in LOOKUP_ANSWERS else self._check_handle()
        if check is not None:  # None: no request budget left, look it up on a later run
            entry["handle_check"] = check
        entry.update(rejected=True, message=self._rejected_message(check or HANDLE_UNKNOWN))

    def _handle_verdict(self) -> str | None:
        """The handle check's answer when no request is needed; ``None``: look the handle up."""
        identifier = self._identifier()
        if not identifier or _is_email(identifier) or _is_did(identifier):
            return HANDLE_SKIPPED
        if _has_extra_text(identifier):
            return HANDLE_EXTRA_TEXT  # never put a password into a URL
        if _may_hold_password(identifier, self._handle_fixes, self.config.secrets.bluesky_app_password or ""):
            return HANDLE_MIXED_UP  # never put a password into a URL
        if not _is_handle(identifier):
            return HANDLE_MISSING  # not even handle syntax: no account can have it
        return None

    def _check_handle(self) -> str | None:
        """Does BLUESKY_HANDLE exist? One request to the public AppView: no token, no retry, and
        not a login attempt. Only for a handle :meth:`_handle_verdict` has no answer for.
        ``None`` when the run's request budget is used up."""
        identifier = self._identifier()
        try:
            reply = self._xrpc(
                "GET",
                self._public() + RESOLVE_HANDLE,
                params={"handle": identifier},
                expect=(200, 400),
                retries=0,
            )
        except BudgetExceeded:
            return None
        except HttpError as exc:  # 5xx / network / 429: no verdict, keep the general advice
            # the status only: request errors can quote the URL, and the URL holds the handle
            self.log.info("could not check BLUESKY_HANDLE on Bluesky (%s)", _status(exc))
            return HANDLE_UNKNOWN
        data = _dict(reply.data)
        if reply.status == 200 and str(data.get("did") or "").startswith("did:"):
            return HANDLE_FOUND
        if reply.status == 400 and (
            reply.error == "InvalidRequest" or "unable to resolve handle" in str(data.get("message")).lower()
        ):
            return HANDLE_MISSING
        self.log.info("could not check BLUESKY_HANDLE on Bluesky (unexpected HTTP %s answer)", reply.status)
        return HANDLE_UNKNOWN

    def _rejected_message(self, check: str) -> str:
        password = self.config.secrets.bluesky_app_password or ""
        self._identifier()  # sets _handle_fixes
        return _rejected_message(
            check,
            looks_right=_looks_like_app_password(password),
            guessed=ADDED_BSKY_SOCIAL in self._handle_fixes,
        )

    def _identifier(self) -> str:
        """BLUESKY_HANDLE as sent to Bluesky, with common paste mistakes fixed (see :func:`_clean_handle`)."""
        if self._handle is None:
            self._handle, self._handle_fixes = _clean_handle(self.config.secrets.bluesky_handle or "")
            if self._handle_fixes:  # say what changed, never the handle itself
                self.log.info("BLUESKY_HANDLE fixed for this run: %s", ", ".join(self._handle_fixes))
        return self._handle

    def _renew(self) -> BlueskySession:
        renewed = self._refresh(self._session) if self._session is not None else None
        return renewed or self._create_session()

    def _auth_search(self, params: dict[str, Any]) -> dict[str, Any]:
        """Search on the remembered route; renew an expired token once, switch routes once."""
        scratch = self.scratch()
        route = scratch.get("route") if scratch.get("route") in ROUTES else "pds"
        renewed = switched = False
        while True:
            url, headers = self._route_target(route)
            reply = self._xrpc("GET", url, params=params, headers=headers)
            if reply.status == 200:
                if scratch.get("route", "pds") != route:
                    scratch["route"] = route
                    self.log.info("Bluesky search now uses the %s route", route)
                return reply.data
            if reply.status in (400, 401) and reply.error in TOKEN_ERRORS and not renewed:
                renewed = True
                try:
                    self._session = self._renew()
                except (LoginError, HttpError) as exc:
                    raise SearchRefused(f"session expired and could not be renewed: {exc}", 401, url) from exc
                continue
            if reply.status in (401, 403) and not switched:
                switched = True
                route = "appview" if route == "pds" else "pds"
                continue
            if reply.status in (401, 403):
                if reply.status == 401:
                    self._expire_access()  # make the next run start with a refresh
                raise SearchRefused(f"search refused on both routes: {reply.describe()}", reply.status, url)
            raise XrpcError(f"search failed: {reply.describe()}", reply.status, url, reply.error)

    def _route_target(self, route: str) -> tuple[str, dict[str, str]]:
        assert self._session is not None
        headers = _bearer(self._session.access_jwt)
        if route == "pds":
            base = self._session.pds or self._entryway()
            headers["atproto-proxy"] = self.bsky.appview_proxy
        else:
            base = self.bsky.appview.rstrip("/")
        if _host(base) == _host(self._public()):
            raise ValueError("refusing to send a Bluesky token to the public AppView; check sources.yaml")
        return base + SEARCH_POSTS, headers

    def _expire_access(self) -> None:
        if self._session is not None:
            self._session.access_exp = self.now
            self._store(self._session)

    def _store(self, session: BlueskySession) -> None:
        self.scratch()["session"] = self._session_box().seal(session)

    def _session_box(self) -> SessionBox:
        if self._box is None:
            secrets = self.config.secrets
            self._box = SessionBox(secrets.bluesky_handle or "", secrets.bluesky_app_password or "")
        return self._box

    # ---- search (both modes) --------------------------------------------
    def _run_terms(
        self, terms: list[str], search: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> HttpError | None:
        """One unit of work per term. Stops (and returns the reason) when Bluesky refuses or rate limits."""
        for term in terms:
            stop: HttpError | None = None
            with self.guard(f"search {term!r}"):
                try:
                    self._search_term(term, search)
                except (SearchRefused, RateLimited) as exc:
                    stop = exc
                    raise
            if stop is not None:
                self.log.warning("stopping Bluesky search for this run: %s", stop)
                return stop
        return None

    def _search_term(
        self,
        term: str,
        search: Callable[[dict[str, Any]], dict[str, Any]],
        first_page: dict[str, Any] | None = None,
    ) -> None:
        params = self._params(term)
        page = first_page
        for _ in range(max(self.bsky.search_pages, 1)):
            if page is None:
                page = search(params)
            posts = page.get("posts")
            if not isinstance(posts, list):
                raise ValueError("searchPosts response has no 'posts' list")
            for post in posts:
                self._take(post, term)
            cursor = page.get("cursor")
            if not posts or not isinstance(cursor, str) or not cursor:
                break
            params = {**params, "cursor": cursor}
            page = None

    def _take(self, post: Any, term: str) -> None:
        try:
            mention = parse_post(post, now=self.now, max_age_hours=self.bsky.max_post_age_hours, term=term)
        except (ValueError, OverflowError, TypeError) as exc:  # an odd post: skip just that post
            self.log.debug("skipping malformed post: %s", exc)
            return
        if mention is None or mention.extra["uri"] in self._seen_uris:
            return
        self._seen_uris.add(mention.extra["uri"])
        self.found.append(mention)

    def _params(self, term: str) -> dict[str, Any]:
        params: dict[str, Any] = {
            "q": term,
            "sort": self.bsky.sort,
            "limit": min(max(self.bsky.limit, 1), 100),
            "since": _iso(self.now - timedelta(hours=self.bsky.max_post_age_hours)),
        }
        if self.bsky.lang:
            params["lang"] = self.bsky.lang
        return params

    # ---- plumbing --------------------------------------------------------
    def _terms(self) -> list[str]:
        return [term.strip() for term in self.bsky.terms if term and term.strip()]

    def _entryway(self) -> str:
        return self.bsky.pds_host.rstrip("/")

    def _public(self) -> str:
        return self.bsky.public_appview.rstrip("/")

    def _fail(self, message: str) -> None:
        self.report.errors.append(message)
        self.report.failed_units += 1
        self.log.warning("%s", message)

    def _xrpc(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        expect: tuple[int, ...] = (200, 400, 401, 403),
        retries: int | None = None,
    ) -> _Reply:
        """One XRPC request. Statuses in ``expect`` come back as a :class:`_Reply` to classify;
        429/5xx/transport errors are retried and raised by the HTTP layer."""
        response = self.http.request(
            method,
            url,
            budget=self.budget,
            params=params,
            headers=headers,
            json=json_body,
            expect=expect,
            retries=retries,
        )
        html = "text/html" in response.headers.get("content-type", "").lower()
        data: Any = None
        if not html:
            try:
                data = response.json()
            except ValueError:
                data = None
        if response.status_code == 200 and not isinstance(data, dict):
            raise HttpError(f"{self.budget.name}: invalid JSON from {urlsplit(url).path}", 200, url)
        return _Reply(response.status_code, data, html, url)


def _status(exc: HttpError) -> str:
    return f"HTTP {exc.status}" if exc.status else type(exc.__cause__ or exc).__name__


def _rejected(reply: _Reply) -> bool:
    """A wrong handle or a wrong password: Bluesky answers both with the same 401."""
    if reply.error in ("AuthFactorTokenRequired", "AccountTakedown"):
        return False
    return reply.status == 401 or reply.error == "AuthenticationRequired"


def _was_rejected(entry: dict[str, Any]) -> bool:
    return entry.get("rejected") is True or str(entry.get("message") or "").startswith(_REJECTED_PREFIX)


def _rejected_message(check: str, *, looks_right: bool, guessed: bool = False) -> str:
    """What to fix after a 401, as exactly as the handle check allows. Never contains a secret
    value (nor the password's length or any part of it). ``looks_right``: the App Password has
    the ``xxxx-xxxx-xxxx-xxxx`` shape. ``guessed``: GemBot added ``.bsky.social`` to the handle.

    An existing handle does not prove the App Password is wrong: the handle may be someone
    else's (a display name turned into ``name.bsky.social``, a typo, another profile's link)."""
    odd = "" if looks_right else f" (the current one doesn't look like an App Password, {APP_PASSWORD_SHAPE})"
    if check == HANDLE_MIXED_UP:  # says what both secrets look like, so no extra hint
        return (
            "BLUESKY_HANDLE and BLUESKY_APP_PASSWORD look mixed up: BLUESKY_HANDLE is the full handle, "
            f"like yourname.bsky.social; BLUESKY_APP_PASSWORD is an App Password, like {APP_PASSWORD_SHAPE} "
            f"({APP_PASSWORDS})"
        )
    if check == HANDLE_FOUND and guessed:
        return (
            "GemBot added .bsky.social to BLUESKY_HANDLE; that account exists. If it isn't the bot's, set "
            f"the full handle; if it is, make a new App Password ({APP_PASSWORDS}){odd}"
        )
    if check == HANDLE_FOUND:
        odd = "" if looks_right else f" (it doesn't look like an App Password, {APP_PASSWORD_SHAPE})"
        return (
            f"BLUESKY_HANDLE exists, so BLUESKY_APP_PASSWORD is probably wrong{odd}: make a new App Password "
            f"({APP_PASSWORDS}), or if that account isn't the bot's, fix BLUESKY_HANDLE"
        )
    either = (
        ""
        if looks_right
        else f". BLUESKY_APP_PASSWORD doesn't look like an App Password either ({APP_PASSWORD_SHAPE}; "
        f"{APP_PASSWORDS})"
    )
    if check == HANDLE_EXTRA_TEXT:
        return f"BLUESKY_HANDLE has extra text after .bsky.social - it should be only the handle{either}"
    if check == HANDLE_MISSING:
        return (
            "BLUESKY_HANDLE is not a Bluesky account: use the full handle, like yourname.bsky.social "
            f"(no @, no link, not the display name){either}"
        )
    return (
        f"{_REJECTED_PREFIX} — create a new App Password (Bluesky {APP_PASSWORDS}), update the secret, "
        f"and check BLUESKY_HANDLE{odd}"
    )


def _untried_message(retry_at: datetime | None) -> str:
    """New secrets met the daily login cap before their first login: nothing is known about them yet."""
    if retry_at is None:
        return (
            "bluesky.max_sessions_per_day in sources.yaml allows no logins: raise it to try the new secrets"
        )
    return f"daily login limit reached; the new secrets will be tried on the first run after {_iso(retry_at)}"


def _login_error_message(reply: _Reply) -> str:
    """Login failures other than a plain 401 (see :func:`_rejected_message` for those)."""
    error = reply.error
    if error == "AuthFactorTokenRequired":
        return (
            "Bluesky asked for an email sign-in code: use an App Password, not your main password "
            f"(Bluesky {APP_PASSWORDS})"
        )
    if error == "AccountTakedown":
        return "Bluesky account has been taken down (AccountTakedown)"
    return f"createSession failed: {reply.describe()}"
