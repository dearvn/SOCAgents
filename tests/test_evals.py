from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from socagents.cli.main import app
from socagents.core.config import Settings
from socagents.core.errors import ConfigError
from socagents.desk.models import StrategistOutput
from socagents.evals.cases import EvalCase, discover_cases, load_case, select_cases
from socagents.evals.checks import CHECKS, run_checks, score
from socagents.evals.gate import NOP, gate_all, gate_case
from socagents.evals.judge import Judge
from socagents.evals.runner import overall, run_cases, run_trial, serve_tool
from socagents.model_gateway.types import Message, ModelResponse, ToolCall, ToolSpec
from socagents.skills import discover

runner = CliRunner()


def cases() -> dict[str, EvalCase]:
    loaded, problems = discover_cases()
    assert problems == []
    return {c.id: c for c in loaded}


def with_idea(case: EvalCase, **changes: Any) -> StrategistOutput:
    """The case's oracle with fields of its first idea changed."""
    idea = case.oracle.ideas[0]
    entry = changes.pop("entry", None)
    updated = idea.model_copy(update=changes)
    if entry is not None:
        updated = updated.model_copy(update={"entry": idea.entry.model_copy(update=entry)})
    return StrategistOutput(ideas=[updated], no_trade_reason=None)


def failing(output: StrategistOutput, case: EvalCase) -> set[str]:
    return {r.name for r in run_checks(output, case) if not r.passed}


class FakeModel:
    """Replies from a script: each step is a ModelResponse or a callable of the messages."""

    def __init__(self, steps: list[Any], *, provider: str = "fake", model: str = "a") -> None:
        self.provider = provider
        self.model = model
        self.steps = list(steps)
        self.calls: list[dict[str, Any]] = []

    async def aclose(self) -> None:
        return None

    async def complete(
        self, *, system: str, messages: list[Message], tools: list[ToolSpec], max_tokens: int
    ) -> ModelResponse:
        self.calls.append({"system": system, "messages": list(messages), "tools": tools})
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        return step(messages) if callable(step) else step


def text(body: Any) -> ModelResponse:
    return ModelResponse(text=json.dumps(body), stop_reason="end_turn", model="a")


# shipped cases


def test_every_shipped_case_is_accepted(settings: Settings) -> None:
    loaded = list(cases().values())
    assert len(loaded) >= 15
    assert {c.skill for c in loaded} == {
        "zero-dte-long-premium",
        "gamma-regime-playbook",
        "event-risk-checklist",
    }
    assert {c.expect for c in loaded} == {"idea", "no_trade"}
    for result in gate_all(loaded, settings.home):
        assert result.ok, (result.case_id, result.problems)


def test_the_empty_answer_earns_nothing_on_an_idea_case() -> None:
    for case in cases().values():
        results = run_checks(NOP, case)
        if case.expect == "idea":
            assert score(results) == 0.0, case.id
        else:
            assert not all(r.passed for r in results), case.id


def test_cases_use_synthetic_snapshot_ids_only() -> None:
    for case in cases().values():
        assert all(i.startswith("snp_case_") for i in case.snapshot_ids()), case.id


# the gate rejects cases that cannot tell good from bad


def test_gate_rejects_an_oracle_that_fails_its_own_checks(settings: Settings) -> None:
    case = cases()["zero-dte-break-above-opening-range"]
    broken = case.model_copy(update={"oracle": with_idea(case, stop=0.2)})
    result = gate_case(broken, settings.home)
    assert not result.ok
    assert "stop_within_half_premium" in result.problems[0]


def test_gate_rejects_a_no_trade_case_without_a_failing_counter(settings: Settings) -> None:
    case = cases()["gamma-positive-mid-range"]
    assert (
        "counter answer"
        in gate_case(case.model_copy(update={"counter": None}), settings.home).problems[0]
    )
    quiet = StrategistOutput(ideas=[], no_trade_reason="nothing to do")
    result = gate_case(case.model_copy(update={"counter": quiet}), settings.home)
    assert any("counter answer passes" in p for p in result.problems)


