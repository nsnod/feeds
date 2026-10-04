"""A minimal Discord REST (API v10) client for a bot token.

GemBot never keeps a gateway connection open: it runs on GitHub Actions cron, so every
action (create channels, post, react, read reactions) is a plain HTTP call. All requests
go through :class:`gembot.http.HttpClient`, which charges a :class:`Budget` per attempt
and already sleeps on 429 using ``Retry-After`` / the JSON ``retry_after``.

On top of that this client:

* sends ``Authorization: Bot <token>`` and Discord's required ``DiscordBot (url, version)``
  User-Agent (the token is never logged or put in an error message);
* honours Discord's rate-limit headers: when a bucket reports ``X-RateLimit-Remaining: 0``
  it waits ``X-RateLimit-Reset-After`` seconds (bounded) before the next call;
* spaces reaction PUTs at least 0.3 s apart (Discord allows about one per 0.25 s);
* turns Discord's JSON errors into :class:`DiscordError` with the error ``code`` and a
  plain-English hint (missing permissions, unknown channel, bad token, ...);
* treats a 403/429 whose body is *not* JSON (``error code: 1010`` / ``1015``), or a 429 longer
  than GemBot waits, as Discord/Cloudflare blocking the runner: raises at once
  (``DiscordError.blocked``) and makes no further Discord requests for the rest of the run;
* defaults ``allowed_mentions`` to ``{"parse": []}`` so a message can never ping
  @everyone/@here or random users/roles by accident;
* warns when Discord accepted a message but dropped its embeds (missing Embed Links).

No gateway connection is needed for any of this: Discord removed the old "connect to the
gateway once before sending messages" rule from its docs in 2021. The Setup workflow still
does one short connect as optional insurance (see ``setup.connect_gateway_once``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote

import httpx

from gembot.discord.embeds import enforce_limits
from gembot.http import Budget, HttpClient, HttpError, RateLimited

log = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/nsnod/feeds, 0.1)"

CHANNEL_TEXT = 0
CHANNEL_CATEGORY = 4
CHANNEL_ANNOUNCEMENT = 5

REACTION_DELAY_S = 0.3
REACTION_PAGE = 100  # GET .../reactions/{emoji} defaults to 25 users; 100 is the maximum

# Every 2xx/4xx status is handed back to us so we can read Discord's JSON error body;
# 429 is still retried inside HttpClient (it is checked before ``expect``).
_PASS_THROUGH = tuple(code for code in range(200, 500) if code != 429)

GATEWAY_HINT = (
    "Check the DISCORD_BOT_TOKEN secret first (it may be wrong or was reset). Running the "
    "'Setup GemBot' workflow also connects the bot to the Discord gateway once, which is "
    "optional insurance and harmless."
)
BLOCKED_HINT = (
    "Discord/Cloudflare temporarily blocked this runner's IP; GemBot stops calling Discord for "
    "this run and the next run will retry."
)
PERMISSIONS = (
    "View Channels, Send Messages, Embed Links, Add Reactions, Read Message History and Manage Channels"
)

ERROR_HINTS: dict[int, str] = {
    0: "Discord rejected the bot token. Copy a fresh token (Developer Portal -> your app -> Bot -> "
    "Reset Token) into the DISCORD_BOT_TOKEN secret.",
    10003: "Unknown Channel: the channel was deleted. Run the 'Setup GemBot' workflow to recreate it.",
    10004: "Unknown Guild: check DISCORD_GUILD_ID and that the bot is still in the server.",
    10008: "Unknown Message: the message was deleted.",
    10014: "Unknown Emoji: check feedback.up_emoji / down_emoji in settings.yaml.",
    30010: "This message already has the maximum number of different reactions.",
    30013: "The server has reached Discord's maximum of 500 channels.",
    40001: "Discord says 'Unauthorized'. " + GATEWAY_HINT,
    50001: "Missing Access: the bot cannot see this channel. Give the bot's role View Channels and "
    "Read Message History on the GemBot category (or re-invite it with the link from the README).",
    50013: f"Missing Permissions: the bot's role needs {PERMISSIONS}. Fix it in Server Settings -> "
    "Roles, or re-invite the bot with the link from the README.",
    50035: "Discord rejected the message format (this is a GemBot bug, please report it).",
    90001: "Reaction blocked: someone blocked the bot or reactions are restricted in this channel.",
}


class DiscordError(HttpError):
    """Discord answered with an error. ``code`` is Discord's JSON error code (e.g. 50013).

    ``blocked`` is True when Discord/Cloudflare blocked the runner (non-JSON 403/429, or a 429
    longer than GemBot waits): stop calling Discord for this run, the next run retries.
    """

    def __init__(
        self,
        message: str,
        status: int | None = None,
        url: str | None = None,
        *,
        code: int | None = None,
        discord_message: str | None = None,
        hint: str | None = None,
        blocked: bool = False,
    ):
        super().__init__(message, status, url)
        self.code = code
        self.discord_message = discord_message
        self.hint = hint
        self.blocked = blocked


@runtime_checkable
class DiscordAPI(Protocol):
    """The Discord calls GemBot needs. Implemented by :class:`DiscordClient` and ``FakeDiscord``."""

    def me(self) -> dict: ...

    def list_guilds(self) -> list[dict]: ...

    def list_channels(self, guild_id: str) -> list[dict]: ...

    def create_channel(
        self, guild_id: str, name: str, type: int, parent_id: str | None = None, topic: str | None = None
    ) -> dict: ...

    def send_message(self, channel_id: str, payload: dict) -> dict: ...

    def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> None: ...

    def get_message(self, channel_id: str, message_id: str) -> dict: ...

    def get_reaction_users(
        self,
        channel_id: str,
        message_id: str,
        emoji: str,
        limit: int = 100,
        after: str | None = None,
        burst: bool = False,
    ) -> list[dict]:
        """One page of users (ordered by id) who reacted; ``after`` pages, ``burst`` = super reactions."""
        ...


def prepare_message(payload: dict) -> dict:
    """What actually goes over the wire: limits enforced, mentions locked down by default."""
    body = enforce_limits(payload)
    if "allowed_mentions" not in body:
        body["allowed_mentions"] = {"parse": []}
    return body


def emoji_path(emoji: str) -> str:
    """URL-encode an emoji for a reactions route (``👍`` -> ``%F0%9F%91%8D``, ``gem:1`` -> ``gem%3A1``)."""
    return quote(emoji, safe="")


def _flatten_errors(errors: Any, path: str = "") -> list[str]:
    """Discord's nested 50035 ``errors`` object -> ``["embeds.0.title: Must be 256 ..."]``."""
    out: list[str] = []
    if isinstance(errors, dict):
        for key, value in errors.items():
            if key == "_errors" and isinstance(value, list):
                for item in value:
                    message = item.get("message") if isinstance(item, dict) else str(item)
                    out.append(f"{path or 'body'}: {message}")
            else:
                out.extend(_flatten_errors(value, f"{path}.{key}" if path else str(key)))
    return out


