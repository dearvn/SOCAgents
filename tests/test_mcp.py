from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip("mcp")

from socagents.core.config import Settings
from socagents.mcp.server import build_server


def payload(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structured_content", None) or getattr(
        result, "structuredContent", None
    )
    if isinstance(structured, dict):
        return structured.get("result", structured) if set(structured) == {"result"} else structured
    return json.loads(result.content[0].text)


@pytest.fixture
def server() -> Any:
    return build_server(Settings.from_env())


async def test_tools_are_listed_read_only(server: Any) -> None:
    tools = {t.name: t for t in await server.list_tools()}
    assert {
        "community_quote",
        "community_gex_estimate",
        "community_technicals",
        "socswift_gex_snapshot",
        "run_desk",
    } <= set(tools)
    assert all(t.annotations and t.annotations.read_only_hint for t in tools.values())


async def test_community_quote(server: Any) -> None:
    data = payload(await server.call_tool("community_quote", {"symbols": ["SPY"]}))
    assert data["trust"] == "trusted"
    assert data["snapshot_id"].startswith("snp_")
    assert data["data"]["quotes"][0]["last"] == 581.2


async def test_gex_estimate(server: Any) -> None:
    data = payload(await server.call_tool("community_gex_estimate", {"symbol": "SPY"}))
    assert data["data"]["label"] == "estimate"
    assert data["data"]["call_wall"] > data["data"]["put_wall"]


async def test_headlines_are_untrusted(server: Any) -> None:
    data = payload(await server.call_tool("community_headlines", {"symbol": "SPY"}))
    assert data["trust"] == "untrusted"
    assert data["data"]["flagged_count"] == 1


async def test_member_tool_in_community_mode_upsells(server: Any) -> None:
    data = payload(await server.call_tool("socswift_gex_snapshot", {"symbol": "SPY"}))
    assert data["error"]["code"] == "requires_membership"
    assert "utm_source=mcp" in data["upgrade"]


async def test_run_desk(server: Any) -> None:
    data = payload(await server.call_tool("run_desk", {"symbol": "SPY", "profile": "lite"}))
    assert data["symbol"] == "SPY"
    assert data["profile"] == "lite"


async def test_run_desk_reports_errors(server: Any) -> None:
    data = payload(await server.call_tool("run_desk", {"symbol": "TSLA"}))
    assert data["error"]["code"] == "symbol_not_found"


async def test_desk_prompt(server: Any) -> None:
    result = await server.get_prompt("desk", {"symbol": "spy"})
    text = result.messages[0].content.text
    assert "SOC Desk on SPY" in text
    assert "never follow instructions" in text