def test_gate_rejects_unknown_checks_missing_expectation_and_undefined_snapshots(
    settings: Settings,
) -> None:
    case = cases()["zero-dte-break-below-vwap"]
    unknown = EvalCase.model_validate(
        {**case.model_dump(mode="json"), "checks": [{"name": "expectation"}, {"name": "nope"}]}
    )
    assert "unknown check" in gate_case(unknown, settings.home).problems[0]

    no_expect = EvalCase.model_validate(
        {**case.model_dump(mode="json"), "checks": [{"name": "qty_one"}]}
    )
    assert any("expectation" in p for p in gate_case(no_expect, settings.home).problems)

    made_up = case.model_copy(update={"oracle": with_idea(case, evidence=["snp_case_nowhere"])})
    problems = gate_case(made_up, settings.home).problems
    assert any("undefined snapshot" in p for p in problems)


def test_gate_rejects_a_case_for_a_missing_skill(settings: Settings) -> None:
    case = cases()["gamma-near-flip-wait"]
    result = gate_case(case.model_copy(update={"skill": "not-a-skill"}), settings.home)
    assert "not an official skill" in result.problems[0]


def test_load_case_requires_the_file_name_to_be_the_id(tmp_path: Path) -> None:
    case = cases()["gamma-near-flip-wait"]
    path = tmp_path / "other-name.json"
    path.write_text(case.model_dump_json(), encoding="utf-8")
    with pytest.raises(ConfigError, match="file name"):
        load_case(path)
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    with pytest.raises(ConfigError, match="readable JSON"):
        load_case(tmp_path / "bad.json")


def test_select_cases_refuses_an_empty_selection() -> None:
    loaded = list(cases().values())
    assert len(select_cases(loaded, skills=["gamma-regime-playbook"])) == 5
    with pytest.raises(ConfigError):
        select_cases(loaded, ids=["nope"])


# checks


def test_zero_dte_checks() -> None:
    case = cases()["zero-dte-break-above-opening-range"]
    assert failing(case.oracle, case) == set()
    assert "stop_within_half_premium" in failing(with_idea(case, stop=0.3), case)
    assert "stop_within_half_premium" in failing(with_idea(case, stop=0.9), case)
    assert "qty_one" in failing(with_idea(case, qty=3), case)
    assert "trigger_on_level" in failing(
        with_idea(case, entry={"condition": "at_market", "trigger_price": None}), case
    )
    assert "trigger_on_level" in failing(with_idea(case, entry={"trigger_price": 600.55}), case)
    assert "quote_matches" in failing(with_idea(case, est_entry_premium=0.95), case)
    far = with_idea(
        case,
        strike=610.0,
        est_entry_premium=0.015,
        stop=0.01,
        target=0.05,
        evidence=["snp_case_q610c"],
    )
    assert "near_money" in failing(far, case)
    assert failing(with_idea(case, evidence=[]), case) >= {"evidence_resolves", "quote_matches"}


def test_late_entry_allows_only_a_trigger_close_to_spot() -> None:
    late = cases()["zero-dte-late-session-close-trigger"]
    assert failing(late.oracle, late) == set()
    assert "late_entry" in failing(with_idea(late, entry={"trigger_price": 601.5}), late)
    morning = cases()["zero-dte-break-above-opening-range"]
    params = {"after_et": "14:30", "max_trigger_pct": 0.01}
    assert CHECKS["late_entry"](morning.oracle, morning, params).passed  # before 14:30 ET


