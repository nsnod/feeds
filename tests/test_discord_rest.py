"""Discord REST client (respx, no network) and the in-memory FakeDiscord."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from gembot.discord.fake import FIRST_ID, FakeDiscord
from gembot.discord.rest import (
    GATEWAY_HINT,
    USER_AGENT,
    DiscordAPI,
    DiscordClient,
    DiscordError,
    emoji_path,
    prepare_message,
)
from gembot.http import Budget, BudgetExceeded, HttpError
from tests.factories import make_http

API = "https://discord.com/api/v10"
TOKEN = "tok.SECRET.123"


class Clock:
    def __init__(self, start: float = 100.0, step: float = 0.0):
        self.now = start
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


@pytest.fixture
def slept() -> list[float]:
    return []


@pytest.fixture
def client(slept) -> DiscordClient:
    return DiscordClient(TOKEN, make_http(sleep=slept.append), Budget("discord", 50), clock=Clock())


def _body(route: respx.Route) -> dict:
    return json.loads(route.calls.last.request.content)


# ---------------------------------------------------------------- happy paths


@respx.mock
def test_me_sends_bot_auth_and_discord_user_agent(client):
    route = respx.get(f"{API}/users/@me").mock(
        return_value=httpx.Response(200, json={"id": "1000", "username": "GemBot", "bot": True})
    )
    assert client.me()["id"] == "1000"
    headers = route.calls.last.request.headers
    assert headers["authorization"] == f"Bot {TOKEN}"
    assert headers["user-agent"] == USER_AGENT == "DiscordBot (https://github.com/nsnod/feeds, 0.1)"
    assert client.budget.used == 1
    assert TOKEN not in repr(client)


@respx.mock
def test_list_guilds_and_channels(client):
    respx.get(f"{API}/users/@me/guilds").mock(
        return_value=httpx.Response(200, json=[{"id": "1", "name": "S"}])
    )
    channels = respx.get(f"{API}/guilds/1/channels").mock(
        return_value=httpx.Response(200, json=[{"id": "7", "name": "general", "type": 0}])
    )
    assert client.list_guilds() == [{"id": "1", "name": "S"}]
    assert client.list_channels("1")[0]["name"] == "general"
    assert channels.calls.last.request.method == "GET"


@respx.mock
def test_create_channel_posts_name_type_parent_and_topic(client):
    route = respx.post(f"{API}/guilds/1/channels").mock(
        side_effect=[
            httpx.Response(201, json={"id": "40", "name": "GemBot", "type": 4}),
            httpx.Response(201, json={"id": "41", "name": "gem-alarm", "type": 0, "parent_id": "40"}),
        ]
    )
    assert client.create_channel("1", "GemBot", 4)["id"] == "40"
    assert _body(route) == {"name": "GemBot", "type": 4}
    created = client.create_channel("1", "gem-alarm", 0, parent_id="40", topic="Alarms")
    assert created["parent_id"] == "40"
    assert _body(route) == {"name": "gem-alarm", "type": 0, "parent_id": "40", "topic": "Alarms"}
    assert route.calls.last.request.headers["content-type"] == "application/json"


@respx.mock
def test_send_message_enforces_limits_and_never_pings_by_default(client):
    route = respx.post(f"{API}/channels/9/messages").mock(
        return_value=httpx.Response(200, json={"id": "555", "channel_id": "9"})
    )
    assert client.send_message("9", {"content": "x" * 2500, "embeds": [{"title": "t" * 300}]})["id"] == "555"
    body = _body(route)
    assert len(body["content"]) <= 2000 and len(body["embeds"][0]["title"]) <= 256
    assert body["allowed_mentions"] == {"parse": []}

    client.send_message("9", {"content": "<@&42> hi", "allowed_mentions": {"parse": [], "roles": ["42"]}})
    assert _body(route)["allowed_mentions"] == {"parse": [], "roles": ["42"]}


@respx.mock
def test_add_reaction_url_encodes_emoji_and_expects_204(client):
    route = respx.put(url__regex=rf"{API}/channels/9/messages/555/reactions/.+/@me").mock(
        return_value=httpx.Response(204)
    )
    assert client.add_reaction("9", "555", "👍") is None
    assert (
        route.calls.last.request.url.raw_path
        == b"/api/v10/channels/9/messages/555/reactions/%F0%9F%91%8D/@me"
    )
    client.add_reaction("9", "555", "gem:123456")
    assert route.calls.last.request.url.raw_path.endswith(b"/reactions/gem:123456/@me")
    assert emoji_path("👎") == "%F0%9F%91%8E"


@respx.mock
def test_reactions_are_paced(slept):
    respx.put(url__regex=r".*/reactions/.*").mock(return_value=httpx.Response(204))
    paced = DiscordClient(TOKEN, make_http(sleep=slept.append), Budget("discord", 10), clock=Clock(step=0.1))
    paced.add_reaction("9", "1", "👍")
    paced.add_reaction("9", "1", "👎")
    assert slept == [pytest.approx(0.2)]  # 0.3s pacing minus the 0.1s that already passed

    slow: list[float] = []
    relaxed = DiscordClient(TOKEN, make_http(sleep=slow.append), Budget("discord", 10), clock=Clock(step=5))
    relaxed.add_reaction("9", "1", "👍")
    relaxed.add_reaction("9", "1", "👎")
    assert slow == []


@respx.mock
def test_get_message_and_reaction_users(client):
    respx.get(f"{API}/channels/9/messages/555").mock(
        return_value=httpx.Response(
            200, json={"id": "555", "reactions": [{"emoji": {"name": "👍"}, "count": 2}]}
        )
    )
    users = respx.get(url__regex=rf"{API}/channels/9/messages/555/reactions/%F0%9F%91%8D.*").mock(
        return_value=httpx.Response(200, json=[{"id": "1"}, {"id": "2", "bot": True}])
    )
    assert client.get_message("9", "555")["reactions"][0]["count"] == 2
    assert client.get_reaction_users("9", "555", "👍") == [{"id": "1"}, {"id": "2", "bot": True}]
    assert users.calls.last.request.url.params["limit"] == "100"
    client.get_reaction_users("9", "555", "👍", limit=500)
    assert users.calls.last.request.url.params["limit"] == "100"


# ---------------------------------------------------------------- rate limits


@respx.mock
def test_429_is_retried_after_json_retry_after(client, slept):
    route = respx.post(f"{API}/channels/9/messages").mock(
        side_effect=[
            httpx.Response(
                429, json={"message": "You are being rate limited.", "retry_after": 0.75, "global": False}
            ),
            httpx.Response(200, json={"id": "1"}),
        ]
    )
    assert client.send_message("9", {"content": "hi"})["id"] == "1"
    assert slept == [0.75]
    assert route.call_count == 2 and client.budget.used == 2


@respx.mock
def test_empty_rate_limit_bucket_waits_for_reset(client, slept):
    respx.get(f"{API}/users/@me").mock(
        side_effect=[
            httpx.Response(
                200,
                json={"id": "1"},
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "1.5"},
            ),
            httpx.Response(
                200,
                json={"id": "1"},
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "999"},
            ),
            httpx.Response(
                200, json={"id": "1"}, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "?"}
            ),
            httpx.Response(
                200, json={"id": "1"}, headers={"X-RateLimit-Remaining": "3", "X-RateLimit-Reset-After": "5"}
            ),
        ]
    )
    for _ in range(4):
        client.me()
    assert slept == [1.5, 20.0]  # bounded by HttpClient.max_backoff_s; junk header ignored


@respx.mock
def test_budget_is_charged_and_enforced(slept):
    respx.get(f"{API}/users/@me").mock(return_value=httpx.Response(200, json={"id": "1"}))
    small = DiscordClient(TOKEN, make_http(sleep=slept.append), Budget("discord", 1))
    small.me()
    with pytest.raises(BudgetExceeded):
        small.me()


# ---------------------------------------------------------------- errors


@respx.mock
def test_403_raises_discord_error_with_code_and_hint(client):
    respx.post(f"{API}/channels/9/messages").mock(
        return_value=httpx.Response(403, json={"message": "Missing Permissions", "code": 50013})
    )
    with pytest.raises(DiscordError) as info:
        client.send_message("9", {"content": "hi"})
    err = info.value
    assert isinstance(err, HttpError)
    assert err.status == 403 and err.code == 50013 and err.discord_message == "Missing Permissions"
    assert "Send Messages" in err.hint and "50013" in str(err)
    assert TOKEN not in str(err)


@respx.mock
def test_missing_access_and_unknown_channel_hints(client):
    respx.get(f"{API}/channels/1/messages/2").mock(
        side_effect=[
            httpx.Response(403, json={"message": "Missing Access", "code": 50001}),
            httpx.Response(404, json={"message": "Unknown Channel", "code": 10003}),
        ]
    )
    with pytest.raises(DiscordError, match="View Channels"):
        client.get_message("1", "2")
    with pytest.raises(DiscordError, match="Setup GemBot") as info:
        client.get_message("1", "2")
    assert info.value.code == 10003


@respx.mock
def test_unauthorized_errors_explain_token_and_gateway(client):
    respx.get(f"{API}/users/@me").mock(
        side_effect=[
            httpx.Response(401, json={"message": "401: Unauthorized", "code": 0}),
            httpx.Response(401, text="nope"),
        ]
    )
    with pytest.raises(DiscordError, match="DISCORD_BOT_TOKEN") as info:
        client.me()
    assert info.value.code == 0
    with pytest.raises(DiscordError, match="Reset Token") as info:
        client.me()
    assert info.value.code is None

    respx.post(f"{API}/channels/9/messages").mock(
        side_effect=[
            httpx.Response(403, json={"message": "Unauthorized", "code": 40001}),
            httpx.Response(
                400, json={"message": "Bot must connect to the gateway at least once", "code": 40002}
            ),
        ]
    )
    with pytest.raises(DiscordError, match="connect to the Discord gateway once") as info:
        client.send_message("9", {"content": "hi"})
    assert info.value.code == 40001
    with pytest.raises(DiscordError) as info:
        client.send_message("9", {"content": "hi"})
    assert info.value.hint == GATEWAY_HINT


@respx.mock
def test_invalid_form_body_details_are_flattened(client):
    respx.post(f"{API}/channels/9/messages").mock(
        return_value=httpx.Response(
            400,
            json={
                "code": 50035,
                "message": "Invalid Form Body",
                "errors": {"embeds": {"0": {"title": {"_errors": [{"code": "X", "message": "Too long."}]}}}},
            },
        )
    )
    with pytest.raises(DiscordError, match=r"embeds\.0\.title: Too long\.") as info:
        client.send_message("9", {"content": "hi"})
    assert info.value.code == 50035


@respx.mock
def test_error_body_without_code_and_invalid_json(client):
    respx.get(f"{API}/guilds/1/channels").mock(
        return_value=httpx.Response(404, json={"message": "404: Not Found"})
    )
    with pytest.raises(DiscordError, match=r"\(404: Not Found\)") as info:
        client.list_channels("1")
    assert info.value.code is None and info.value.hint is None
    respx.get(f"{API}/users/@me/guilds").mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(DiscordError, match="invalid JSON"):
        client.list_guilds()


@respx.mock
def test_server_errors_stay_http_errors_after_retries(client):
    respx.get(f"{API}/users/@me").mock(return_value=httpx.Response(502))
    with pytest.raises(HttpError) as info:
        client.me()
    assert not isinstance(info.value, DiscordError) and info.value.status == 502


def test_client_requires_token_and_uses_http_sleep_by_default():
    with pytest.raises(ValueError, match="DISCORD_BOT_TOKEN"):
        DiscordClient("", make_http(), Budget("discord", 1))
    http = make_http()
    assert DiscordClient("t", http, Budget("discord", 1), api_base=API + "/").api_base == API
    assert DiscordClient("t", http, Budget("discord", 1)).sleep is http.sleep


def test_prepare_message_keeps_input_untouched():
    payload = {"content": "hi"}
    assert prepare_message(payload) == {"content": "hi", "allowed_mentions": {"parse": []}}
    assert payload == {"content": "hi"}


# ---------------------------------------------------------------- FakeDiscord


def test_fake_implements_the_protocol_with_deterministic_ids():
    fake = FakeDiscord(guilds=["Server"])
    assert isinstance(fake, DiscordAPI)
    assert isinstance(DiscordClient("t", make_http(), Budget("d", 1)), DiscordAPI)
    guild_id = next(iter(fake.guilds))
    assert guild_id == str(FIRST_ID)
    category = fake.create_channel(guild_id, "GemBot", 4)
    channel = fake.create_channel(guild_id, "gem-alarm", 0, parent_id=category["id"], topic="t")
    assert int(channel["id"]) == int(category["id"]) + 1
    assert fake.list_channels(guild_id)[1]["topic"] == "t"
    assert fake.me() == {"id": "1000", "username": "GemBot", "bot": True}
    assert fake.list_guilds() == [{"id": guild_id, "name": "Server"}]
    assert fake.channel_by_name("gem-alarm")["id"] == channel["id"]
    assert fake.channel_by_name("nope") is None


def test_fake_messages_and_reactions():
    fake = FakeDiscord()
    guild_id = fake.add_guild()
    channel_id = fake.add_channel(guild_id, "gem-alarm")["id"]
    message = fake.send_message(channel_id, {"content": "hi"})
    assert fake.sent == [(channel_id, {"content": "hi", "allowed_mentions": {"parse": []}})]
    assert fake.messages_in(channel_id)[0]["id"] == message["id"]

    fake.add_reaction(channel_id, message["id"], "👍")
    fake.react(channel_id, message["id"], "👍", "7")
    fake.react(channel_id, message["id"], "👍", "7")  # same user twice counts once
    fake.react(channel_id, message["id"], "👎", "8", bot=True)
    got = fake.get_message(channel_id, message["id"])
    assert got["reactions"] == [
        {"emoji": {"id": None, "name": "👍"}, "count": 2, "me": True},
        {"emoji": {"id": None, "name": "👎"}, "count": 1, "me": False},
    ]
    users = fake.get_reaction_users(channel_id, message["id"], "👍")
    assert users == [{"id": "1000", "username": "user1000", "bot": True}, {"id": "7", "username": "user7"}]
    assert fake.get_reaction_users(channel_id, message["id"], "👍", limit=1) == users[:1]

    fake.unreact(channel_id, message["id"], "👎", "8")
    fake.unreact(channel_id, message["id"], "👍", "7")
    assert [r["emoji"]["name"] for r in fake.get_message(channel_id, message["id"])["reactions"]] == ["👍"]


def test_fake_errors_and_failure_injection():
    fake = FakeDiscord()
    guild_id = fake.add_guild()
    channel_id = fake.add_channel(guild_id, "text")["id"]
    with pytest.raises(DiscordError) as info:
        fake.list_channels("nope")
    assert info.value.code == 10004
    with pytest.raises(DiscordError) as info:
        fake.send_message("nope", {"content": "x"})
    assert info.value.code == 10003
    with pytest.raises(DiscordError) as info:
        fake.get_message(channel_id, "nope")
    assert info.value.code == 10008
    with pytest.raises(DiscordError) as info:
        fake.create_channel(guild_id, "child", 0, parent_id=channel_id)
    assert info.value.code == 50035

    boom = DiscordError("Missing Permissions", 403, code=50013)
    fake.fail("me", boom, times=2)
    for _ in range(2):
        with pytest.raises(DiscordError):
            fake.me()
    assert fake.me()["id"] == "1000"
    fake.fail("list_guilds", boom)
    for _ in range(3):
        with pytest.raises(DiscordError):
            fake.list_guilds()
    assert ("me",) in fake.calls
