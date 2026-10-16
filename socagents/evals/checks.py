"""Deterministic checks over a Strategist answer.

Every check returns pass or fail with a one-line reason. A check that looks at ideas fails
when a case expects an idea and the answer has none, so an empty answer earns nothing on an
idea case. On a no-trade case the same check passes with nothing to look at; the
``expectation`` check is what fails an idea there.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import time
from typing import Any

from socagents.core.timeutil import ET
from socagents.desk.graph import MAX_IDEAS
from socagents.desk.models import DeskIdea, StrategistOutput
from socagents.desk.verify import matches
from socagents.evals.cases import EvalCase

# A trigger "on a level" must sit within a few cents of it. ``verify.matches`` allows 0.25%
# (about $1.50 on SPY), which is too loose to tell one level from the next (TODO B7).
LEVEL_ABS_TOL = 0.02
LEVEL_REL_TOL = 0.0001


def on_level(price: float, levels: list[float]) -> bool:
    tolerance = max(LEVEL_ABS_TOL, abs(price) * LEVEL_REL_TOL)
    return any(abs(level - price) <= tolerance for level in levels)


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


CheckFn = Callable[[StrategistOutput, EvalCase, dict[str, Any]], CheckResult]
_IdeaRule = Callable[[DeskIdea, EvalCase, dict[str, Any]], str | None]

CHECKS: dict[str, CheckFn] = {}


def _register(name: str) -> Callable[[CheckFn], CheckFn]:
    def wrap(fn: CheckFn) -> CheckFn:
        CHECKS[name] = fn
        return fn

    return wrap


def _ideas(output: StrategistOutput) -> list[DeskIdea]:
    return output.ideas[:MAX_IDEAS]


def _per_idea(name: str, rule: _IdeaRule) -> CheckFn:
    """A check that applies ``rule`` to each idea; the rule returns a failure reason or None."""

    def check(output: StrategistOutput, case: EvalCase, params: dict[str, Any]) -> CheckResult:
        ideas = _ideas(output)
        if not ideas:
            if case.expect == "idea":
                return CheckResult(name, False, "no idea to check")
            return CheckResult(name, True, "no idea, as expected")
        for idx, idea in enumerate(ideas):
            problem = rule(idea, case, params)
            if problem:
                return CheckResult(name, False, f"idea {idx + 1} ({idea.contract}): {problem}")
        return CheckResult(name, True)

    CHECKS[name] = check
    return check


def _text(idea: DeskIdea) -> str:
    return f"{idea.rationale}\n{idea.invalidation}"


def _is_long_premium(idea: DeskIdea) -> bool:
    return idea.instrument == "option" and idea.action == "buy" and idea.price_basis == "premium"


# common


@_register("expectation")
def _expectation(output: StrategistOutput, case: EvalCase, params: dict[str, Any]) -> CheckResult:
    ideas = _ideas(output)
    if case.expect == "idea":
        if ideas:
            return CheckResult("expectation", True)
        return CheckResult("expectation", False, "expected an idea, got no trade")
    if ideas:
        return CheckResult("expectation", False, f"expected no trade, got {len(ideas)} idea(s)")
    if not (output.no_trade_reason or "").strip():
        return CheckResult("expectation", False, "no trade, but no reason given")
    return CheckResult("expectation", True)


def _evidence_resolves(idea: DeskIdea, case: EvalCase, params: dict[str, Any]) -> str | None:
    known = case.snapshot_ids()
    if not idea.evidence:
        return "cites no snapshot"
    unknown = [e for e in idea.evidence if e not in known]
    if unknown:
        return f"cites unknown snapshot(s) {', '.join(unknown[:3])}"
    return None


def _quote_matches(idea: DeskIdea, case: EvalCase, params: dict[str, Any]) -> str | None:
    if idea.instrument != "option":
        return None
    expiration = idea.expiration.isoformat() if idea.expiration else None
    for sid in idea.evidence:
        quote = case.quote_for(sid)
        if (
            quote is not None
            and quote.right == idea.right
            and quote.strike == idea.strike
            and quote.expiration.isoformat() == expiration
        ):
            if matches(idea.est_entry_premium, [quote.mid]):
                return None
            return f"premium {idea.est_entry_premium:g} is not the quote's mid {quote.mid:g}"
    return "cites no quote for its own contract"


def _qty_one(idea: DeskIdea, case: EvalCase, params: dict[str, Any]) -> str | None:
    return None if idea.qty == 1 else f"qty is {idea.qty}"


def _complete_plan(idea: DeskIdea, case: EvalCase, params: dict[str, Any]) -> str | None:
    missing = [
        name
        for name, value in (
            ("stop", idea.stop),
            ("target", idea.target),
            ("invalidation", idea.invalidation.strip() or None),
        )
        if value is None
    ]
    return f"missing {', '.join(missing)}" if missing else None


def _trigger_on_level(idea: DeskIdea, case: EvalCase, params: dict[str, Any]) -> str | None:
    if idea.entry.condition == "at_market" or idea.entry.trigger_price is None:
        return "enters at market, not on a level"
    if not on_level(idea.entry.trigger_price, case.level_prices()):
        return f"trigger {idea.entry.trigger_price:g} is not a level from the reports"
    return None


_per_idea("evidence_resolves", _evidence_resolves)
_per_idea("quote_matches", _quote_matches)
_per_idea("qty_one", _qty_one)
_per_idea("complete_plan", _complete_plan)
_per_idea("trigger_on_level", _trigger_on_level)


# zero-dte-long-premium


def _stop_within_half(idea: DeskIdea, case: EvalCase, params: dict[str, Any]) -> str | None:
    if not _is_long_premium(idea):
        return None
    if idea.stop is None:
        return "no stop"
    floor = idea.est_entry_premium * float(params.get("max_loss_fraction", 0.5))
    if idea.stop < floor - 1e-9:
        return f"stop {idea.stop:g} risks more than half of {idea.est_entry_premium:g}"
    if idea.stop >= idea.est_entry_premium:
        return f"stop {idea.stop:g} is not below the entry {idea.est_entry_premium:g}"
    return None


def _late_entry(idea: DeskIdea, case: EvalCase, params: dict[str, Any]) -> str | None:
    hh, mm = (int(p) for p in str(params.get("after_et", "14:30")).split(":"))
    if case.as_of.astimezone(ET).time() < time(hh, mm):
        return None
    trigger = idea.entry.trigger_price
    if trigger is None:
        return "late-session idea with no trigger"
    distance = abs(trigger - case.spot) / case.spot * 100
    max_pct = float(params.get("max_trigger_pct", 0.15))
    if distance > max_pct:
        return f"late-session trigger is {distance:.2f}% from spot (max {max_pct:g}%)"
    return None


def _near_money(idea: DeskIdea, case: EvalCase, params: dict[str, Any]) -> str | None:
    if idea.instrument != "option" or idea.strike is None:
        return None
    distance = abs(idea.strike - case.spot) / case.spot * 100
    max_pct = float(params.get("max_pct", 0.5))
    if distance > max_pct:
        return f"strike {idea.strike:g} is {distance:.2f}% from spot (max {max_pct:g}%)"
    return None


_per_idea("stop_within_half_premium", _stop_within_half)
_per_idea("late_entry", _late_entry)
_per_idea("near_money", _near_money)


# gamma-regime-playbook


def _fact(case: EvalCase, name: str) -> float:
    value = case.facts.get(name)
    if not isinstance(value, int | float):
        raise ValueError(f"case {case.id} needs a numeric fact {name!r}")
    return float(value)


def _waits_beyond_flip(idea: DeskIdea, case: EvalCase, params: dict[str, Any]) -> str | None:
    flip = _fact(case, "flip")
    trigger = idea.entry.trigger_price
    if idea.entry.condition == "at_market" or trigger is None:
        return "does not wait for a move beyond the flip"
    if idea.right == "call" and (idea.entry.condition != "break_out" or trigger < flip):
        return f"call trigger {trigger:g} is not a break above the flip {flip:g}"
    if idea.right == "put" and (idea.entry.condition != "break_down" or trigger > flip):
        return f"put trigger {trigger:g} is not a break below the flip {flip:g}"
    return None


def _break_matches_direction(idea: DeskIdea, case: EvalCase, params: dict[str, Any]) -> str | None:
    wanted = {"call": "break_out", "put": "break_down"}.get(idea.right or "")
    if wanted and idea.entry.condition != wanted:
        return f"a {idea.right} in negative gamma should trigger on a {wanted}"
    return None


def _wall_rejection(idea: DeskIdea, case: EvalCase, params: dict[str, Any]) -> str | None:
    """Positive gamma: a call off the put wall or a put off the call wall, near that wall."""
    trigger = idea.entry.trigger_price
    if trigger is None:
        return "no trigger near a wall"
    wall = _fact(case, "put_wall" if idea.right == "call" else "call_wall")
    distance = abs(trigger - wall) / wall * 100
    max_pct = float(params.get("max_pct", 0.25))
    if distance > max_pct:
        side = "put" if idea.right == "call" else "call"
        return f"trigger {trigger:g} is {distance:.2f}% from the {side} wall {wall:g}"
    return None


_per_idea("waits_beyond_flip", _waits_beyond_flip)
_per_idea("break_matches_direction", _break_matches_direction)
_per_idea("wall_rejection", _wall_rejection)


# event-risk-checklist

_IV_RE = re.compile(r"\bIV\b|implied vol|volatility|vol crush", re.IGNORECASE)
_WAIT_RE = re.compile(
    r"5[- ]?min|five[- ]minute|after (the )?(release|report|print|data|number)", re.IGNORECASE
)


def _names_event(idea: DeskIdea, case: EvalCase, params: dict[str, Any]) -> str | None:
    name = str(params.get("event") or case.facts.get("event") or "")
    if not name:
        raise ValueError(f"case {case.id} needs an event name")
    return None if name.lower() in _text(idea).lower() else f"does not name the {name} release"


def _mentions_iv(idea: DeskIdea, case: EvalCase, params: dict[str, Any]) -> str | None:
    if not _is_long_premium(idea):
        return None
    return None if _IV_RE.search(_text(idea)) else "does not mention the volatility drop"


def _waits_for_release(idea: DeskIdea, case: EvalCase, params: dict[str, Any]) -> str | None:
    return None if _WAIT_RE.search(_text(idea)) else "does not wait for a close after the release"


_per_idea("names_event", _names_event)
_per_idea("mentions_iv_drop", _mentions_iv)
_per_idea("waits_for_release", _waits_for_release)


def run_checks(output: StrategistOutput, case: EvalCase) -> list[CheckResult]:
    results: list[CheckResult] = []
    for spec in case.checks:
        fn = CHECKS.get(spec.name)
        if fn is None:
            results.append(CheckResult(spec.name, False, "unknown check"))
            continue
        results.append(fn(output, case, spec.params))
    return results


def score(results: list[CheckResult]) -> float:
    """Partial credit: the share of checks passed."""
    return sum(r.passed for r in results) / len(results) if results else 0.0
