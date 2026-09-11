"""Google Gemini API provider (bring your own key)."""

from __future__ import annotations

import os
from typing import Any

import httpx

from socagents.core.errors import ConfigError
from socagents.core.ids import new_id
from socagents.model_gateway.http import HttpModelBase
from socagents.model_gateway.types import Message, ModelResponse, ToolCall, ToolSpec, Usage

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

# Gemini function declarations accept an OpenAPI-style subset of JSON Schema.
_GEMINI_SCHEMA_KEYS = {
    "type",
    "format",
    "description",
    "nullable",
    "enum",
    "items",
    "properties",
    "required",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "pattern",
}


class GeminiModel(HttpModelBase):
    provider = "google"

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str = GEMINI_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        backoff_s: float = 1.0,
    ) -> None:
        key = api_key or os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise ConfigError("GOOGLE_API_KEY (or GEMINI_API_KEY) is not set.")
        super().__init__(transport=transport, backoff_s=backoff_s)
        self.model = model
        self._key = key
        self._url = f"{base_url.rstrip('/')}/models/{model}:generateContent"

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int,
    ) -> ModelResponse:
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": to_gemini_contents(messages),
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        if tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "parameters": to_gemini_schema(t.input_schema),
                        }
                        for t in tools
                    ]
                }
            ]
        headers = {"x-goog-api-key": self._key, "content-type": "application/json"}
        data = await self._post_json(self._url, headers=headers, body=body)
        return parse_gemini_response(data, self.model)


def to_gemini_contents(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "user":
            out.append({"role": "user", "parts": [{"text": m.content}]})
        elif m.role == "assistant":
            parts: list[dict[str, Any]] = []
            if m.content:
                parts.append({"text": m.content})
            parts.extend(
                {"functionCall": {"name": c.name, "args": c.arguments}} for c in m.tool_calls
            )
            out.append({"role": "model", "parts": parts})
        else:
            part = {"functionResponse": {"name": m.name or "", "response": {"content": m.content}}}
            previous = out[-1] if out else None
            if (
                previous is not None
                and previous["role"] == "user"
                and all("functionResponse" in p for p in previous["parts"])
            ):
                previous["parts"].append(part)
            else:
                out.append({"role": "user", "parts": [part]})
    return out


def to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline ``$ref``s and reduce a Pydantic JSON Schema to the subset Gemini accepts."""
    defs: dict[str, Any] = schema.get("$defs", {})

    def convert(node: Any) -> Any:
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return convert(defs[node["$ref"].rsplit("/", 1)[-1]])
        if "anyOf" in node:
            options = [o for o in node["anyOf"] if o.get("type") != "null"]
            nullable = len(options) < len(node["anyOf"])
            merged = convert(options[0]) if len(options) == 1 else {"type": "string"}
            if nullable:
                merged["nullable"] = True
            if "description" in node:
                merged["description"] = node["description"]
            return merged
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key not in _GEMINI_SCHEMA_KEYS:
                continue
            if key == "properties":
                out[key] = {name: convert(prop) for name, prop in value.items()}
            elif key == "items":
                out[key] = convert(value)
            else:
                out[key] = value
        return out

    result: dict[str, Any] = convert(schema)
    return result


def parse_gemini_response(data: dict[str, Any], model: str) -> ModelResponse:
    candidates = data.get("candidates") or []
    parts = (candidates[0].get("content") or {}).get("parts", []) if candidates else []
    texts: list[str] = []
    calls: list[ToolCall] = []
    for part in parts:
        if "text" in part:
            texts.append(part["text"])
        elif "functionCall" in part:
            fc = part["functionCall"]
            calls.append(
                ToolCall(
                    id=fc.get("id") or new_id("call"),
                    name=fc["name"],
                    arguments=fc.get("args") or {},
                )
            )
    usage = data.get("usageMetadata") or {}
    return ModelResponse(
        text="".join(texts),
        tool_calls=calls,
        usage=Usage(
            input_tokens=int(usage.get("promptTokenCount", 0)),
            output_tokens=int(usage.get("candidatesTokenCount", 0)),
        ),
        stop_reason=str(candidates[0].get("finishReason") or "") if candidates else "",
        model=model,
    )
