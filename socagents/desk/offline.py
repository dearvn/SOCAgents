"""Offline desk roles for the ``fixture/scripted`` model.

Deterministic templates over real tool results, so the full desk pipeline (tools, gateway,
snapshots, risk engine, verification, storage) runs in tests, CI, and demos without an LLM.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from typing import Any

from socagents.core.ids import new_id
from socagents.desk.roles import parse_context
from socagents.model_gateway.types import Message, ModelResponse, ToolCall, ToolSpec

Result = tuple[dict[str, Any], str]  # (tool data, snapshot id)
Composer = Callable[[str, dict[str, Any], dict[str, list[Result]]], dict[str, Any]]

_ANALYST_PLANS: dict[str, list[tuple[str, str]]] = {
    "dealer_positioning": [("socswift_gex", "symbol"), ("get_gex_estimate", "symbol")],
    "flow": [("socswift_flow", "symbol"), ("get_flow_estimate", "symbol")],
    "technical": [("get_technicals", "symbol")],
    "event_news": [("get_headlines", "symbol"), ("get_event_calendar", "")],
    "futures_hedge": [("socswift_hedge_flow", "family")],
}


def respond(role: str, messages: list[Message], tools: list[ToolSpec], model: str) -> ModelResponse:
    ctx = parse_context(next((m.content for m in messages if m.role == "user"), ""))
    available = {t.name for t in tools}
    if not any(m.role == "tool" for m in messages):
        calls = _plan(role, ctx, available)
        if calls:
            return ModelResponse(text="", tool_calls=calls, stop_reason="tool_use", model=model)
    composer = _COMPOSERS.get(role)
    body = composer(role, ctx, _tool_results(messages)) if composer else {}
    return ModelResponse(text=json.dumps(body), stop_reason="end_turn", model=model)


def _tool_results(messages: list[Message]) -> dict[str, list[Result]]:
    results: dict[str, list[Result]] = {}
    for m in messages:
        if m.role != "tool" or m.is_error:
            continue
        try:
            payload = json.loads(m.content)
        except ValueError:
            continue
        if "data" in payload:
            results.setdefault(m.name or "", []).append(
                (payload["data"], payload.get("snapshot_id") or "")
            )
    return results


def _first(results: dict[str, list[Result]], *names: str) -> Result | None:
    for name in names:
        if results.get(name):
            return results[name][0]
    return None


def _plan(role: str, ctx: dict[str, Any], available: set[str]) -> list[ToolCall]:
    symbol = str(ctx.get("symbol", ""))
    if role == "strategist":
        direction = _direction(ctx.get("reports", []))
        spot = ctx.get("spot")
        if direction is None or not isinstance(spot, int | float):
            return []
        strike = math.ceil(spot) if direction == "call" else math.floor(spot)
        args: dict[str, Any] = {"symbol": symbol, "right": direction, "strike": float(strike)}
        return [ToolCall(id=new_id("call"), name="get_option_quote", arguments=args)]
    calls: list[ToolCall] = []
    for name, arg in _ANALYST_PLANS.get(role, []):
        if name not in available:
            continue
        arguments: dict[str, Any] = {}
        if arg == "symbol":
            arguments = {"symbol": symbol}
        elif arg == "family":
            arguments = {"family": "NQ" if symbol in {"QQQ", "NDX"} else "ES"}
        calls.append(ToolCall(id=new_id("call"), name=name, arguments=arguments))
        if role in {"dealer_positioning", "flow"}:
            break  # member tool if available, else the Community estimate
    return calls


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:g}"


def _level(price: float | None, kind: str, snap: str) -> list[dict[str, Any]]:
    return [] if price is None else [{"price": price, "kind": kind, "evidence": [snap]}]


def _report(
    stance: str,
    confidence: float,
    summary: str,
    *,
    levels: list[dict[str, Any]] | None = None,
    signals: list[dict[str, Any]] | None = None,
    evidence: list[str] | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "stance": stance,
        "confidence": confidence,
        "summary": summary,
        "key_levels": levels or [],
        "signals": signals or [],
        "evidence": [e for e in (evidence or []) if e],
        "notes": notes or [],
    }


def _dealer(role: str, ctx: dict[str, Any], results: dict[str, list[Result]]) -> dict[str, Any]:
    found = _first(results, "get_gex_estimate")
    if found is None:
        return _report("neutral", 0.2, "No dealer positioning data was available.")
    d, snap = found
    spot, zero, regime = d["spot"], d.get("zero_gamma"), d["regime"]
    above_flip = zero is None or spot >= zero
    if regime == "positive" and above_flip:
        stance, read = "neutral", "Positive gamma (estimate): dealer hedging tends to dampen moves"
    elif regime == "negative" and not above_flip:
        stance, read = "bearish", "Negative gamma (estimate): dealer hedging can extend moves"
    else:
        side = "above" if above_flip else "below"
        stance, read = (
            "neutral",
            f"{regime.capitalize()} net gamma (estimate), spot {side} the flip",
        )
    summary = (
        f"{read}. Spot {spot:.2f}; call wall {_fmt(d.get('call_wall'))}, put wall "
        f"{_fmt(d.get('put_wall'))}, estimated zero-gamma {_fmt(zero)}."
    )
    levels = (
        _level(d.get("call_wall"), "call wall (estimate)", snap)
        + _level(d.get("put_wall"), "put wall (estimate)", snap)
        + _level(zero, "zero-gamma flip (estimate)", snap)
    )
    signals = [
        {"name": "regime", "value": regime, "evidence": [snap]},
        {"name": "net_gex_usd_per_1pct", "value": f"{d['net_gex']:,.0f}", "evidence": [snap]},
    ]
    return _report(stance, 0.55, summary, levels=levels, signals=signals, evidence=[snap])


def _flow(role: str, ctx: dict[str, Any], results: dict[str, list[Result]]) -> dict[str, Any]:
    found = _first(results, "get_flow_estimate")
    if found is None:
        return _report("neutral", 0.2, "No options flow data was available.")
    d, snap = found
    ratio = d.get("call_put_premium_ratio")
    stance = {"call-heavy": "bullish", "put-heavy": "bearish"}.get(d["bias"], "neutral")
    summary = (
        f"Call premium ${d['call_premium_usd'] / 1e6:,.1f}M vs put premium "
        f"${d['put_premium_usd'] / 1e6:,.1f}M"
        + (f" ({ratio:.2f}x)" if ratio is not None else "")
        + f"; {d['bias']}. Public chain proxy: buyers and sellers cannot be told apart."
    )
    levels: list[dict[str, Any]] = []
    for item in d.get("unusual", [])[:2]:
        levels += _level(item["strike"], f"unusual {item['right']} volume", snap)
    signals = [{"name": "bias", "value": d["bias"], "evidence": [snap]}]
    return _report(stance, 0.45, summary, levels=levels, signals=signals, evidence=[snap])


def _technical(role: str, ctx: dict[str, Any], results: dict[str, list[Result]]) -> dict[str, Any]:
    found = _first(results, "get_technicals")
    if found is None:
        return _report("neutral", 0.2, "No bars were available.")
    d, snap = found
    stance = {"up": "bullish", "down": "bearish"}.get(d["trend"], "neutral")
    rsi = d.get("rsi14")
    summary = (
        f"Last {d['last']:.2f} is {'above' if d['above_vwap'] else 'below'} session VWAP "
        f"{d['vwap']:.2f}; EMA 9 {d['ema_fast']:.2f} vs EMA 21 {d['ema_slow']:.2f}"
        + (f"; RSI {rsi:.0f}" if rsi is not None else "")
        + f"; trend {d['trend']}."
    )
    levels = (
        _level(d["vwap"], "session VWAP", snap)
        + _level(d["session_high"], "session high", snap)
        + _level(d["session_low"], "session low", snap)
    )
    signals = [{"name": "trend", "value": d["trend"], "evidence": [snap]}]
    return _report(
        stance,
        0.55 if stance != "neutral" else 0.4,
        summary,
        levels=levels,
        signals=signals,
        evidence=[snap],
    )


def _events(role: str, ctx: dict[str, Any], results: dict[str, list[Result]]) -> dict[str, Any]:
    events = _first(results, "get_event_calendar")
    headlines = _first(results, "get_headlines")
    evidence: list[str] = []
    parts: list[str] = []
    notes: list[str] = []
    if events is not None:
        d, snap = events
        evidence.append(snap)
        high = [e["name"] for e in d.get("events", []) if e.get("importance") == "high"]
        parts.append(
            f"High-importance events in the next 48h: {', '.join(high)}."
            if high
            else "No high-importance events scheduled in the next 48h."
        )
    if headlines is not None:
        d, snap = headlines
        evidence.append(snap)
        count = len(d.get("headlines", []))
        flagged = int(d.get("flagged_count", 0))
        parts.append(f"{count} headlines reviewed as untrusted data.")
        if flagged:
            note = f"{flagged} headline(s) looked like instructions and were ignored."
            parts.append(note)
            notes.append(note)
    if not parts:
        return _report("neutral", 0.2, "No event or headline data was available.")
    return _report("neutral", 0.3, " ".join(parts), evidence=evidence, notes=notes)


def _hedge(role: str, ctx: dict[str, Any], results: dict[str, list[Result]]) -> dict[str, Any]:
    found = _first(results, "socswift_hedge_flow")
    if found is None:
        return _report("neutral", 0.2, "No dealer hedge flow data was available.")
    return _report(
        "neutral",
        0.4,
        "Dealer hedge flow received; no strong directional pressure.",
        evidence=[found[1]],
    )


def _direction(reports: list[dict[str, Any]]) -> str | None:
    score = sum(
        r.get("confidence", 0) * {"bullish": 1, "bearish": -1}.get(r.get("stance", ""), 0)
        for r in reports
    )
    if abs(score) < 0.3:
        return None
    return "call" if score > 0 else "put"


def _levels(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [lvl for r in reports for lvl in r.get("key_levels", [])]


def _researcher(role: str, ctx: dict[str, Any], results: dict[str, list[Result]]) -> dict[str, Any]:
    reports = ctx.get("reports", [])
    want = "bullish" if role == "bull" else "bearish"
    support = [r for r in reports if r.get("stance") == want]
    spot = ctx.get("spot") or 0
    points: list[str] = []
    evidence: list[str] = []
    if support:
        for r in support[:2]:
            points.append(f"{r['role'].replace('_', ' ')}: {r['summary']}")
            evidence += r.get("evidence", [])[:1]
    else:
        levels = _levels(reports)
        below = [lvl for lvl in levels if lvl["price"] < spot]
        above = [lvl for lvl in levels if lvl["price"] > spot]
        if role == "bull" and below:
            best = max(below, key=lambda lvl: lvl["price"])
            points.append(
                f"No analyst is outright bullish, but price holds above the "
                f"{best['kind']} at {best['price']:g}."
            )
            evidence += best["evidence"][:1]
        elif role == "bear" and above:
            best = min(above, key=lambda lvl: lvl["price"])
            points.append(
                f"No analyst is outright bearish, but the {best['kind']} at "
                f"{best['price']:g} caps upside."
            )
            evidence += best["evidence"][:1]
        else:
            points.append(f"The {want} case is weak: no analyst report supports it.")
    rebuttal = [t for t in ctx.get("debate", []) if t.get("side") != role]
    if rebuttal:
        points.append(f"Against the {rebuttal[-1]['side']}: that case depends on levels holding.")
    return {"argument": " ".join(points), "evidence": list(dict.fromkeys(evidence))}


def _strategist(role: str, ctx: dict[str, Any], results: dict[str, list[Result]]) -> dict[str, Any]:
    reports = ctx.get("reports", [])
    direction = _direction(reports)
    if direction is None:
        return {
            "ideas": [],
            "no_trade_reason": "Analysts are split or neutral; no directional edge.",
        }
    found = _first(results, "get_option_quote")
    if found is None:
        return {"ideas": [], "no_trade_reason": "No option quote was available."}
    q, snap = found
    mid = float(q["mid"])
    if mid < 0.05:
        return {"ideas": [], "no_trade_reason": "The premium is too small to define risk."}
    spot = float(ctx.get("spot") or q["underlying_price"])
    levels = _levels(reports)
    above = sorted(lvl["price"] for lvl in levels if lvl["price"] > spot)
    below = sorted((lvl["price"] for lvl in levels if lvl["price"] < spot), reverse=True)
    entry: dict[str, Any] = {"condition": "at_market", "trigger_price": None}
    if direction == "call":
        if above and above[0] - spot <= spot * 0.01:
            entry = {"condition": "break_out", "trigger_price": above[0]}
        invalidation = f"5m close below {below[0]:g}" if below else "5m close below session VWAP"
    else:
        if below and spot - below[0] <= spot * 0.01:
            entry = {"condition": "break_down", "trigger_price": below[0]}
        invalidation = f"5m close above {above[0]:g}" if above else "5m close above session VWAP"
    supporting = [
        r for r in reports if r.get("stance") == ("bullish" if direction == "call" else "bearish")
    ]
    evidence = [snap] + [e for r in supporting for e in r.get("evidence", [])[:1]]
    idea = {
        "structure": f"long_{direction}",
        "symbol": ctx.get("symbol"),
        "instrument": "option",
        "action": "buy",
        "right": direction,
        "strike": q["strike"],
        "expiration": q["expiration"],
        "qty": 1,
        "legs": 1,
        "entry": entry,
        "price_basis": "premium",
        "est_entry_premium": round(mid, 2),
        "stop": round(mid * 0.5, 2),
        "target": round(mid * 2.0, 2),
        "invalidation": invalidation,
        "rationale": (
            f"Analyst majority leans {'bullish' if direction == 'call' else 'bearish'}; a long "
            f"{direction} keeps risk defined to the premium."
        ),
        "evidence": list(dict.fromkeys(evidence)),
    }
    return {"ideas": [idea], "no_trade_reason": None}


def _risk_officer(
    role: str, ctx: dict[str, Any], results: dict[str, list[Result]]
) -> dict[str, Any]:
    critiques = []
    for i, check in enumerate(ctx.get("risk_checks", [])):
        if check.get("mode") == "educational":
            text = (
                "Educational check only: the data is delayed, so this idea is not "
                "risk-checked for execution. Treat the levels as reference, not signals."
            )
        elif check.get("decision") == "allow":
            text = "Deterministic checks pass. Respect the invalidation level and the size."
        else:
            text = "Rejected by the risk engine: " + "; ".join(
                r["message"] for r in check.get("reasons", [])
            )
        critiques.append({"index": i, "critique": text})
    return {"critiques": critiques}


def _lead(role: str, ctx: dict[str, Any], results: dict[str, list[Result]]) -> dict[str, Any]:
    reports = ctx.get("reports", [])
    by_role = {r["role"]: r for r in reports}
    dealer = by_role.get("dealer_positioning", {})
    regime_value = next(
        (s["value"] for s in dealer.get("signals", []) if s.get("name") == "regime"), None
    )
    if regime_value == "positive":
        regime = "positive gamma (estimate), mean-reverting"
    elif regime_value == "negative":
        regime = "negative gamma (estimate), trend-prone"
    else:
        regime = "unclear (no dealer positioning data)"

    levels: list[dict[str, Any]] = []
    for name in ("dealer_positioning", "technical", "flow"):
        for lvl in by_role.get(name, {}).get("key_levels", []):
            if all(abs(lvl["price"] - k["price"]) > lvl["price"] * 0.001 for k in levels):
                levels.append(lvl)
    levels = levels[:6]

    def find(word: str) -> dict[str, Any] | None:
        return next((lvl for lvl in levels if word in lvl["kind"]), None)

    call_wall, put_wall = find("call wall"), find("put wall")
    flip, high = find("zero-gamma"), find("session high")
    scenarios = []
    if put_wall and call_wall:
        scenarios.append(
            {
                "name": "base",
                "condition": f"holds between {put_wall['price']:g} and {call_wall['price']:g}",
                "path": "range trade while dealer hedging dampens moves",
                "evidence": put_wall["evidence"][:1] + call_wall["evidence"][:1],
            }
        )
    upper = call_wall or high
    if upper:
        scenarios.append(
            {
                "name": "bull",
                "condition": f"sustained move above {upper['price']:g}",
                "path": "the level gives way and upside can extend",
                "evidence": upper["evidence"][:1],
            }
        )
    lower = flip or put_wall
    if lower:
        target = f" toward {put_wall['price']:g}" if put_wall and put_wall is not lower else ""
        scenarios.append(
            {
                "name": "bear",
                "condition": f"5m close below {lower['price']:g}",
                "path": f"below the flip, hedging can extend moves{target}",
                "evidence": lower["evidence"][:1],
            }
        )

    summary = " ".join(r["summary"].split(". ")[0].rstrip(".") + "." for r in reports[:3])
    debate = ctx.get("debate", [])
    majority = _direction(reports)
    dissent_side = "bear" if majority == "call" else "bull" if majority == "put" else None
    dissent_turns = [t for t in debate if t.get("side") == dissent_side]
    if dissent_side is not None and dissent_turns:
        dissent = f"{dissent_side.capitalize()} case: {dissent_turns[-1]['argument']}"
    elif debate:
        dissent = f"{debate[-1]['side'].capitalize()} case: {debate[-1]['argument']}"
    else:
        dissent = "No debate in this profile."
    return {
        "regime": regime,
        "summary": summary,
        "key_levels": levels,
        "scenarios": scenarios,
        "dissent": dissent,
    }


_COMPOSERS: dict[str, Composer] = {
    "dealer_positioning": _dealer,
    "flow": _flow,
    "technical": _technical,
    "event_news": _events,
    "futures_hedge": _hedge,
    "bull": _researcher,
    "bear": _researcher,
    "strategist": _strategist,
    "risk_officer": _risk_officer,
    "desk_lead": _lead,
}