def discord_error(response: httpx.Response, method: str, path: str) -> DiscordError:
    """Build a :class:`DiscordError` (with a human hint) from an error response."""
    status = response.status_code
    code: int | None = None
    message: str | None = None
    details: list[str] = []
    try:
        body = response.json()
    except Exception:
        body = None
        if status in (403, 429):  # Cloudflare answers in plain text: "error code: 1010" / "1015"
            snippet = " ".join(response.text.split())[:80] or "empty body"
            text = f"Discord {method} {path} failed: HTTP {status} ({snippet}). {BLOCKED_HINT}"
            return DiscordError(text, status, path, discord_message=snippet, hint=BLOCKED_HINT, blocked=True)
    if isinstance(body, dict):
        raw_code = body.get("code")
        code = raw_code if isinstance(raw_code, int) else None
        message = str(body["message"]) if body.get("message") is not None else None
        details = _flatten_errors(body.get("errors"))
    hint = ERROR_HINTS.get(code) if code is not None else None
    if hint is None and status == 401:
        hint = ERROR_HINTS[0]
    if message and "gateway" in message.lower():
        hint = GATEWAY_HINT
    text = f"Discord {method} {path} failed: HTTP {status}"
    if code is not None or message:
        text += f" (code {code}: {message})" if code is not None else f" ({message})"
    if details:
        text += " [" + "; ".join(details[:5]) + "]"
    if hint:
        text += f". {hint}"
    return DiscordError(text, status, path, code=code, discord_message=message, hint=hint)


