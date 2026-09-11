from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

import pytest

from socagents.risk import (
    PriceBasis,
    RiskContext,
    RiskDecision,
    RiskProfile,
    TradeIdea,
    evaluate,
    max_loss_usd,
)

TODAY = date(2026, 9, 10)
PROFILE = RiskProfile()
FRESH = RiskContext(today=TODAY, check_mode="execution", data_age_sec=5, delayed=False)


def call_idea(**overrides: Any) -> TradeIdea:
    base: dict[str, Any] = {
        "symbol": "SPX",
        "instrument": "option",
        "action": "buy",
        "qty": 1,
        "price_basis": PriceBasis.PREMIUM,
        "est_entry_price": 4.20,
        "stop": 2.10,
        "target": 6.50,
        "right": "call",
        "strike": 5820.0,
        "expiration": TODAY,
    }
    base.update(overrides)
    return TradeIdea(**base)


def equity_idea(**overrides: Any) -> TradeIdea:
    base: dict[str, Any] = {
        "symbol": "AAPL",
        "instrument": "equity",
        "action": "buy",
        "qty": 10,
        "price_basis": PriceBasis.UNDERLYING,
        "est_entry_price": 200.0,
        "stop": 195.0,
        "target": 212.0,
    }
    base.update(overrides)
    return TradeIdea(**base)


def codes(decision: RiskDecision) -> set[str]:
    return {r.code for r in decision.reasons}


def fix(decision: RiskDecision, field: str) -> float | int:
    return next(f.value for f in decision.suggested_fixes if f.field == field)


def test_max_loss_formula_uses_premium_times_100() -> None:
    assert max_loss_usd(call_idea()) == 210.0  # (4.20 - 2.10) * 100 * 1
    assert max_loss_usd(call_idea(qty=2)) == 420.0
    assert max_loss_usd(equity_idea()) == 50.0  # (200 - 195) * 10


def test_max_loss_is_none_without_stop() -> None:
    assert max_loss_usd(call_idea(stop=None)) is None


def test_valid_long_call_passes() -> None:
    d = evaluate(call_idea(), PROFILE, FRESH)
    assert d.decision == "allow"
    assert d.executable
    assert d.display == "pass"
    assert d.max_loss_usd == 210.0
    assert d.reasons == []
    assert d.profile_version == PROFILE.version


def test_valid_equity_passes() -> None:
    assert evaluate(equity_idea(), PROFILE, FRESH).decision == "allow"


def test_educational_mode_is_never_executable() -> None:
    ctx = RiskContext(today=TODAY, check_mode="educational", data_age_sec=900, delayed=True)
    d = evaluate(call_idea(), PROFILE, ctx)
    assert d.decision == "allow"
    assert not d.executable


def test_execution_mode_denies_delayed_data() -> None:
    ctx = RiskContext(today=TODAY, check_mode="execution", data_age_sec=900, delayed=True)
    assert codes(evaluate(call_idea(), PROFILE, ctx)) == {"delayed_data"}


def test_execution_mode_denies_stale_data() -> None:
    ctx = RiskContext(today=TODAY, check_mode="execution", data_age_sec=120, delayed=False)
    assert codes(evaluate(call_idea(), PROFILE, ctx)) == {"stale_data"}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("est_entry_price", 0.0),
        ("est_entry_price", math.nan),
        ("stop", -1.0),
        ("target", math.nan),
        ("strike", math.inf),
    ],
)
def test_invalid_numbers_are_rejected(field: str, value: float) -> None:
    d = evaluate(call_idea(**{field: value}), PROFILE, FRESH)
    assert codes(d) == {"invalid_number"}
    assert d.display == "reject"


def test_multi_leg_is_analysis_only() -> None:
    assert "multi_leg_not_supported" in codes(evaluate(call_idea(legs=2), PROFILE, FRESH))


def test_option_needs_full_contract() -> None:
    assert "incomplete_contract" in codes(evaluate(call_idea(strike=None), PROFILE, FRESH))


def test_option_needs_premium_basis() -> None:
    d = evaluate(call_idea(price_basis=PriceBasis.UNDERLYING), PROFILE, FRESH)
    assert "price_basis_mismatch" in codes(d)


