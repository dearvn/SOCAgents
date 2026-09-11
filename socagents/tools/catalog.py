"""All built-in tools, and registries narrowed to a role's tool list."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from socagents.tools.market import BARS_TOOL, CHAIN_TOOL, QUOTE_TOOL
from socagents.tools.member import MEMBER_TOOLS
from socagents.tools.registry import ToolRegistry
from socagents.tools.research import RESEARCH_TOOLS
from socagents.tools.sdk import Tool

ALL_TOOLS: tuple[Tool[Any, Any], ...] = (
    QUOTE_TOOL,
    CHAIN_TOOL,
    BARS_TOOL,
    *RESEARCH_TOOLS,
    *MEMBER_TOOLS,
)
_BY_NAME = {tool.name: tool for tool in ALL_TOOLS}


def full_registry() -> ToolRegistry:
    return registry_for(_BY_NAME)


def registry_for(names: Iterable[str]) -> ToolRegistry:
    registry = ToolRegistry()
    for name in names:
        tool = _BY_NAME.get(name)
        if tool is None:
            raise KeyError(f"Unknown tool {name!r}.")
        registry.register(tool)
    return registry
