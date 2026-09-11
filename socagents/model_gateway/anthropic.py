"""Anthropic Messages API provider (bring your own key)."""

from __future__ import annotations

import os
from typing import Any

import httpx

from socagents.core.errors import ConfigError
from socagents.model_gateway.http import HttpModelBase
from socagents.model_gateway.types import Message, ModelResponse, ToolCall, ToolSpec, Usage

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


class AnthropicModel(HttpModelBase):
    provider = "anthropic"

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str = ANTHROPIC_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        backoff_s: float = 1.0,
    ) -> None:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ConfigError("ANTHROPIC_API_KEY is not set.")
        super().__init__(transport=transport, backoff_s=backoff_s)
        self.model = model
        self._key = key
        self._url = base_url

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
            "max_tokens": max_tokens,
            "system": system,
            "messages": to_anthropic_messages(messages),
        }
        if tools:
            body["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in tools
            ]
        headers = {
            "x-api-key": self._key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        data = await self._post_json(self._url, headers=headers, body=body)
        return parse_anthropic_response(data, self.model)


def to_anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "user":
            out.append({"role": "user", "content": m.content})
        elif m.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            blocks.extend(
                {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                for c in m.tool_calls
            )
            out.append({"role": "assistant", "content": blocks})
        else:
            block = {
                "type": "tool_result",
                "tool_use_id": m.tool_call_id,
                "content": m.content,
                "is_error": m.is_error,
            }
            previous = out[-1] if out else None
            if (
                previous is not None
                and previous["role"] == "user"
                and isinstance(previous["content"], list)
            ):
                previous["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    return out


def parse_anthropic_response(data: dict[str, Any], model: str) -> ModelResponse:
    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in data.get("content", []):
        if block.get("type") == "text":
            texts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            calls.append(
                ToolCall(id=block["id"], name=block["name"], arguments=block.get("input") or {})
            )
    usage = data.get("usage") or {}
    return ModelResponse(
        text="".join(texts),
        tool_calls=calls,
        usage=Usage(
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
        ),
        stop_reason=str(data.get("stop_reason") or ""),
        model=model,
    )
