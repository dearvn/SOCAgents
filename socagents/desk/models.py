"""SOC Desk data models: analyst reports, debate turns, ideas, and the Desk Report."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from socagents.core.errors import SocAgentsError
from socagents.risk.engine import PriceBasis, RiskFix, RiskReason, TradeIdea

Stance = Literal["bullish", "bearish", "neutral"]

DISCLAIMER = "Not investment advice. AI can be wrong."
EDUCATIONAL_LABEL = "Educational: delayed data, not risk-checked for execution."
REPLAY_LABEL = "Replay: recorded data, not risk-checked for execution."
FOOTER = "Made with SOCAgents · https://github.com/dearvn/SOCAgents"


class KeyLevel(BaseModel):
    price: float
    kind: str
    evidence: list[str] = Field(default_factory=list)


class Signal(BaseModel):
    name: str
    value: str
    evidence: list[str] = Field(default_factory=list)


class AnalystReport(BaseModel):
    role: str = ""
    stance: Stance
    confidence: float = Field(ge=0, le=1)
    summary: str
    key_levels: list[KeyLevel] = Field(default_factory=list)
    signals: list[Signal] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class DebateArgument(BaseModel):
    argument: str
    evidence: list[str] = Field(default_factory=list)


class DebateTurn(DebateArgument):
    side: Literal["bull", "bear"]
    round: int


class IdeaEntry(BaseModel):
    condition: Literal["break_out", "break_down", "at_market"] = "at_market"
    trigger_price: float | None = None


class DeskIdea(BaseModel):
    structure: str = Field(description="For example long_call, long_put, long_stock.")
    symbol: str
    instrument: Literal["option", "equity"] = "option"
    action: Literal["buy", "sell"] = "buy"
    right: Literal["call", "put"] | None = None
    strike: float | None = None
    expiration: date | None = None
    qty: int = Field(default=1, ge=1)
    legs: int = Field(default=1, ge=1)
    entry: IdeaEntry = Field(default_factory=IdeaEntry)
    price_basis: PriceBasis = Field(
        description="premium for options (stop and target are option prices), underlying for "
        "equities."
    )
    est_entry_premium: float = Field(
        description="Estimated entry price: option mid premium, or share price for equities."
    )
    stop: float | None = None
    target: float | None = None
    invalidation: str = ""
    rationale: str = ""
    evidence: list[str] = Field(default_factory=list)

    @property
    def contract(self) -> str:
        if self.instrument == "equity":
            return self.symbol
        expiration = self.expiration.isoformat() if self.expiration else "?"
        strike = f"{self.strike:g}" if self.strike is not None else "?"
        right = (self.right or "?")[0].upper()
        return f"{self.symbol} {expiration} {strike}{right}"

    def to_trade_idea(self) -> TradeIdea:
        return TradeIdea(
            symbol=self.symbol,
            instrument=self.instrument,
            action=self.action,
            qty=self.qty,
            price_basis=self.price_basis,
            est_entry_price=self.est_entry_premium,
            stop=self.stop,
            target=self.target,
            right=self.right,
            strike=self.strike,
            expiration=self.expiration,
            legs=self.legs,
        )


class StrategistOutput(BaseModel):
    ideas: list[DeskIdea] = Field(default_factory=list, max_length=3)
    no_trade_reason: str | None = None


class Critique(BaseModel):
    index: int
    critique: str


class CritiqueOutput(BaseModel):
    critiques: list[Critique] = Field(default_factory=list)


class IdeaRiskCheck(BaseModel):
    mode: Literal["execution", "educational"]
    decision: Literal["allow", "deny"]
    display: Literal["pass", "fix", "reject"]
    reasons: list[RiskReason] = Field(default_factory=list)
    suggested_fixes: list[RiskFix] = Field(default_factory=list)
    max_loss_usd: float | None = None
    critique: str | None = None


class ReviewedIdea(BaseModel):
    idea: DeskIdea
    risk_check: IdeaRiskCheck
    convertible: bool
    not_convertible_reason: str | None = None
    label: str | None = None


class Scenario(BaseModel):
    name: str
    condition: str
    path: str
    evidence: list[str] = Field(default_factory=list)


class LeadOutput(BaseModel):
    regime: str
    summary: str
    key_levels: list[KeyLevel] = Field(default_factory=list)
    scenarios: list[Scenario] = Field(default_factory=list)
    dissent: str = ""


class DataFreshness(BaseModel):
    oldest_snapshot_sec: int | None
    delayed: bool


class UsageSummary(BaseModel):
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = 0.0

    def add(self, *, calls: int, input_tokens: int, output_tokens: int, cost: float | None) -> None:
        self.model_calls += calls
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cost_usd = None if cost is None or self.cost_usd is None else self.cost_usd + cost


class SkillRef(BaseModel):
    name: str
    source: str
    sha256: str


class DeskReport(BaseModel):
    id: str
    desk_run_id: str
    symbol: str
    as_of: datetime
    mode: Literal["community", "member"]
    profile: str
    regime: str
    summary: str
    key_levels: list[KeyLevel]
    scenarios: list[Scenario]
    ideas: list[ReviewedIdea]
    no_trade_reason: str | None = None
    dissent: str
    analysts: list[AnalystReport]
    debate: list[DebateTurn]
    data_freshness: DataFreshness
    removed_unverified: list[str] = Field(default_factory=list)
    missing_roles: list[str] = Field(default_factory=list)
    notices: list[str] = Field(default_factory=list)
    models: dict[str, str] = Field(default_factory=dict)
    usage: UsageSummary = Field(default_factory=UsageSummary)
    created_at: datetime
    replay_of: str | None = None
    skills: list[SkillRef] = Field(default_factory=list)
    disclaimer: str = DISCLAIMER

    def shareable_view(self) -> dict[str, Any]:
        """Regime, key levels, and scenarios only. Member reports are never shareable."""
        if self.mode == "member":
            raise SocAgentsError(
                "Reports built on SocSwift member data cannot be exported or shared.",
                code="member_data_not_shareable",
            )
        return {
            "symbol": self.symbol,
            "as_of": self.as_of.isoformat(),
            "mode": self.mode,
            "profile": self.profile,
            "regime": self.regime,
            "key_levels": [level.model_dump() for level in self.key_levels],
            "scenarios": [scenario.model_dump() for scenario in self.scenarios],
            "data_freshness": self.data_freshness.model_dump(),
            "disclaimer": self.disclaimer,
            "made_with": FOOTER,
        }
