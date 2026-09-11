from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from socagents.core.errors import ConfigError, ModelError
from socagents.model_gateway.anthropic import AnthropicModel, to_anthropic_messages
from socagents.model_gateway.gemini import GeminiModel, to_gemini_schema
from socagents.model_gateway.openai_compat import OpenAICompatibleModel, parse_openai_response
from socagents.model_gateway.types import Message, ToolCall, ToolSpec
from socagents.tools.market import ChainSummaryIn, QuoteIn

TOOLS = [ToolSpec(name="get_quote", description="Quote.", input_schema=QuoteIn.model_json_schema())]
HISTORY = [
    Message(role="user", content="SPY?"),
    Message(
        role="assistant",
        content="",
        tool_calls=[ToolCall(id="c1", name="get_quote", arguments={"symbols": ["SPY"]})],
    ),
    Message(role="tool", tool_call_id="c1", name="get_quote", content='{"data":1}'),
]


def transport(responses: list[httpx.Response], seen: list[httpx.Request]) -> httpx.MockTransport:
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return queue.pop(0)

    return httpx.MockTransport(handler)


def body(request: httpx.Request) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(request.content)
    return data


async def run(model: Any) -> Any:
    try:
        return await model.complete(system="sys", messages=HISTORY, tools=TOOLS, max_tokens=100)
    finally:
        await model.aclose()


# Anthropic


async def test_anthropic_request_and_response() -> None:
    seen: list[httpx.Request] = []
    reply = {
        "content": [
            {"type": "text", "text": "hi"},
            {"type": "tool_use", "id": "t1", "name": "get_quote", "input": {"symbols": ["QQQ"]}},
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "stop_reason": "tool_use",
    }
    model = AnthropicModel(
        "test-model", api_key="k", transport=transport([httpx.Response(200, json=reply)], seen)
    )
    result = await run(model)

    request = seen[0]
    sent = body(request)
    assert request.headers["x-api-key"] == "k"
    assert request.headers["anthropic-version"]
    assert sent["system"] == "sys"
    assert sent["max_tokens"] == 100
    assert sent["tools"][0]["name"] == "get_quote"
    assert sent["messages"][1]["content"][0]["type"] == "tool_use"
    assert sent["messages"][2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "c1",
        "content": '{"data":1}',
        "is_error": False,
    }
    assert result.text == "hi"
    assert result.tool_calls[0].arguments == {"symbols": ["QQQ"]}
    assert (result.usage.input_tokens, result.usage.output_tokens) == (10, 5)


def test_anthropic_merges_consecutive_tool_results() -> None:
    messages = [
        *HISTORY,
        Message(role="tool", tool_call_id="c2", name="get_bars", content="{}"),
    ]
    converted = to_anthropic_messages(messages)
    assert len(converted) == 3
    assert [b["tool_use_id"] for b in converted[2]["content"]] == ["c1", "c2"]


def test_anthropic_requires_key() -> None:
    with pytest.raises(ConfigError):
        AnthropicModel("test-model")


# OpenAI and Ollama


async def test_openai_request_and_response() -> None:
    seen: list[httpx.Request] = []
    reply = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "t1",
                            "type": "function",
                            "function": {"name": "get_quote", "arguments": '{"symbols":["SPY"]}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3},
    }
    model = OpenAICompatibleModel(
        "test-model", api_key="k", transport=transport([httpx.Response(200, json=reply)], seen)
    )
    result = await run(model)

    request = seen[0]
    sent = body(request)
    assert str(request.url) == "https://api.openai.com/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer k"
    assert sent["messages"][0] == {"role": "system", "content": "sys"}
    assert sent["messages"][2]["tool_calls"][0]["function"]["arguments"] == '{"symbols": ["SPY"]}'
    assert sent["messages"][3] == {"role": "tool", "tool_call_id": "c1", "content": '{"data":1}'}
    assert sent["max_completion_tokens"] == 100
    assert sent["tools"][0]["function"]["name"] == "get_quote"
    assert result.text == ""
    assert result.tool_calls[0].arguments == {"symbols": ["SPY"]}
    assert result.usage.input_tokens == 12


async def test_ollama_uses_local_endpoint_without_auth() -> None:
    seen: list[httpx.Request] = []
    reply = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
    model = OpenAICompatibleModel(
        "llama",
        provider="ollama",
        base_url="http://localhost:11434/v1",
        api_key=None,
        token_param="max_tokens",
        transport=transport([httpx.Response(200, json=reply)], seen),
    )
    result = await run(model)
    assert str(seen[0].url) == "http://localhost:11434/v1/chat/completions"
    assert "authorization" not in seen[0].headers
    assert body(seen[0])["max_tokens"] == 100
    assert result.text == "ok"


def test_openai_unparseable_arguments_are_kept_for_rejection() -> None:
    data = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {"id": "t", "function": {"name": "get_quote", "arguments": "{bad"}}
                    ]
                }
            }
        ]
    }
    result = parse_openai_response(data, "m")
    assert result.tool_calls[0].arguments == {"_unparsed_arguments": "{bad"}


def test_openai_requires_key() -> None:
    with pytest.raises(ConfigError):
        OpenAICompatibleModel("test-model")


# Gemini


async def test_gemini_request_and_response() -> None:
    seen: list[httpx.Request] = []
    reply = {
        "candidates": [
            {
                "content": {
                    "parts": [{"functionCall": {"name": "get_quote", "args": {"symbols": ["SPY"]}}}]
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 2},
    }
    model = GeminiModel(
        "test-model", api_key="k", transport=transport([httpx.Response(200, json=reply)], seen)
    )
    result = await run(model)

    request = seen[0]
    sent = body(request)
    assert request.url.path.endswith("/models/test-model:generateContent")
    assert request.headers["x-goog-api-key"] == "k"
    assert "key" not in request.url.params
    assert [c["role"] for c in sent["contents"]] == ["user", "model", "user"]
    assert sent["contents"][2]["parts"][0]["functionResponse"]["name"] == "get_quote"
    assert result.tool_calls[0].name == "get_quote"
    assert result.tool_calls[0].id.startswith("call_")
    assert result.usage.output_tokens == 2


def test_gemini_schema_is_reduced_to_supported_subset() -> None:
    schema = to_gemini_schema(ChainSummaryIn.model_json_schema())
    text = json.dumps(schema)
    assert "title" not in text
    assert "anyOf" not in text
    assert "additionalProperties" not in text
    assert schema["properties"]["expiration"]["nullable"] is True
    assert schema["properties"]["expiration"]["format"] == "date"


# Retries and errors

Factory = Callable[[httpx.MockTransport], Any]


async def test_retries_rate_limits_then_succeeds() -> None:
    seen: list[httpx.Request] = []
    ok = {"content": [{"type": "text", "text": "done"}], "usage": {}}
    model = AnthropicModel(
        "m",
        api_key="k",
        backoff_s=0,
        transport=transport([httpx.Response(429, json={}), httpx.Response(200, json=ok)], seen),
    )
    result = await run(model)
    assert len(seen) == 2
    assert result.text == "done"


async def test_client_errors_are_not_retried() -> None:
    seen: list[httpx.Request] = []
    model = AnthropicModel(
        "m",
        api_key="k",
        backoff_s=0,
        transport=transport([httpx.Response(400, json={"error": {"message": "bad input"}})], seen),
    )
    with pytest.raises(ModelError) as info:
        await run(model)
    assert info.value.code == "model_http_error"
    assert "bad input" in str(info.value)
    assert "k" not in str(info.value).split()
    assert len(seen) == 1