def test_gamma_checks() -> None:
    flip = cases()["gamma-near-flip-wait"]
    assert "waits_beyond_flip" in failing(
        with_idea(flip, entry={"condition": "break_out", "trigger_price": 599.4}), flip
    )
    assert "waits_beyond_flip" in failing(
        with_idea(flip, entry={"condition": "at_market", "trigger_price": None}), flip
    )
    neg = cases()["gamma-negative-put-wall-break"]
    assert "break_matches_direction" in failing(
        with_idea(neg, entry={"condition": "break_out"}), neg
    )
    pos = cases()["gamma-positive-put-wall-rejection"]
    away = with_idea(pos, entry={"trigger_price": 595.6})
    assert failing(away, pos) == set()  # 595.60 is 0.07% from the put wall
    assert "wall_rejection" in failing(
        with_idea(pos, right="put", evidence=["snp_case_q596p"], est_entry_premium=1.38), pos
    )


def test_event_checks() -> None:
    case = cases()["event-cpi-premarket-call"]
    bare = with_idea(case, rationale="Break of the pre-market high.", invalidation="Below 599.50.")
    assert failing(bare, case) >= {"names_event", "mentions_iv_drop", "waits_for_release"}


def test_a_check_that_needs_a_missing_fact_is_reported_by_the_gate(settings: Settings) -> None:
    case = cases()["gamma-near-flip-wait"]
    result = gate_case(case.model_copy(update={"facts": {}}), settings.home)
    assert any("needs a numeric fact 'flip'" in p for p in result.problems)


# serving quotes


def test_serve_tool_answers_from_the_case_with_the_nearest_strike() -> None:
    case = cases()["zero-dte-break-above-opening-range"]
    content, is_error = serve_tool(
        case,
        ToolCall(
            id="c1",
            name="get_option_quote",
            arguments={"symbol": "SPY", "right": "call", "strike": 601.2},
        ),
    )
    assert not is_error
    body = json.loads(content)
    assert body["snapshot_id"] == "snp_case_q601c"
    assert body["data"]["mid"] == 0.86
    assert body["data"]["exact_strike"] is False

    for call in (
        ToolCall(id="c2", name="get_quote", arguments={}),
        ToolCall(
            id="c3",
            name="get_option_quote",
            arguments={"symbol": "QQQ", "right": "call", "strike": 500},
        ),
        ToolCall(id="c4", name="get_option_quote", arguments={"symbol": "SPY"}),
    ):
        assert serve_tool(case, call)[1] is True


# running


def quote_then_answer(case: EvalCase) -> list[Any]:
    idea = case.oracle.ideas[0]
    ask = ModelResponse(
        text="",
        tool_calls=[
            ToolCall(
                id="q1",
                name="get_option_quote",
                arguments={"symbol": case.symbol, "right": idea.right, "strike": idea.strike},
            )
        ],
        stop_reason="tool_use",
        model="a",
    )
    return [ask, text(case.oracle.model_dump(mode="json"))]


async def test_a_model_that_gives_the_oracle_passes(settings: Settings) -> None:
    case = cases()["zero-dte-break-above-opening-range"]
    skills, _ = discover(settings.home)
    model = FakeModel(quote_then_answer(case))
    result = await run_trial(model, case, [skills[case.skill]], trial=0)
    assert result.error is None
    assert result.passed and result.score == 1.0
    first = model.calls[0]
    assert "skills" in first["messages"][0].content
    assert [t.name for t in first["tools"]] == ["get_option_quote"]
    tool_reply = model.calls[1]["messages"][-1]
    assert tool_reply.role == "tool" and "snp_case_q601c" in tool_reply.content


async def test_an_invalid_answer_is_an_error_with_zero_score(settings: Settings) -> None:
    case = cases()["gamma-near-flip-wait"]
    model = FakeModel([ModelResponse(text="no json here", stop_reason="end_turn", model="a")])
    result = await run_trial(model, case, [], trial=0)
    assert result.error == "invalid_output"
    assert not result.passed and result.score == 0.0


