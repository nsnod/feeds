"""Optional Claude classifier: raw HTTP through HttpClient, mocked with respx."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from gembot.config import LLMSettings
from gembot.enrich.llm import (
    ANTHROPIC_VERSION,
    HARD_CAP,
    MAX_POST_CHARS,
    SCHEMA,
    LLMClassifier,
    build_llm,
)
from gembot.http import Budget
from gembot.models import LLMVerdict
from tests.factories import make_config, make_http, make_mention

API = "https://api.anthropic.com/v1/messages"
GOOD = {
    "game_title": "Gorilla Pizza Panic",
    "is_a_specific_game": True,
    "friendslop_fit_0_1": 0.92,
    "one_line_pitch": "Gorillas deliver pizza together in a physics-driven co-op scramble.",
}


def reply(payload, stop_reason: str = "end_turn", raw_text: str | None = None) -> httpx.Response:
    text = raw_text if raw_text is not None else json.dumps(payload)
    return httpx.Response(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop_reason,
            "usage": {"input_tokens": 700, "output_tokens": 60},
        },
    )


def classifier(**kw) -> LLMClassifier:
    settings = kw.pop("settings", LLMSettings())
    return LLMClassifier(
        "sk-ant-test-key", make_http(**kw.pop("http_kw", {})), settings, kw.pop("budget", None)
    )


def post(**kw):
    kw.setdefault("title", "My game Gorilla Pizza Panic is a co-op pizza delivery game")
    kw.setdefault("text", "Proximity chat, ragdolls and pizza. Wishlist it!")
    kw.setdefault("links", ["https://store.steampowered.com/app/777/"])
    kw.setdefault("channel", "r/IndieDev")
    return make_mention("reddit", kw.pop("source_id", "r1"), **kw)


@respx.mock
def test_success_and_request_shape():
    route = respx.post(API).mock(return_value=reply(GOOD))
    llm = classifier()
    verdict = llm.classify(post(text="x" * 5000))
    assert verdict == LLMVerdict(
        game_title="Gorilla Pizza Panic",
        is_a_specific_game=True,
        friendslop_fit=0.92,
        one_line_pitch=GOOD["one_line_pitch"],
    )
    request = route.calls.last.request
    assert request.headers["x-api-key"] == "sk-ant-test-key"
    assert request.headers["anthropic-version"] == ANTHROPIC_VERSION == "2023-06-01"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["user-agent"] == "GemBot-test/0.1"
    body = json.loads(request.content)
    assert body["model"] == "claude-haiku-4-5-20251001"
    assert body["max_tokens"] == 400
    assert isinstance(body["system"], str) and "friendslop" in body["system"]
    assert body["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}
    (message,) = body["messages"]
    assert message["role"] == "user"
    content = message["content"]
    assert content.startswith("<post>") and content.endswith("</post>")
    assert "Source: reddit (r/IndieDev)" in content
    assert "Title: My game Gorilla Pizza Panic" in content
    assert "Links: https://store.steampowered.com/app/777/" in content
    assert "x" * MAX_POST_CHARS in content and "x" * (MAX_POST_CHARS + 1) not in content
    assert llm.budget.used == 1 and llm.calls == 1 and llm.last_error is None


def test_schema_is_strict_and_nullable_fields_use_any_of():
    assert SCHEMA["additionalProperties"] is False
    assert SCHEMA["required"] == ["game_title", "is_a_specific_game", "friendslop_fit_0_1", "one_line_pitch"]
    assert SCHEMA["properties"]["game_title"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}
    assert SCHEMA["properties"]["one_line_pitch"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}
    assert SCHEMA["properties"]["is_a_specific_game"] == {"type": "boolean"}
    assert SCHEMA["properties"]["friendslop_fit_0_1"] == {"type": "number"}


@respx.mock
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {**GOOD, "friendslop_fit_0_1": 1.7, "game_title": "  Gorilla   Pizza Panic "},
            {"friendslop_fit": 1.0, "game_title": "Gorilla Pizza Panic"},
        ),
        ({**GOOD, "friendslop_fit_0_1": -0.3}, {"friendslop_fit": 0.0}),
        ({**GOOD, "friendslop_fit_0_1": "high"}, {"friendslop_fit": 0.0}),
        ({**GOOD, "friendslop_fit_0_1": float("nan")}, {"friendslop_fit": 0.0}),
        ({**GOOD, "is_a_specific_game": "yes"}, {"is_a_specific_game": False}),
        (
            {
                "game_title": None,
                "is_a_specific_game": False,
                "friendslop_fit_0_1": 0,
                "one_line_pitch": None,
            },
            {"game_title": None, "one_line_pitch": None, "friendslop_fit": 0.0},
        ),
        ({**GOOD, "game_title": "   ", "one_line_pitch": "  "}, {"game_title": None, "one_line_pitch": None}),
        ({**GOOD, "one_line_pitch": 42}, {"one_line_pitch": None}),
    ],
)
def test_values_are_cleaned_and_clamped(payload, expected):
    respx.post(API).mock(return_value=reply(payload))
    verdict = classifier().classify(post())
    assert verdict is not None
    for key, value in expected.items():
        assert getattr(verdict, key) == value


@respx.mock
def test_long_pitch_is_clipped_to_140_chars():
    pitch = "A chaotic co-op game " + "where gorillas deliver pizza " * 10
    respx.post(API).mock(return_value=reply({**GOOD, "one_line_pitch": pitch}))
    verdict = classifier().classify(post())
    assert verdict is not None and len(verdict.one_line_pitch) <= 140
    assert verdict.one_line_pitch.endswith("…") and "  " not in verdict.one_line_pitch
    unbroken = "x" * 300
    respx.post(API).mock(return_value=reply({**GOOD, "one_line_pitch": unbroken}))
    assert len(classifier().classify(post()).one_line_pitch) == 140


@respx.mock
def test_structured_output_rejected_falls_back_once_and_stays_off():
    rejected = httpx.Response(
        400,
        json={
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "output_config: Extra inputs are not permitted",
            },
        },
    )
    loose_text = 'Here you go:\n```json\n{"game_title": "Moss Movers", "is_a_specific_game": true, ' + (
        '"friendslop_fit_0_1": 0.4, "one_line_pitch": null}\n```'
    )
    route = respx.post(API).mock(
        side_effect=[rejected, reply(None, raw_text=loose_text), reply(None, raw_text=loose_text)]
    )
    llm = classifier()
    verdict = llm.classify(post())
    assert verdict == LLMVerdict(game_title="Moss Movers", is_a_specific_game=True, friendslop_fit=0.4)
    first, second = (json.loads(c.request.content) for c in route.calls)
    assert "output_config" in first and "output_config" not in second
    assert "Reply with only a JSON object" in second["system"]
    assert llm.structured is False and llm.budget.used == 2
    assert llm.classify(post(source_id="r2")) is not None
    assert route.call_count == 3  # the next call goes straight to the plain request
    assert "output_config" not in json.loads(route.calls.last.request.content)


@respx.mock
def test_fallback_reply_without_json_object_returns_none():
    respx.post(API).mock(
        side_effect=[
            httpx.Response(400, json={"error": {"message": "unknown field: output_config.format"}}),
            reply(None, raw_text="I can't find a { proper object here"),
        ]
    )
    llm = classifier()
    assert llm.classify(post()) is None
    assert "no JSON object" in llm.last_error


@respx.mock
def test_fallback_skipped_when_budget_is_gone():
    respx.post(API).mock(return_value=httpx.Response(400, json={"error": {"message": "bad output_config"}}))
    llm = classifier(budget=Budget("llm", 1))
    assert llm.classify(post()) is None
    assert "budget" in llm.last_error and llm.budget.used == 1


@respx.mock
def test_other_400_is_not_retried():
    route = respx.post(API).mock(
        return_value=httpx.Response(400, json={"error": {"message": "max_tokens: too large"}})
    )
    llm = classifier()
    assert llm.classify(post()) is None
    assert route.call_count == 1 and "HTTP 400" in llm.last_error and llm.structured is True


@respx.mock
@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_refusal_and_truncation_return_none(stop_reason):
    respx.post(API).mock(return_value=reply(GOOD, stop_reason=stop_reason))
    llm = classifier()
    assert llm.classify(post()) is None
    assert stop_reason in llm.last_error


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        reply(None, raw_text="{not json"),
        reply(None, raw_text='["a", "list"]'),
        reply(None, raw_text=""),
        httpx.Response(200, json={"content": [{"type": "tool_use", "id": "x"}], "stop_reason": "end_turn"}),
        httpx.Response(200, json={"content": None, "stop_reason": "end_turn"}),
        httpx.Response(200, text="<html>gateway</html>"),
    ],
)
def test_malformed_replies_return_none(response):
    respx.post(API).mock(return_value=response)
    llm = classifier()
    assert llm.classify(post()) is None
    assert llm.last_error


@respx.mock
def test_server_errors_and_rate_limits_never_raise():
    route = respx.post(API).mock(
        return_value=httpx.Response(529, json={"error": {"type": "overloaded_error"}})
    )
    llm = classifier(http_kw={"retries": 2})
    assert llm.classify(post()) is None
    assert route.call_count == 3 and "529" in llm.last_error and llm.budget.used == 3
    respx.post(API).mock(return_value=httpx.Response(429, headers={"retry-after": "120"}))
    assert classifier().classify(post()) is None
    slept: list[float] = []
    respx.post(API).mock(side_effect=[httpx.Response(429, headers={"retry-after": "2"}), reply(GOOD)])
    assert classifier(http_kw={"sleep": slept.append}).classify(post()) is not None
    assert slept == [2.0]
    respx.post(API).mock(side_effect=httpx.ConnectError("offline"))
    assert classifier(http_kw={"retries": 0}).classify(post()) is None


@respx.mock
def test_hard_cap_of_40_calls_per_run():
    route = respx.post(API).mock(return_value=reply(GOOD))
    llm = classifier(settings=LLMSettings(max_calls=500))
    assert llm.budget.limit == HARD_CAP == 40
    results = [llm.classify(post(source_id=str(i))) for i in range(41)]
    assert all(r is not None for r in results[:40])
    assert results[40] is None and route.call_count == 40
    assert "budget" in llm.last_error
    capped = classifier(budget=Budget("llm", 999))
    assert capped.budget.limit == 40


@respx.mock
def test_steam_details_and_custom_api_base_are_used():
    route = respx.post("https://proxy.example/v1/messages").mock(return_value=reply(GOOD))
    llm = classifier(settings=LLMSettings(api_base="https://proxy.example/", model="claude-haiku-4-5"))
    mention = make_mention(
        "steam",
        "777",
        title="Gorilla Pizza Panic",
        extra={
            "steam": {
                "appid": 777,
                "short_description": "Deliver pizza as gorillas.",
                "categories": ["Online Co-op"],
            }
        },
    )
    assert llm.classify(mention) is not None
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "claude-haiku-4-5"
    content = body["messages"][0]["content"]
    assert "Steam: Deliver pizza as gorillas. | Steam categories: Online Co-op" in content
    assert "Text: (none)" in content and "Links: (none)" in content


def test_build_llm_requires_a_key_and_respects_budgets():
    http = make_http()
    assert build_llm(make_config(), http) is None
    config = make_config(env={"ANTHROPIC_API_KEY": "sk-ant-x"})
    llm = build_llm(config, http)
    assert isinstance(llm, LLMClassifier)
    assert llm.budget.limit == min(config.settings.llm.max_calls, config.settings.budgets.llm, 40)
    assert (
        llm.http is not http and llm.http.timeout == config.settings.llm.timeout_s
    )  # longer timeout for Claude
    assert llm.http.user_agent == http.user_agent
    llm.close()
    small = make_config(env={"ANTHROPIC_API_KEY": "sk-ant-x"})
    small.settings.budgets.llm = 5
    small.settings.llm.timeout_s = 5
    llm = build_llm(small, http)
    assert llm.budget.limit == 5 and llm.http is http
    llm.close()  # does not close the shared client
    assert http._client is None or not http._client.is_closed
