"""Tool registry."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from socagents.model_gateway.types import ToolSpec
from socagents.tools.sdk import Tool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool[Any, Any]] = {}

    def register(self, tool: Tool[Any, Any]) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool {tool.name!r} is already registered.")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool[Any, Any] | None:
        return self._tools.get(name)

    def specs(self) -> list[ToolSpec]:
        return [tool.spec() for tool in self._tools.values()]

    def __iter__(self) -> Iterator[Tool[Any, Any]]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)
