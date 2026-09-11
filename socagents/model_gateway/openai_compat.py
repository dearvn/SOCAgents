"""OpenAI Chat Completions provider. Also serves Ollama and other OpenAI-compatible endpoints."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from socagents.core.errors import ConfigError
from socagents.model_gateway.http import HttpModelBase
from socagents.model_gateway.types import Message, ModelResponse, ToolCall, ToolSpec, Usage

OPENAI_BASE_URL = "https://api.openai.com/v1"


class OpenAICompatibleModel(HttpModelBase):
    def __init__(
        self,
        model: str,
        *,
        provider: str = "openai",
        base_url: str = OPENAI_BASE_URL,
        api_key: str | None = None,
        token_param: str = "max_completion_tokens",
        transport: httpx.AsyncBaseTransport | None = None,
        backoff_s: float = 1.0,
    ) -> None:
        if provider == "openai":
            api_key = api_key or os.environ.get("OPENAI_API_KEY")
            if not api_key:
                raise ConfigError("OPENAI_API_KEY is not set.")
        super().__init__(transport=transport, backoff_s=backoff_s)
        self.provider = provider
        self.model = model
        self._key = api_key
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._token_param = token_param

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int,
    ) -> ModelResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *to_openai_messages(messages)],
            self._token_param: max_tokens,
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ]
        headers = {"content-type": "application/json"}
        if self._key:
            headers["authorization"] = f"Bearer {self._key}"
        data = await self._post_json(self._url, headers=headers, body=body)
        return parse_openai_response(data, self.model)


def to_openai_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "user":
            out.append({"role": "user", "content": m.content})
        elif m.role == "assistant":
            item: dict[str, Any] = {"role": "assistant", "content": m.content or None}
            if m.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                    }
                    for c in m.tool_calls
                ]
            out.append(item)
        else:
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
    return out


def parse_openai_response(data: dict[str, Any], model: str) -> ModelResponse:
    choices = data.get("choices") or []
    message = choices[0].get("message", {}) if choices else {}
    calls: list[ToolCall] = []
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function", {})
        raw = fn.get("arguments") or "{}"
        try:
            arguments = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except json.JSONDecodeError:
            # Keep the raw text so the tool gateway rejects it as invalid arguments.
            arguments = {"_unparsed_arguments": raw}
        if not isinstance(arguments, dict):
            arguments = {"_unparsed_arguments": raw}
        calls.append(
            ToolCall(
                id=tc.get("id") or fn.get("name", "call"),
                name=fn.get("name", ""),
                arguments=arguments,
            )
        )
    usage = data.get("usage") or {}
    return ModelResponse(
        text=message.get("content") or "",
        tool_calls=calls,
        usage=Usage(
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
        ),
        stop_reason=str(choices[0].get("finish_reason") or "") if choices else "",
        model=model,
    )
