from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from socagents.core.errors import ProviderError, SymbolNotFound
from socagents.providers.fixture import FixtureProvider
from socagents.tools.market import (
    BarsIn,
    ChainSummaryIn,
    QuoteIn,
    get_bars,
    get_option_chain_summary,
    get_quote,
)


def test_fixture_symbols() -> None:
    assert FixtureProvider().available_symbols() == ["QQQ", "SPY"]


async def test_unknown_symbol_raises() -> None:
    with pytest.raises(SymbolNotFound):
        await FixtureProvider().quotes(["TSLA"])


async def test_bars_interval_must_match() -> None:
    with pytest.raises(ProviderError):
        await FixtureProvider().bars("SPY", "1m")


async def test_quote_view(ctx) -> None:
    out = await get_quote(QuoteIn(symbols=["spy"]), ctx)
    q = out.quotes[0]
    assert q.symbol == "SPY"
    assert q.last == 581.20
    assert q.change_pct == pytest.approx((581.20 / 578.77 - 1) * 100, abs=1e-3)
    assert out.delayed_sec == 900


async def test_chain_summary_defaults_to_nearest_expiration(ctx) -> None:
    out = await get_option_chain_summary(ChainSummaryIn(symbol="SPY"), ctx)
    assert out.expiration == date(2026, 9, 10)
    assert out.available_expirations == [date(2026, 9, 10), date(2026, 9, 11), date(2026, 9, 18)]
    assert out.top_call_oi[0].strike in {585.0, 590.0}
    assert out.top_put_oi[0].strike in {575.0, 570.0}
    assert out.put_call_oi_ratio == pytest.approx(out.total_put_oi / out.total_call_oi, abs=1e-4)
    assert out.atm_strike == 581.0


async def test_chain_summary_for_specific_expiration(ctx) -> None:
    out = await get_option_chain_summary(
        ChainSummaryIn(symbol="SPY", expiration=date(2026, 9, 18)), ctx
    )
    assert out.expiration == date(2026, 9, 18)


async def test_chain_summary_unknown_expiration(ctx) -> None:
    with pytest.raises(ProviderError) as info:
        await get_option_chain_summary(
            ChainSummaryIn(symbol="SPY", expiration=date(2026, 12, 18)), ctx
        )
    assert info.value.code == "expiration_not_found"


async def test_bars_summary(ctx) -> None:
    out = await get_bars(BarsIn(symbol="SPY"), ctx)
    assert out.bar_count == 51
    assert len(out.last_bars) == 12
    assert out.last == 581.20
    assert out.session_low <= out.vwap <= out.session_high


@pytest.mark.parametrize("symbols", [[], ["BAD SYMBOL"], ["1SPY"], ["A" * 11]])
def test_symbol_validation(symbols: list[str]) -> None:
    with pytest.raises(ValidationError):
        QuoteIn(symbols=symbols)


def test_inputs_forbid_extra_fields() -> None:
    with pytest.raises(ValidationError):
        BarsIn(symbol="SPY", extra="x")  # type: ignore[call-arg]
