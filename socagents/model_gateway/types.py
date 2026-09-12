"""Provider-neutral message, tool, and response types."""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

# Stop reasons that mean the reply was cut off at max_tokens (Anthropic, OpenAI, Gemini).
TRUNCATED_STOP_REASONS = frozenset({"max_tokens", "length", "MAX_TOKENS"})


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    role: Literal["user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    is_error: bool = False


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class ModelResponse(BaseModel):
    text: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    stop_reason: str = ""
    model: str


class ModelProvider(Protocol):
    provider: str
    model: str

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int,
    ) -> ModelResponse: ...

    async def aclose(self) -> None: ...
