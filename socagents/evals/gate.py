"""Case acceptance: a case is only usable when its checks can tell a good answer from a bad one.

* The reference answer (oracle) must pass every check.
* On an idea case, the empty answer (no ideas, no reason) must fail every check.
* On a no-trade case, the empty answer still fails (no reason given), and the case needs a
  counter answer, a plausible idea, which must fail the ``expectation`` check.
* Static rules: the case's skill exists and lints clean, every check name is known, and every
  snapshot id the oracle cites is defined by the case.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from socagents.desk.models import StrategistOutput
from socagents.evals.cases import SNAPSHOT_RE, EvalCase
from socagents.evals.checks import CHECKS, run_checks
from socagents.skills import discover, lint

NOP = StrategistOutput(ideas=[], no_trade_reason=None)


@dataclass
class GateResult:
    case_id: str
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _failing(results: list[tuple[str, bool]]) -> list[str]:
    return [name for name, passed in results if not passed]


def gate_case(case: EvalCase, home: Path) -> GateResult:
    result = GateResult(case.id)
    problems = result.problems

    skills, _ = discover(home)
    skill = skills.get(case.skill)
    if skill is None or skill.source != "official":
        problems.append(f"skill {case.skill!r} is not an official skill")
    elif lint(skill):
        problems.append(f"skill {case.skill!r} does not lint clean")

    unknown = [c.name for c in case.checks if c.name not in CHECKS]
    if unknown:
        problems.append(f"unknown check(s): {', '.join(unknown)}")
        return result
    if "expectation" not in {c.name for c in case.checks}:
        problems.append("every case needs the expectation check")

    bad_ids = sorted(i for i in case.snapshot_ids() if not SNAPSHOT_RE.fullmatch(i))
    if bad_ids:
        problems.append(f"snapshot ids must look like snp_<a-z0-9_>: {', '.join(bad_ids[:3])}")
    ids = [q.snapshot_id for q in case.quotes]
    if len(ids) != len(set(ids)):
        problems.append("quote snapshot ids must be unique")
    for idea in case.oracle.ideas:
        missing = [e for e in idea.evidence if e not in case.snapshot_ids()]
        if missing:
            problems.append(f"oracle cites undefined snapshot(s): {', '.join(missing)}")

    try:
        oracle = run_checks(case.oracle, case)
        nop = run_checks(NOP, case)
        counter = run_checks(case.counter, case) if case.counter is not None else None
    except ValueError as exc:  # a check needs a fact the case does not define
        problems.append(str(exc))
        return result

    failed = _failing([(r.name, r.passed) for r in oracle])
    if failed:
        details = "; ".join(f"{r.name}: {r.detail}" for r in oracle if not r.passed)
        problems.append(f"oracle fails {', '.join(failed)} ({details})")

    if case.expect == "idea":
        passed = [r.name for r in nop if r.passed]
        if passed:
            problems.append(f"the empty answer passes {', '.join(passed)}")
    else:
        if all(r.passed for r in nop):
            problems.append("the empty answer passes every check")
        if counter is None:
            problems.append("a no-trade case needs a counter answer")
        elif next((r.passed for r in counter if r.name == "expectation"), True):
            problems.append("the counter answer passes the expectation check")
    return result


def gate_all(cases: list[EvalCase], home: Path) -> list[GateResult]:
    return [gate_case(c, home) for c in cases]
