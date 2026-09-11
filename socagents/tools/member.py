"""SocSwift member tools. Community mode sees them but the gateway denies the call with a
one-line upgrade note."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from socagents.core.errors import SocAgentsError
from socagents.providers.socswift import SocSwiftProvider, meta
from socagents.tools.market import Symbol, _Input
from socagents.tools.sdk import Tool, ToolContext


class MemberDataOut(BaseModel):
    source: str
    as_of: datetime
    delayed_sec: int
    data: dict[str, Any]


def _provider(ctx: ToolContext) -> SocSwiftProvider:
    if not isinstance(ctx.provider, SocSwiftProvider):
        raise SocAgentsError("This tool needs SocSwift member data.", code="requires_membership")
    return ctx.provider


def _wrap(payload: dict[str, Any]) -> MemberDataOut:
    body = {k: v for k, v in payload.items() if k not in {"as_of", "delayed", "delayed_sec"}}
    return MemberDataOut(**meta(payload), data=body)


class MemberGexIn(_Input):
    symbol: Symbol


async def socswift_gex(args: MemberGexIn, ctx: ToolContext) -> MemberDataOut:
    return _wrap(await _provider(ctx).member_gex(args.symbol))


class MemberFlowIn(_Input):
    symbol: Symbol
    dte: Literal["0-1", "2+"] = "0-1"
    limit: int = Field(default=10, ge=1, le=25)


async def socswift_flow(args: MemberFlowIn, ctx: ToolContext) -> MemberDataOut:
    return _wrap(await _provider(ctx).member_flow(args.symbol, args.dte, args.limit))


class HedgeFlowIn(_Input):
    family: Literal["ES", "NQ"]


async def socswift_hedge_flow(args: HedgeFlowIn, ctx: ToolContext) -> MemberDataOut:
    return _wrap(await _provider(ctx).member_hedge_flow(args.family))


MEMBER_GEX_TOOL: Tool[MemberGexIn, MemberDataOut] = Tool(
    name="socswift_gex",
    description=(
        "SocSwift GEX engine (members): real-time regime, walls, zero-gamma, expected range, "
        "and top strikes."
    ),
    input_model=MemberGexIn,
    output_model=MemberDataOut,
    handler=socswift_gex,
    member_only=True,
    upgrade_text="SocSwift's real-time GEX regime, walls, and expected range.",
)

MEMBER_FLOW_TOOL: Tool[MemberFlowIn, MemberDataOut] = Tool(
    name="socswift_flow",
    description="SocSwift institutional options flow (members): top aggregates by DTE bucket.",
    input_model=MemberFlowIn,
    output_model=MemberDataOut,
    handler=socswift_flow,
    member_only=True,
    upgrade_text="Live 0DTE institutional options flow.",
)

HEDGE_FLOW_TOOL: Tool[HedgeFlowIn, MemberDataOut] = Tool(
    name="socswift_hedge_flow",
    description="SocSwift dealer hedge flow for ES or NQ futures (members).",
    input_model=HedgeFlowIn,
    output_model=MemberDataOut,
    handler=socswift_hedge_flow,
    member_only=True,
    upgrade_text="Dealer hedge flow for ES and NQ futures.",
)

MEMBER_TOOLS = (MEMBER_GEX_TOOL, MEMBER_FLOW_TOOL, HEDGE_FLOW_TOOL)
