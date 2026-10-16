"""Run eval cases: the Strategist role, its production prompt, and quotes served from the case."""

from __future__ import annotations

import asyncio
import json
import statistics
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from socagents.core.errors import ModelError, SocAgentsError
from socagents.desk.models import StrategistOutput
from socagents.desk.roles import ROLES, system_prompt, user_message
from socagents.desk.structured import parse_or_repair
from socagents.evals.cases import CaseQuote, EvalCase
from socagents.evals.checks import CheckResult, run_checks, score
from socagents.evals.judge import Judge, JudgeResult
from socagents.model_gateway.types import (
    TRUNCATED_STOP_REASONS,
    Message,
    ModelProvider,
    ToolCall,
    Usage,
)
from socagents.skills import Skill
from socagents.tools.research import OPTION_QUOTE_TOOL, OptionQuoteIn

ROLE = ROLES["strategist"]
CASE_SOURCE = "eval-case"


@dataclass
class TrialResult:
    case_id: str
    trial: int
    with_skill: bool
    output: StrategistOutput | None = None
    error: str | None = None
    checks: list[CheckResult] = field(default_factory=list)
    judge: JudgeResult | None = None
    usage: Usage = field(default_factory=Usage)

    @property
    def score(self) -> float:
        return score(self.checks) if self.output is not None else 0.0

    @property
    def passed(self) -> bool:
        return self.output is not None and all(c.passed for c in self.checks)


