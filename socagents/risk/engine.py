"""Deterministic risk engine.

``evaluate`` is a pure function: no I/O, no clock, no randomness. It returns allow or deny
with reasons and suggested fixes. Whether a human approval is needed is decided elsewhere,
by the tool policy.

Units: for options, ``est_entry_price``, ``stop``, and ``target`` are option premiums per
share and one contract covers 100 shares. For equities they are share prices.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MULTIPLIER = {"option": 100, "equity": 1}
_EPS = 1e-9

_Deny = Callable[[str, str], None]


class PriceBasis(StrEnum):
    PREMIUM = "premium"
    UNDERLYING = "underlying"


class TradeIdea(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    instrument: Literal["option", "equity"]
    action: Literal["buy", "sell"]
    qty: int = Field(gt=0)
    price_basis: PriceBasis
    est_entry_price: float
    stop: float | None = None
    target: float | None = None
    right: Literal["call", "put"] | None = None
    strike: float | None = None
    expiration: date | None = None
    legs: int = Field(default=1, ge=1)


class RiskProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = 1
    max_loss_per_trade_usd: float = Field(default=500.0, gt=0)
    max_open_risk_usd: float = Field(default=2_000.0, gt=0)
    max_contracts: int = Field(default=5, gt=0)
    max_shares: int = Field(default=500, gt=0)
    allowed_symbols: frozenset[str] | None = None
    allow_short: bool = False
    min_dte: int = Field(default=0, ge=0)
    max_dte: int = Field(default=45, ge=0)
    require_stop: bool = True
    min_reward_risk: float = Field(default=1.0, ge=0)
    max_data_age_sec: int = Field(default=60, ge=0)


class RiskContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    today: date
    check_mode: Literal["execution", "educational"]
    data_age_sec: int = Field(ge=0)
    delayed: bool
    open_risk_usd: float = Field(default=0.0, ge=0)


class RiskReason(BaseModel):
    code: str
    message: str


class RiskFix(BaseModel):
    field: str
    value: float | int
    message: str


class RiskDecision(BaseModel):
    decision: Literal["allow", "deny"]
    check_mode: Literal["execution", "educational"]
    reasons: list[RiskReason]
    suggested_fixes: list[RiskFix]
    profile_version: int
    max_loss_usd: float | None

    @property
    def executable(self) -> bool:
        """True only for an allow decision made on fresh data in execution mode."""
        return self.decision == "allow" and self.check_mode == "execution"

    @property
    def display(self) -> Literal["pass", "fix", "reject"]:
        if self.decision == "allow":
            return "pass"
        return "fix" if self.suggested_fixes else "reject"


def max_loss_usd(idea: TradeIdea) -> float | None:
    """Loss if the stop is hit: |entry − stop| × multiplier × qty. ``None`` without a stop."""
    if idea.stop is None:
        return None
    per_unit = abs(idea.est_entry_price - idea.stop)
    return round(per_unit * MULTIPLIER[idea.instrument] * idea.qty, 2)


def evaluate(idea: TradeIdea, profile: RiskProfile, ctx: RiskContext) -> RiskDecision:
    reasons: list[RiskReason] = []
    fixes: list[RiskFix] = []

    def deny(code: str, message: str) -> None:
        reasons.append(RiskReason(code=code, message=message))

    def result(loss: float | None) -> RiskDecision:
        return RiskDecision(
            decision="deny" if reasons else "allow",
            check_mode=ctx.check_mode,
            reasons=reasons,
            suggested_fixes=fixes,
            profile_version=profile.version,
            max_loss_usd=loss,
        )

    numbers = (idea.est_entry_price, idea.stop, idea.target, idea.strike)
    if any(n is not None and (not math.isfinite(n) or n <= 0) for n in numbers):
        deny("invalid_number", "Prices and strikes must be finite and positive.")
        return result(None)

    _check_structure(idea, deny)
    if idea.action == "sell" and not profile.allow_short:
        deny("short_not_allowed", "Short positions are not allowed by this risk profile.")
    if profile.allowed_symbols is not None and idea.symbol.upper() not in profile.allowed_symbols:
        deny("symbol_not_allowed", f"{idea.symbol} is not in the allowed symbol list.")

    size_limit = profile.max_contracts if idea.instrument == "option" else profile.max_shares
    if idea.qty > size_limit:
        deny("size_limit", f"Quantity {idea.qty} exceeds the limit of {size_limit}.")
        fixes.append(RiskFix(field="qty", value=size_limit, message=f"Reduce qty to {size_limit}."))

    if idea.instrument == "option" and idea.expiration is not None:
        dte = (idea.expiration - ctx.today).days
        if dte < 0:
            deny("contract_expired", "The contract has already expired.")
        elif not profile.min_dte <= dte <= profile.max_dte:
            deny(
                "dte_out_of_range",
                f"{dte} days to expiration is outside {profile.min_dte}–{profile.max_dte}.",
            )

    loss = _check_stop_and_target(idea, profile, ctx, deny, fixes)

    if ctx.check_mode == "execution":
        if ctx.delayed:
            deny("delayed_data", "Execution checks need real-time data; this data is delayed.")
        elif ctx.data_age_sec > profile.max_data_age_sec:
            deny(
                "stale_data",
                f"Data is {ctx.data_age_sec}s old; the limit is {profile.max_data_age_sec}s.",
            )

    return result(loss)


def _check_structure(idea: TradeIdea, deny: _Deny) -> None:
    if idea.legs > 1:
        deny("multi_leg_not_supported", "Multi-leg structures are analysis only in this version.")
    if idea.instrument == "option":
        if idea.right is None or idea.strike is None or idea.expiration is None:
            deny("incomplete_contract", "Options need right, strike, and expiration.")
        if idea.price_basis is not PriceBasis.PREMIUM:
            deny("price_basis_mismatch", "Option stops and targets must be option premiums.")
    else:
        if idea.right is not None or idea.strike is not None or idea.expiration is not None:
            deny(
                "unexpected_option_fields", "Equity ideas cannot have right, strike, or expiration."
            )
        if idea.price_basis is not PriceBasis.UNDERLYING:
            deny("price_basis_mismatch", "Equity stops and targets must be share prices.")


def _check_stop_and_target(
    idea: TradeIdea, profile: RiskProfile, ctx: RiskContext, deny: _Deny, fixes: list[RiskFix]
) -> float | None:
    entry = idea.est_entry_price
    long = idea.action == "buy"
    unit = MULTIPLIER[idea.instrument]
    allowed_per_unit = profile.max_loss_per_trade_usd / (unit * idea.qty)

    if idea.stop is None:
        if profile.require_stop:
            deny("stop_required", "A stop is required.")
            candidate = entry - allowed_per_unit if long else entry + allowed_per_unit
            if candidate <= 0:
                candidate = entry * 0.5
            fixes.append(
                RiskFix(
                    field="stop",
                    value=round(candidate, 2),
                    message="Add a stop within the max loss limit.",
                )
            )
        return None

    if (long and idea.stop >= entry - _EPS) or (not long and idea.stop <= entry + _EPS):
        deny("stop_wrong_side", "The stop must be on the losing side of the entry price.")
        return None

    loss = max_loss_usd(idea)
    assert loss is not None
    if loss > profile.max_loss_per_trade_usd + _EPS:
        deny(
            "max_loss_exceeded",
            f"Max loss ${loss:,.2f} exceeds the ${profile.max_loss_per_trade_usd:,.2f} limit.",
        )
        per_unit_loss = abs(entry - idea.stop) * unit
        qty_fix = math.floor(profile.max_loss_per_trade_usd / per_unit_loss + _EPS)
        if qty_fix >= 1:
            fixes.append(RiskFix(field="qty", value=qty_fix, message=f"Reduce qty to {qty_fix}."))
        stop_fix = entry - allowed_per_unit if long else entry + allowed_per_unit
        if stop_fix > 0:
            fixes.append(
                RiskFix(
                    field="stop",
                    value=round(stop_fix, 2),
                    message="Tighten the stop to fit the max loss limit.",
                )
            )
    if ctx.open_risk_usd + loss > profile.max_open_risk_usd + _EPS:
        deny(
            "open_risk_exceeded",
            f"Open risk would reach ${ctx.open_risk_usd + loss:,.2f}; "
            f"the limit is ${profile.max_open_risk_usd:,.2f}.",
        )

    if idea.target is not None:
        if (long and idea.target <= entry + _EPS) or (not long and idea.target >= entry - _EPS):
            deny("target_wrong_side", "The target must be on the winning side of the entry price.")
        else:
            risk = abs(entry - idea.stop)
            reward_risk = abs(idea.target - entry) / risk
            if reward_risk + _EPS < profile.min_reward_risk:
                deny(
                    "reward_risk_too_low",
                    f"Reward/risk {reward_risk:.2f} is below {profile.min_reward_risk:.2f}.",
                )
                target_fix = entry + profile.min_reward_risk * risk * (1 if long else -1)
                if target_fix > 0:
                    fixes.append(
                        RiskFix(
                            field="target",
                            value=round(target_fix, 2),
                            message="Move the target to meet the minimum reward/risk.",
                        )
                    )
    return loss
