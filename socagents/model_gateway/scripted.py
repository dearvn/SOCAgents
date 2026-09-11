"""Offline scripted model for fixture runs, tests, and CI.

It is not an LLM. It requests the market data tools for the symbols in the prompt, then
assembles a templated answer from the tool results, citing snapshot ids.
"""

from __future__ import annotations

import json
from typing import Any

from socagents.core.errors import ConfigError
from socagents.core.ids import new_id
from socagents.model_gateway.types import Message, ModelResponse, ToolCall, ToolSpec, Usage

SYMBOLS_PREFIX = "Symbols:"
ROLE_MARKER = "Role:"  # matches socagents.desk.roles.ROLE_MARKER


class ScriptedModel:
    provider = "fixture"

    def __init__(self, model: str = "scripted") -> None:
        if model != "scripted":
            raise ConfigError("The fixture model provider has one model: fixture/scripted.")
        self.model = model

    async def aclose(self) -> None:
        return None

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int,
    ) -> ModelResponse:
        role = _role_from(system)
        if role is not None:
            from socagents.desk.offline import respond

            return respond(role, messages, tools, self.model)
        if not any(m.role == "tool" for m in messages):
            calls = _plan_calls(_symbols_from(messages), {t.name for t in tools})
            if calls:
                return ModelResponse(
                    text="",
                    tool_calls=calls,
                    usage=Usage(),
                    stop_reason="tool_use",
                    model=self.model,
                )
        return ModelResponse(
            text=compose_answer(messages), usage=Usage(), stop_reason="end_turn", model=self.model
        )


def _role_from(system: str) -> str | None:
    for line in system.splitlines():
        if line.startswith(ROLE_MARKER):
            return line.removeprefix(ROLE_MARKER).strip() or None
    return None


def _symbols_from(messages: list[Message]) -> list[str]:
    for m in messages:
        if m.role != "user":
            continue
        for line in m.content.splitlines():
            if line.startswith(SYMBOLS_PREFIX):
                raw = line.removeprefix(SYMBOLS_PREFIX).split(",")
                return [s.strip().upper() for s in raw if s.strip()]
    return []


def _plan_calls(symbols: list[str], available: set[str]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    if symbols and "get_quote" in available:
        calls.append(ToolCall(id=new_id("call"), name="get_quote", arguments={"symbols": symbols}))
    for symbol in symbols:
        if "get_option_chain_summary" in available:
            calls.append(
                ToolCall(
                    id=new_id("call"), name="get_option_chain_summary", arguments={"symbol": symbol}
                )
            )
        if "get_bars" in available:
            calls.append(ToolCall(id=new_id("call"), name="get_bars", arguments={"symbol": symbol}))
    return calls


def _num(value: float) -> str:
    return f"{value:,.2f}"


def _strike(value: float) -> str:
    return f"{value:g}"


def _delay(seconds: int) -> str:
    return "real-time" if seconds <= 0 else f"delayed {seconds // 60}m"


def compose_answer(messages: list[Message]) -> str:
    quotes: dict[str, tuple[dict[str, Any], str, int]] = {}
    chains: dict[str, tuple[dict[str, Any], str]] = {}
    bars: dict[str, tuple[dict[str, Any], str]] = {}
    errors: list[str] = []

    for m in messages:
        if m.role != "tool":
            continue
        try:
            payload = json.loads(m.content)
        except json.JSONDecodeError:
            errors.append(f"{m.name}: unreadable result")
            continue
        if "error" in payload:
            errors.append(f"{m.name}: {payload['error'].get('code')}")
            continue
        data = payload.get("data", {})
        snap = payload.get("snapshot_id", "")
        if m.name == "get_quote":
            for q in data.get("quotes", []):
                quotes[q["symbol"]] = (q, snap, int(data.get("delayed_sec", 0)))
        elif m.name == "get_option_chain_summary":
            chains[data["symbol"]] = (data, snap)
        elif m.name == "get_bars":
            bars[data["symbol"]] = (data, snap)

    symbols = list(dict.fromkeys([*quotes, *chains, *bars]))
    lines: list[str] = []
    for symbol in symbols:
        if symbol in quotes:
            q, snap, delayed = quotes[symbol]
            lines.append(
                f"{symbol} {_num(q['last'])}, {q['change_pct']:+.2f}% on the day, "
                f"{_delay(delayed)} [{snap}]."
            )
        if symbol in chains:
            c, snap = chains[symbol]
            calls = " and ".join(_strike(s["strike"]) for s in c["top_call_oi"][:2])
            puts = " and ".join(_strike(s["strike"]) for s in c["top_put_oi"][:2])
            ratio = c.get("put_call_oi_ratio")
            ratio_text = f"; put/call OI ratio {ratio:.2f}" if ratio is not None else ""
            lines.append(
                f"Open interest ({c['expiration']} expiry): largest call OI at {calls}, "
                f"largest put OI at {puts}{ratio_text} [{snap}]."
            )
        if symbol in bars:
            b, snap = bars[symbol]
            side = "above" if b["last"] >= b["vwap"] else "below"
            lines.append(
                f"Price action ({b['interval']} bars): last {_num(b['last'])} is {side} session "
                f"VWAP {_num(b['vwap'])}; session range {_num(b['session_low'])}"
                f"–{_num(b['session_high'])} [{snap}]."
            )
        if symbol in quotes and symbol in chains:
            last = quotes[symbol][0]["last"]
            c = chains[symbol][0]
            # top_*_oi lists are sorted by open interest, so the first match is the largest.
            above = [s["strike"] for s in c["top_call_oi"] if s["strike"] > last]
            below = [s["strike"] for s in c["top_put_oi"] if s["strike"] < last]
            if above and below:
                lines.append(
                    f"Read: price sits between the largest put OI below at {_strike(below[0])} "
                    f"and the largest call OI above at {_strike(above[0])}. Large open interest "
                    "often acts as a reference level, not a guarantee."
                )
        lines.append("")

    if errors:
        lines.append("Missing data: " + "; ".join(errors) + ".")
    if not symbols and not errors:
        lines.append("No market data was requested, so there is nothing to report.")
    lines.append(
        "Offline scripted model: this answer is assembled from tool data by a template, "
        "not written by an LLM. Not investment advice."
    )
    return "\n".join(lines).strip()