@dataclass
class CaseSummary:
    case_id: str
    skill: str
    expect: str
    with_skill: bool
    trials: list[TrialResult]

    @property
    def pass_rate(self) -> float:
        return sum(t.passed for t in self.trials) / len(self.trials)

    @property
    def mean_score(self) -> float:
        return statistics.fmean(t.score for t in self.trials)

    @property
    def score_sd(self) -> float:
        return statistics.pstdev(t.score for t in self.trials) if len(self.trials) > 1 else 0.0

    @property
    def judge_mean(self) -> float | None:
        scores = [t.judge.score for t in self.trials if t.judge is not None]
        return statistics.fmean(scores) if scores else None

    def failed_checks(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for trial in self.trials:
            if trial.error:
                counts[trial.error] = counts.get(trial.error, 0) + 1
            for check in trial.checks:
                if not check.passed:
                    counts[check.name] = counts.get(check.name, 0) + 1
        return counts


def _quote_payload(case: EvalCase, quote: CaseQuote, requested: float) -> dict[str, Any]:
    return {
        "source": CASE_SOURCE,
        "as_of": case.as_of.isoformat(),
        "delayed_sec": 0,
        "symbol": case.symbol,
        "underlying_price": case.spot,
        "expiration": quote.expiration.isoformat(),
        "strike": quote.strike,
        "requested_strike": requested,
        "exact_strike": quote.strike == requested,
        "right": quote.right,
        "bid": quote.bid,
        "ask": quote.ask,
        "mid": quote.mid,
        "last": None,
        "iv": quote.iv,
        "delta": quote.delta,
        "gamma": None,
        "volume": quote.volume,
        "open_interest": quote.open_interest,
        "price_basis": "premium",
    }


def serve_tool(case: EvalCase, call: ToolCall) -> tuple[str, bool]:
    """Answer one tool call from the case. Returns (message content, is_error)."""
    if call.name != OPTION_QUOTE_TOOL.name:
        return json.dumps({"error": {"code": "unknown_tool", "message": call.name}}), True
    try:
        args = OptionQuoteIn.model_validate(call.arguments)
    except ValidationError as exc:
        return json.dumps({"error": {"code": "invalid_arguments", "message": str(exc)[:300]}}), True
    if args.symbol != case.symbol:
        message = f"No data for {args.symbol} in this case."
        return json.dumps({"error": {"code": "symbol_not_found", "message": message}}), True
    expirations = sorted({q.expiration for q in case.quotes})
    expiration = args.expiration or (expirations[0] if expirations else None)
    candidates = [q for q in case.quotes if q.expiration == expiration and q.right == args.right]
    if not candidates:
        message = f"No {args.right}s expire on {expiration}."
        return json.dumps({"error": {"code": "expiration_not_found", "message": message}}), True
    quote = min(candidates, key=lambda q: abs(q.strike - args.strike))
    content = {
        "trust": OPTION_QUOTE_TOOL.trust.value,
        "snapshot_id": quote.snapshot_id,
        "data": _quote_payload(case, quote, args.strike),
    }
    return json.dumps(content), False


async def run_strategist(
    model: ModelProvider, case: EvalCase, skills: list[Skill]
) -> tuple[StrategistOutput, Usage]:
    """One Strategist answer on the case, with the same prompt the desk uses."""
    applied = [s for s in skills if ROLE.name in s.manifest.applies_to]
    context = case.strategist_context()
    if applied:
        context["skills"] = [s.context_entry() for s in applied]
    system = system_prompt(
        ROLE,
        symbol=case.symbol,
        mode="community",
        output_model=StrategistOutput,
        with_skills=bool(applied),
    )
    task = f"Propose trade ideas for {case.symbol}, or say why there is no trade."
    messages = [Message(role="user", content=user_message(task, context))]
    tools = [OPTION_QUOTE_TOOL.spec()]
    usage = Usage()
    answer: str | None = None
    for _ in range(ROLE.max_steps):
        response = await model.complete(
            system=system, messages=messages, tools=tools, max_tokens=ROLE.max_output_tokens
        )
        usage.input_tokens += response.usage.input_tokens
        usage.output_tokens += response.usage.output_tokens
        if response.stop_reason in TRUNCATED_STOP_REASONS:
            raise ModelError("The reply reached the output limit.", code="output_truncated")
        messages.append(
            Message(role="assistant", content=response.text, tool_calls=response.tool_calls)
        )
        if not response.tool_calls:
            answer = response.text
            break
        for call in response.tool_calls:
            content, is_error = serve_tool(case, call)
            messages.append(
                Message(
                    role="tool",
                    tool_call_id=call.id,
                    name=call.name,
                    content=content,
                    is_error=is_error,
                )
            )
    if answer is None:
        raise ModelError("The Strategist used every step without answering.", code="max_steps")
    parsed, repair = await parse_or_repair(
        model,
        system=system,
        text=answer,
        output_model=StrategistOutput,
        max_tokens=ROLE.max_output_tokens,
    )
    usage.input_tokens += repair.input_tokens
    usage.output_tokens += repair.output_tokens
    return parsed, usage


async def run_trial(
    model: ModelProvider,
    case: EvalCase,
    skills: list[Skill],
    trial: int,
    judge: Judge | None = None,
) -> TrialResult:
    result = TrialResult(case_id=case.id, trial=trial, with_skill=bool(skills))
    try:
        result.output, result.usage = await run_strategist(model, case, skills)
    except SocAgentsError as exc:
        result.error = exc.code
        return result
    result.checks = run_checks(result.output, case)
    if judge is not None:
        result.judge = await judge.grade(case, result.output, result.checks)
    return result


async def run_cases(
    model: ModelProvider,
    cases: list[EvalCase],
    skills_by_name: dict[str, Skill],
    *,
    trials: int,
    ablate: bool = False,
    judge: Judge | None = None,
    concurrency: int = 4,
) -> list[CaseSummary]:
    """Run every case ``trials`` times with its skill, and also without it when ``ablate``."""
    gate = asyncio.Semaphore(concurrency)
    arms = (True, False) if ablate else (True,)

    async def one(case: EvalCase, with_skill: bool, trial: int) -> TrialResult:
        skills = [skills_by_name[case.skill]] if with_skill else []
        async with gate:
            return await run_trial(model, case, skills, trial, judge)

    summaries: list[CaseSummary] = []
    for case in cases:
        for with_skill in arms:
            results = await asyncio.gather(*(one(case, with_skill, t) for t in range(trials)))
            summaries.append(
                CaseSummary(case.id, case.skill, case.expect, with_skill, list(results))
            )
    return summaries


def overall(summaries: list[CaseSummary], *, with_skill: bool = True) -> dict[str, float]:
    chosen = [s for s in summaries if s.with_skill is with_skill]
    if not chosen:
        return {}
    trials = [t for s in chosen for t in s.trials]
    per_case = [s.pass_rate for s in chosen]
    return {
        "cases": float(len(chosen)),
        "trials": float(len(trials)),
        "pass_at_1": statistics.fmean(per_case),
        "pass_at_1_sd": statistics.pstdev(per_case) if len(per_case) > 1 else 0.0,
        "mean_score": statistics.fmean(t.score for t in trials),
        "errors": float(sum(t.error is not None for t in trials)),
    }
