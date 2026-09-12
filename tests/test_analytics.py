from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from socagents.analytics.flow import estimate_flow
from socagents.analytics.gex import bs_gamma, estimate_gex, zero_gamma_level
from socagents.analytics.technicals import atr, compute_technicals, ema, rsi, vwap
from socagents.providers.base import Bar, OptionChain, OptionContract
from socagents.providers.fixture import FixtureProvider

AS_OF = datetime(2026, 9, 10, 17, 45, tzinfo=UTC)


def contract(
    strike: float,
    right: str,
    oi: int,
    *,
    volume: int = 0,
    bid: float = 1.0,
    ask: float = 1.2,
    iv: float = 0.2,
    gamma: float | None = None,
    expiration: date = date(2026, 9, 18),
) -> OptionContract:
    return OptionContract(
        expiration=expiration,
        strike=strike,
        right=right,  # type: ignore[arg-type]
        bid=bid,
        ask=ask,
        volume=volume,
        open_interest=oi,
        iv=iv,
        gamma=gamma,
    )


def chain(contracts: list[OptionContract], spot: float = 100.0) -> OptionChain:
    return OptionChain(
        source="test",
        as_of=AS_OF,
        delayed_sec=0,
        symbol="TEST",
        underlying_price=spot,
        contracts=contracts,
    )


# GEX


def test_bs_gamma_peaks_at_the_money() -> None:
    years = 7 / 365
    assert bs_gamma(100, 100, years, 0.2) > bs_gamma(100, 120, years, 0.2) > 0
    assert bs_gamma(100, 100, years, 0.0) == 0.0


def test_walls_come_from_call_and_put_exposure() -> None:
    est = estimate_gex(
        chain(
            [
                contract(110, "call", 50_000, gamma=0.05),
                contract(105, "call", 5_000, gamma=0.05),
                contract(90, "put", 40_000, gamma=0.05),
                contract(95, "put", 5_000, gamma=0.05),
            ]
        )
    )
    assert est.call_wall == 110
    assert est.put_wall == 90
    assert est.contracts_used == 4
    by_strike = {s.strike: s for s in est.top_strikes}
    assert by_strike[110].net_gex > 0 > by_strike[90].net_gex


def test_regime_follows_net_exposure() -> None:
    assert estimate_gex(chain([contract(100, "call", 10_000)])).regime == "positive"
    assert estimate_gex(chain([contract(100, "put", 10_000)])).regime == "negative"


def test_provider_gamma_is_used_when_present() -> None:
    est = estimate_gex(chain([contract(100, "call", 1_000, gamma=0.05)]))
    assert est.net_gex == pytest.approx(0.05 * 1_000 * 100 * 100 * 100 * 0.01)


def test_zero_gamma_sits_between_put_and_call_open_interest() -> None:
    contracts = [contract(95, "put", 30_000), contract(105, "call", 30_000)]
    flip = zero_gamma_level(contracts, 100.0, AS_OF)
    assert flip is not None
    assert 95 < flip < 105


def test_estimate_on_fixture_chain() -> None:
    import asyncio

    full = asyncio.run(FixtureProvider().option_chain("SPY"))
    est = estimate_gex(full)
    assert est.put_wall is not None and est.call_wall is not None
    assert est.put_wall < full.underlying_price < est.call_wall
    assert est.zero_gamma is not None
    assert est.put_wall < est.zero_gamma < est.call_wall


def test_estimate_needs_open_interest() -> None:
    with pytest.raises(ValueError):
        estimate_gex(chain([contract(100, "call", 0)]))


def test_expired_and_far_expirations_are_excluded() -> None:
    est = estimate_gex(
        chain(
            [
                contract(100, "call", 1_000, expiration=date(2026, 9, 9)),
                contract(100, "call", 1_000, expiration=date(2027, 1, 15)),
                contract(100, "call", 1_000),
            ]
        ),
        max_dte=30,
    )
    assert est.expirations_used == [date(2026, 9, 18)]


# Closing print of Fri 2026-09-11 (15:59:59 ET): that day's contracts are done.
CLOSE = datetime(2026, 9, 11, 19, 59, 59, tzinfo=UTC)


def closing_chain() -> OptionChain:
    return chain(
        [
            contract(100, "call", 5_000, volume=9_000, expiration=date(2026, 9, 11)),
            contract(100, "put", 5_000, volume=9_000, expiration=date(2026, 9, 11)),
            contract(100, "call", 1_000, volume=900, expiration=date(2026, 9, 14)),
            contract(100, "put", 1_000, volume=900, expiration=date(2026, 9, 14)),
        ]
    ).model_copy(update={"as_of": CLOSE})


def test_gex_after_the_close_skips_contracts_that_expired_that_day() -> None:
    est = estimate_gex(closing_chain())
    assert est.expirations_used == [date(2026, 9, 14)]
    assert est.contracts_used == 2


def test_flow_after_the_close_skips_contracts_that_expired_that_day() -> None:
    flow = estimate_flow(closing_chain())
    assert flow.expirations_used == [date(2026, 9, 14)]
    assert flow.call_volume == 900


# technicals


def bars(closes: list[float], volume: int = 100) -> list[Bar]:
    start = AS_OF - timedelta(minutes=5 * len(closes))
    return [
        Bar(
            ts=start + timedelta(minutes=5 * i),
            open=c,
            high=c + 0.5,
            low=c - 0.5,
            close=c,
            volume=volume,
        )
        for i, c in enumerate(closes)
    ]


def test_indicator_basics() -> None:
    assert ema([5.0] * 10, 3)[-1] == pytest.approx(5.0)
    assert rsi([float(i) for i in range(20)]) == 100.0
    assert rsi([1.0, 2.0]) is None
    assert atr(bars([10.0] * 20)) == pytest.approx(1.0)
    assert vwap(bars([10.0, 20.0])) == pytest.approx(15.0)


def test_trend_classification() -> None:
    up = compute_technicals(bars([100 + i * 0.5 for i in range(40)]))
    down = compute_technicals(bars([100 - i * 0.5 for i in range(40)]))
    assert up.trend == "up" and up.above_vwap
    assert down.trend == "down" and not down.above_vwap
    assert up.opening_range_high == max(b.high for b in bars([100 + i * 0.5 for i in range(6)]))


def test_technicals_need_bars() -> None:
    with pytest.raises(ValueError):
        compute_technicals([])


# flow


def test_flow_bias_and_unusual_volume() -> None:
    near = date(2026, 9, 11)
    flow = estimate_flow(
        chain(
            [
                contract(105, "call", 100, volume=5_000, bid=2.0, ask=2.2, expiration=near),
                contract(95, "put", 10_000, volume=600, bid=1.0, ask=1.2, expiration=near),
            ]
        )
    )
    assert flow.bias == "call-heavy"
    assert flow.call_premium_usd == pytest.approx(5_000 * 2.1 * 100)
    assert [u.strike for u in flow.unusual] == [105]
    assert flow.unusual[0].vol_oi_ratio == 50.0


def test_flow_without_puts_has_no_ratio() -> None:
    flow = estimate_flow(
        chain([contract(105, "call", 100, volume=10, expiration=date(2026, 9, 11))])
    )
    assert flow.call_put_premium_ratio is None
    assert flow.bias == "call-heavy"


def test_flow_needs_near_expirations() -> None:
    with pytest.raises(ValueError):
        estimate_flow(chain([contract(100, "call", 1, expiration=date(2026, 12, 18))]))