def test_equity_needs_underlying_basis() -> None:
    d = evaluate(equity_idea(price_basis=PriceBasis.PREMIUM), PROFILE, FRESH)
    assert "price_basis_mismatch" in codes(d)


def test_equity_rejects_option_fields() -> None:
    assert "unexpected_option_fields" in codes(evaluate(equity_idea(right="call"), PROFILE, FRESH))


def test_short_denied_by_default() -> None:
    d = evaluate(call_idea(action="sell", stop=6.30, target=2.10), PROFILE, FRESH)
    assert codes(d) == {"short_not_allowed"}


def test_short_allowed_when_profile_allows() -> None:
    profile = RiskProfile(allow_short=True)
    d = evaluate(call_idea(action="sell", stop=6.30, target=2.10), profile, FRESH)
    assert d.decision == "allow"
    assert d.max_loss_usd == 210.0


def test_symbol_allowlist() -> None:
    profile = RiskProfile(allowed_symbols=frozenset({"SPY"}))
    assert "symbol_not_allowed" in codes(evaluate(call_idea(), profile, FRESH))


def test_size_limit_suggests_max_qty() -> None:
    d = evaluate(call_idea(qty=6, est_entry_price=1.0, stop=0.5, target=2.0), PROFILE, FRESH)
    assert codes(d) == {"size_limit"}
    assert fix(d, "qty") == PROFILE.max_contracts
    assert d.display == "fix"


def test_expired_contract() -> None:
    d = evaluate(call_idea(expiration=TODAY - timedelta(days=1)), PROFILE, FRESH)
    assert "contract_expired" in codes(d)


def test_dte_out_of_range() -> None:
    d = evaluate(call_idea(expiration=TODAY + timedelta(days=60)), PROFILE, FRESH)
    assert "dte_out_of_range" in codes(d)


def test_stop_required_suggests_stop_within_limit() -> None:
    profile = RiskProfile(max_loss_per_trade_usd=100)
    d = evaluate(call_idea(stop=None), profile, FRESH)
    assert "stop_required" in codes(d)
    assert fix(d, "stop") == 3.2  # 4.20 - 100 / 100


def test_stop_required_falls_back_to_half_entry() -> None:
    d = evaluate(call_idea(stop=None), PROFILE, FRESH)
    assert fix(d, "stop") == 2.1


@pytest.mark.parametrize("stop", [5.0, 4.20])
def test_stop_on_wrong_side(stop: float) -> None:
    assert "stop_wrong_side" in codes(evaluate(call_idea(stop=stop), PROFILE, FRESH))


def test_max_loss_exceeded_suggests_qty_and_stop() -> None:
    d = evaluate(call_idea(qty=3), PROFILE, FRESH)
    assert codes(d) == {"max_loss_exceeded"}
    assert d.max_loss_usd == 630.0
    assert fix(d, "qty") == 2
    assert fix(d, "stop") == 2.53  # 4.20 - 500 / 300


def test_applying_suggested_qty_passes() -> None:
    first = evaluate(call_idea(qty=3), PROFILE, FRESH)
    second = evaluate(call_idea(qty=fix(first, "qty")), PROFILE, FRESH)
    assert second.decision == "allow"


def test_open_risk_limit() -> None:
    ctx = FRESH.model_copy(update={"open_risk_usd": 1_900.0})
    assert codes(evaluate(call_idea(), PROFILE, ctx)) == {"open_risk_exceeded"}


def test_target_on_wrong_side() -> None:
    assert "target_wrong_side" in codes(evaluate(call_idea(target=4.0), PROFILE, FRESH))


def test_reward_risk_too_low_suggests_target() -> None:
    d = evaluate(call_idea(target=5.0), PROFILE, FRESH)
    assert codes(d) == {"reward_risk_too_low"}
    assert fix(d, "target") == 6.3  # 4.20 + 1.0 * 2.10


def test_problems_accumulate() -> None:
    d = evaluate(call_idea(legs=2, qty=9, target=4.0), PROFILE, FRESH)
    assert {"multi_leg_not_supported", "size_limit", "target_wrong_side"} <= codes(d)


def test_evaluate_is_deterministic_and_does_not_mutate() -> None:
    idea = call_idea(qty=3)
    before = idea.model_dump()
    assert evaluate(idea, PROFILE, FRESH) == evaluate(idea, PROFILE, FRESH)
    assert idea.model_dump() == before
