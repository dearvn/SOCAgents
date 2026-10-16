"""Eval case files: one market situation, its expected outcome, checks, rubric, and answers."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from socagents.core.errors import ConfigError
from socagents.core.market import market_session
from socagents.desk.graph import MAX_IDEAS
from socagents.desk.models import AnalystReport, DebateTurn, StrategistOutput

CASES_DIR = Path(__file__).parent / "cases"
CASE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,79}")
SNAPSHOT_RE = re.compile(r"snp_[a-z0-9_]{1,40}")

Expect = Literal["idea", "no_trade"]


class CaseQuote(BaseModel):
    """One option contract the case serves through ``get_option_quote``."""

    snapshot_id: str
    right: Literal["call", "put"]
    strike: float = Field(gt=0)
    expiration: date
    bid: float = Field(ge=0)
    ask: float = Field(ge=0)
    iv: float | None = None
    delta: float | None = None
    volume: int = 0
    open_interest: int = 0

    @property
    def mid(self) -> float:
        return round((self.bid + self.ask) / 2, 4)


class CheckSpec(BaseModel):
    name: str
    params: dict[str, Any] = Field(default_factory=dict)


class Rubric(BaseModel):
    must_do: list[str] = Field(min_length=1)
    must_avoid: list[str] = Field(min_length=1)
    best_practice: list[str] = Field(min_length=1)


class EvalCase(BaseModel):
    id: str
    skill: str
    title: str = Field(min_length=1, max_length=120)
    expect: Expect
    symbol: str
    as_of: datetime
    spot: float = Field(gt=0)
    reports: list[AnalystReport] = Field(min_length=1)
    debate: list[DebateTurn] = Field(default_factory=list)
    quotes: list[CaseQuote] = Field(default_factory=list)
    facts: dict[str, Any] = Field(default_factory=dict)
    checks: list[CheckSpec] = Field(min_length=1)
    rubric: Rubric
    oracle: StrategistOutput
    counter: StrategistOutput | None = None

    @field_validator("as_of")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("as_of needs a timezone")
        return value

    @property
    def spot_snapshot(self) -> str:
        return "snp_case_spot"

    def snapshot_ids(self) -> set[str]:
        """Every snapshot id the case's data defines."""
        ids = {self.spot_snapshot, *(q.snapshot_id for q in self.quotes)}
        for report in self.reports:
            ids.update(report.evidence)
            for level in report.key_levels:
                ids.update(level.evidence)
            for signal in report.signals:
                ids.update(signal.evidence)
        return ids

    def level_prices(self) -> list[float]:
        return [level.price for report in self.reports for level in report.key_levels]

    def quote_for(self, sid: str) -> CaseQuote | None:
        return next((q for q in self.quotes if q.snapshot_id == sid), None)

    def strategist_context(self) -> dict[str, Any]:
        """The Strategist's context, shaped like the one ``DeskGraph`` builds."""
        session = market_session(self.as_of, self.as_of)
        return {
            "symbol": self.symbol,
            "mode": "community",
            "spot": self.spot,
            "spot_snapshot": self.spot_snapshot,
            "as_of": self.as_of.isoformat(),
            "market": session.context(),
            "reports": [r.model_dump(mode="json") for r in self.reports],
            "debate": [t.model_dump(mode="json") for t in self.debate],
            "max_ideas": MAX_IDEAS,
        }


def load_case(path: Path) -> EvalCase:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(
            f"{path}: not a readable JSON file ({exc}).", code="invalid_case"
        ) from exc
    try:
        case = EvalCase.model_validate(data)
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        where = ".".join(str(p) for p in first["loc"]) or "case"
        raise ConfigError(f"{path}: {where}: {first['msg']}.", code="invalid_case") from exc
    if path.stem != case.id:
        raise ConfigError(f"{path}: the file name must be the case id.", code="invalid_case")
    return case


def discover_cases(root: Path = CASES_DIR) -> tuple[list[EvalCase], list[str]]:
    """Every loadable case under ``root/<skill>/<id>.json``, and a line per file that failed."""
    cases: list[EvalCase] = []
    problems: list[str] = []
    for path in sorted(root.glob("*/*.json")):
        try:
            case = load_case(path)
        except ConfigError as exc:
            problems.append(str(exc))
            continue
        if path.parent.name != case.skill:
            problems.append(f"{path}: the folder name must be the case's skill.")
            continue
        cases.append(case)
    return cases, problems


def select_cases(
    cases: list[EvalCase], *, skills: list[str] | None = None, ids: list[str] | None = None
) -> list[EvalCase]:
    chosen = [c for c in cases if (not skills or c.skill in skills) and (not ids or c.id in ids)]
    if (skills or ids) and not chosen:
        raise ConfigError("No eval case matches. See `socagents eval list`.", code="no_cases")
    return chosen