class DiscordClient:
    """Real Discord REST client (bot token, API v10)."""

    def __init__(
        self,
        token: str,
        http: HttpClient,
        budget: Budget,
        api_base: str = DEFAULT_API_BASE,
        *,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        reaction_delay_s: float = REACTION_DELAY_S,
    ):
        if not token:
            raise ValueError("DISCORD_BOT_TOKEN is not set")
        self._token = token
        self.http = http
        self.budget = budget
        self.api_base = api_base.rstrip("/")
        self.sleep = sleep if sleep is not None else http.sleep
        self.clock = clock
        self.reaction_delay_s = reaction_delay_s
        self._last_reaction_at: float | None = None
        self.blocked: DiscordError | None = None  # set once Discord/Cloudflare blocks this run

    def __repr__(self) -> str:  # never show the token
        return f"DiscordClient(api_base={self.api_base!r}, budget={self.budget.name!r})"

    # ------------------------------------------------------------------ plumbing
    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bot {self._token}", "User-Agent": USER_AGENT}

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        expect: tuple[int, ...] = (200,),
    ) -> httpx.Response:
        if self.blocked is not None:  # never keep knocking once blocked: that extends the ban
            raise self.blocked
        try:
            response = self.http.request(
                method,
                self.api_base + path,
                budget=self.budget,
                headers=self._headers,
                json=json,
                params=params,
                expect=_PASS_THROUGH,
            )
        except RateLimited as exc:  # HttpClient already waited and retried within its bounds
            self.blocked = DiscordError(
                f"Discord {method} {path} failed: HTTP 429, rate limited for longer than GemBot waits. "
                + BLOCKED_HINT,
                429,
                path,
                hint=BLOCKED_HINT,
                blocked=True,
            )
            raise self.blocked from exc
        self._respect_bucket(response)
        if response.status_code not in expect:
            error = discord_error(response, method, path)
            if error.blocked:
                self.blocked = error
            raise error
        return response

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._request(method, path, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise DiscordError(f"Discord {method} {path}: invalid JSON", response.status_code, path) from exc

    def _respect_bucket(self, response: httpx.Response) -> None:
        """If this route's bucket is empty, wait until it resets before the next request."""
        if response.headers.get("x-ratelimit-remaining") != "0":
            return
        try:
            wait = float(response.headers.get("x-ratelimit-reset-after", "0"))
        except ValueError:
            return
        if wait > 0:
            wait = min(wait, self.http.max_backoff_s)
            log.debug(
                "discord: bucket %s empty, waiting %.2fs", response.headers.get("x-ratelimit-bucket"), wait
            )
            self.sleep(wait)

    # ------------------------------------------------------------------ API
    def me(self) -> dict:
        return self._json("GET", "/users/@me")

    def list_guilds(self) -> list[dict]:
        return self._json("GET", "/users/@me/guilds")

    def list_channels(self, guild_id: str) -> list[dict]:
        return self._json("GET", f"/guilds/{guild_id}/channels")

    def create_channel(
        self, guild_id: str, name: str, type: int, parent_id: str | None = None, topic: str | None = None
    ) -> dict:
        body: dict[str, Any] = {"name": name, "type": type}
        if parent_id:
            body["parent_id"] = parent_id
        if topic:
            body["topic"] = topic
        return self._json("POST", f"/guilds/{guild_id}/channels", json=body, expect=(200, 201))

    def send_message(self, channel_id: str, payload: dict) -> dict:
        body = prepare_message(payload)
        message = self._json("POST", f"/channels/{channel_id}/messages", json=body, expect=(200, 201))
        if body.get("embeds") and isinstance(message, dict) and not message.get("embeds"):
            log.warning(
                "discord: message %s in channel %s was posted without its embeds: the bot lacks "
                "Embed Links in this channel",
                message.get("id"),
                channel_id,
            )
        return message

    def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        if self._last_reaction_at is not None and self.reaction_delay_s > 0:
            waited = self.clock() - self._last_reaction_at
            if waited < self.reaction_delay_s:
                self.sleep(self.reaction_delay_s - waited)
        try:
            self._request(
                "PUT",
                f"/channels/{channel_id}/messages/{message_id}/reactions/{emoji_path(emoji)}/@me",
                expect=(200, 204),
            )
        finally:
            self._last_reaction_at = self.clock()

    def get_message(self, channel_id: str, message_id: str) -> dict:
        return self._json("GET", f"/channels/{channel_id}/messages/{message_id}")

    def get_reaction_users(
        self,
        channel_id: str,
        message_id: str,
        emoji: str,
        limit: int = REACTION_PAGE,
        after: str | None = None,
        burst: bool = False,
    ) -> list[dict]:
        """One page of reacting users. Always sends ``limit`` (Discord's default is only 25)."""
        params: dict[str, Any] = {"limit": max(1, min(int(limit), REACTION_PAGE))}
        if after:
            params["after"] = after
        if burst:
            params["type"] = 1
        return self._json(
            "GET",
            f"/channels/{channel_id}/messages/{message_id}/reactions/{emoji_path(emoji)}",
            params=params,
        )