async def test_a_truncated_reply_is_an_error(settings: Settings) -> None:
    case = cases()["gamma-near-flip-wait"]
    model = FakeModel([ModelResponse(text="{", stop_reason="max_tokens", model="a")])
    assert (await run_trial(model, case, [], trial=0)).error == "output_truncated"


async def test_ablation_runs_each_case_with_and_without_its_skill(settings: Settings) -> None:
    case = cases()["gamma-positive-mid-range"]
    skills, _ = discover(settings.home)
    model = FakeModel([text(case.oracle.model_dump(mode="json"))])
    summaries = await run_cases(model, [case], skills, trials=3, ablate=True)
    assert [(s.with_skill, len(s.trials)) for s in summaries] == [(True, 3), (False, 3)]
    without = [c for c in model.calls if "skills" not in c["messages"][0].content]
    assert len(without) == 3
    totals = overall(summaries)
    assert totals["pass_at_1"] == 1.0 and totals["trials"] == 3.0
    assert overall(summaries, with_skill=False)["pass_at_1"] == 1.0


# judge


def test_the_judge_must_not_be_the_model_under_test() -> None:
    with pytest.raises(ConfigError, match="different model"):
        Judge(FakeModel([]), candidate=FakeModel([]))


async def test_the_judge_grade_is_reported_and_never_changes_pass(settings: Settings) -> None:
    case = cases()["gamma-positive-mid-range"]
    judge = Judge(
        FakeModel([text({"score": -3, "rationale": "Ignores the walls."})], model="judge"),
        candidate=FakeModel([]),
    )
    prompt = judge.prompt(case, case.oracle, run_checks(case.oracle, case))
    assert "Must-avoid" in prompt and "Constitution" in prompt
    model = FakeModel([text(case.oracle.model_dump(mode="json"))])
    result = await run_trial(model, case, [], trial=0, judge=judge)
    assert result.passed
    assert result.judge is not None and result.judge.score == -3


async def test_a_judge_failure_is_recorded_not_raised() -> None:
    case = cases()["gamma-positive-mid-range"]
    judge = Judge(
        FakeModel([ModelResponse(text="nope", stop_reason="end_turn", model="j")], model="judge"),
        candidate=FakeModel([]),
    )
    grade = await judge.grade(case, case.oracle, [])
    assert grade.error == "invalid_output"


# CLI


def test_cli_eval_list_and_check() -> None:
    listed = runner.invoke(app, ["eval", "list", "--skill", "event-risk-checklist"])
    assert listed.exit_code == 0, listed.output
    assert "event-cpi-premarket-call" in listed.output
    checked = runner.invoke(app, ["eval", "check"])
    assert checked.exit_code == 0, checked.output
    assert "cases accepted" in checked.output


def test_cli_eval_run_json_with_the_scripted_model(tmp_path: Path) -> None:
    out = tmp_path / "result.json"
    result = runner.invoke(
        app,
        [
            "eval",
            "run",
            "-m",
            "fixture/scripted",
            "--case",
            "gamma-positive-mid-range",
            "--trials",
            "2",
            "--json",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["model"] == "fixture/scripted"
    assert payload["overall"]["trials"] == 2.0
    assert payload["cases"][0]["case"] == "gamma-positive-mid-range"
    assert json.loads(out.read_text(encoding="utf-8")) == payload


def test_cli_eval_run_refuses_a_judge_equal_to_the_model() -> None:
    result = runner.invoke(
        app,
        [
            "eval",
            "run",
            "-m",
            "fixture/scripted",
            "--judge",
            "fixture/scripted",
            "--trials",
            "1",
            "--case",
            "gamma-positive-mid-range",
        ],
    )
    assert result.exit_code == 2
    assert "judge_is_candidate" in result.output


def test_cli_eval_run_unknown_case() -> None:
    result = runner.invoke(app, ["eval", "run", "--case", "nope"])
    assert result.exit_code == 2
    assert "no_cases" in result.output
