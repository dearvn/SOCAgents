"""Open-interest GEX estimate from an option chain.

This is a common public approximation, not SocSwift's GEX engine:
- gamma exposure per contract = gamma × open interest × 100 × spot² × 0.01, in USD of
  delta hedging per 1% move;
- call exposure counts as positive and put exposure as negative (dealers assumed long calls
  and short puts);
- the zero-gamma level is where total exposure changes sign as spot moves, re-pricing gamma
  with Black-Scholes at each hypothetical spot.

Open interest updates once a day and dealer positioning is assumed, so the output is always an
estimate.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time
from itertools import pairwise
from typing import Literal

from pydantic import BaseModel

from socagents.core.market import expiration_is_live
from socagents.core.timeutil import ET
from socagents.providers.base import OptionChain, OptionContract

CONTRACT_SIZE = 100
MIN_YEARS = 1 / (365 * 24)  # one hour
METHOD = (
    "Open-interest estimate: calls positive, puts negative, gamma × OI × 100 × spot² × 1%. "
    "Dealer positioning is assumed; open interest updates once a day."
)


class StrikeGex(BaseModel):
    strike: float
    call_gex: float
    put_gex: float
    net_gex: float


class GexEstimate(BaseModel):
    symbol: str
    spot: float
    expirations_used: list[date]
    contracts_used: int
    net_gex: float
    regime: Literal["positive", "negative"]
    call_wall: float | None
    put_wall: float | None
    zero_gamma: float | None
    top_strikes: list[StrikeGex]
    method: str = METHOD


def years_to_expiry(expiration: date, as_of: datetime) -> float:
    close = datetime.combine(expiration, time(16, 0), tzinfo=ET)
    return max((close - as_of).total_seconds() / (365 * 24 * 3600), MIN_YEARS)


def bs_gamma(spot: float, strike: float, years: float, iv: float) -> float:
    if spot <= 0 or strike <= 0 or iv <= 0 or years <= 0:
        return 0.0
    vol_t = iv * math.sqrt(years)
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * years) / vol_t
    return math.exp(-0.5 * d1 * d1) / (math.sqrt(2 * math.pi) * spot * vol_t)


def _exposure(contract: OptionContract, spot: float, gamma: float) -> float:
    sign = 1.0 if contract.right == "call" else -1.0
    return sign * gamma * contract.open_interest * CONTRACT_SIZE * spot * spot * 0.01


def eligible_contracts(chain: OptionChain, max_dte: int) -> list[OptionContract]:
    today = chain.as_of.astimezone(ET).date()
    return [
        c
        for c in chain.contracts
        if c.open_interest > 0
        and expiration_is_live(c.expiration, chain.as_of)
        and (c.expiration - today).days <= max_dte
    ]


def total_gex_at(contracts: list[OptionContract], spot: float, as_of: datetime) -> float:
    total = 0.0
    for c in contracts:
        if c.iv:
            gamma = bs_gamma(spot, c.strike, years_to_expiry(c.expiration, as_of), c.iv)
            total += _exposure(c, spot, gamma)
    return total


def zero_gamma_level(
    contracts: list[OptionContract],
    spot: float,
    as_of: datetime,
    *,
    width: float = 0.08,
    steps: int = 161,
) -> float | None:
    """Spot level nearest to the current spot where total exposure changes sign."""
    near = [c for c in contracts if c.iv and abs(c.strike / spot - 1) <= 0.2]
    if not near:
        return None
    grid = [spot * (1 - width + 2 * width * i / (steps - 1)) for i in range(steps)]
    values = [total_gex_at(near, s, as_of) for s in grid]
    crossings: list[float] = []
    for (s0, v0), (s1, v1) in pairwise(zip(grid, values, strict=True)):
        if v0 == 0:
            crossings.append(s0)
        elif v0 * v1 < 0:
            crossings.append(s0 + (s1 - s0) * (-v0) / (v1 - v0))
    if not crossings:
        return None
    nearest: float = min(crossings, key=lambda s: abs(s - spot))
    return round(nearest, 2)


def estimate_gex(chain: OptionChain, *, max_dte: int = 30, top_n: int = 5) -> GexEstimate:
    contracts = eligible_contracts(chain, max_dte)
    if not contracts:
        raise ValueError(f"No {chain.symbol} contracts with open interest within {max_dte} days.")
    spot = chain.underlying_price
    by_strike: dict[float, list[float]] = {}
    for c in contracts:
        if c.gamma is not None:
            gamma = c.gamma
        elif c.iv:
            gamma = bs_gamma(spot, c.strike, years_to_expiry(c.expiration, chain.as_of), c.iv)
        else:
            continue
        exposure = _exposure(c, spot, gamma)
        slot = by_strike.setdefault(c.strike, [0.0, 0.0])
        slot[0 if c.right == "call" else 1] += exposure

    strikes = [
        StrikeGex(
            strike=k, call_gex=round(v[0], 2), put_gex=round(v[1], 2), net_gex=round(v[0] + v[1], 2)
        )
        for k, v in sorted(by_strike.items())
    ]
    net = sum(s.net_gex for s in strikes)
    calls = [s for s in strikes if s.call_gex > 0]
    puts = [s for s in strikes if s.put_gex < 0]
    return GexEstimate(
        symbol=chain.symbol,
        spot=spot,
        expirations_used=sorted({c.expiration for c in contracts}),
        contracts_used=len(contracts),
        net_gex=round(net, 2),
        regime="positive" if net >= 0 else "negative",
        call_wall=max(calls, key=lambda s: s.call_gex).strike if calls else None,
        put_wall=min(puts, key=lambda s: s.put_gex).strike if puts else None,
        zero_gamma=zero_gamma_level(contracts, spot, chain.as_of),
        top_strikes=sorted(strikes, key=lambda s: abs(s.net_gex), reverse=True)[:top_n],
    )
