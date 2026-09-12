"""Options flow estimate from a public chain: traded premium and unusual volume.

Free chains show only totals for the day, so this cannot tell buyers from sellers or see
block trades. It is a rough proxy for where volume concentrates.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel

from socagents.core.market import expiration_is_live
from socagents.core.timeutil import ET
from socagents.providers.base import OptionChain, OptionContract

CONTRACT_SIZE = 100
METHOD = (
    "Volume × mid price × 100 from a delayed public chain. Buyers and sellers cannot be told "
    "apart, so this is a proxy, not institutional flow."
)


class UnusualContract(BaseModel):
    expiration: date
    strike: float
    right: Literal["call", "put"]
    volume: int
    open_interest: int
    vol_oi_ratio: float | None
    premium_usd: float


class FlowEstimate(BaseModel):
    symbol: str
    expirations_used: list[date]
    call_volume: int
    put_volume: int
    call_premium_usd: float
    put_premium_usd: float
    call_put_premium_ratio: float | None
    bias: Literal["call-heavy", "put-heavy", "balanced"]
    unusual: list[UnusualContract]
    method: str = METHOD


def mid_price(c: OptionContract) -> float:
    if c.bid is not None and c.ask is not None and c.ask >= c.bid:
        return (c.bid + c.ask) / 2
    return c.last or 0.0


def estimate_flow(
    chain: OptionChain,
    *,
    max_dte: int = 7,
    min_volume: int = 500,
    min_vol_oi: float = 1.0,
    top_n: int = 5,
) -> FlowEstimate:
    today = chain.as_of.astimezone(ET).date()
    contracts = [
        c
        for c in chain.contracts
        if expiration_is_live(c.expiration, chain.as_of) and (c.expiration - today).days <= max_dte
    ]
    if not contracts:
        raise ValueError(f"No {chain.symbol} contracts expire within {max_dte} days.")

    def premium(c: OptionContract) -> float:
        return c.volume * mid_price(c) * CONTRACT_SIZE

    calls = [c for c in contracts if c.right == "call"]
    puts = [c for c in contracts if c.right == "put"]
    call_premium = sum(premium(c) for c in calls)
    put_premium = sum(premium(c) for c in puts)
    ratio = call_premium / put_premium if put_premium else None
    if ratio is not None and ratio >= 1.25:
        bias: Literal["call-heavy", "put-heavy", "balanced"] = "call-heavy"
    elif ratio is not None and ratio <= 0.8:
        bias = "put-heavy"
    else:
        bias = "balanced" if ratio is not None else "call-heavy" if call_premium else "balanced"

    unusual = [
        c
        for c in contracts
        if c.volume >= min_volume
        and (c.open_interest == 0 or c.volume / c.open_interest >= min_vol_oi)
    ]
    unusual.sort(key=premium, reverse=True)
    return FlowEstimate(
        symbol=chain.symbol,
        expirations_used=sorted({c.expiration for c in contracts}),
        call_volume=sum(c.volume for c in calls),
        put_volume=sum(c.volume for c in puts),
        call_premium_usd=round(call_premium, 2),
        put_premium_usd=round(put_premium, 2),
        call_put_premium_ratio=round(ratio, 4) if ratio is not None else None,
        bias=bias,
        unusual=[
            UnusualContract(
                expiration=c.expiration,
                strike=c.strike,
                right=c.right,
                volume=c.volume,
                open_interest=c.open_interest,
                vol_oi_ratio=round(c.volume / c.open_interest, 2) if c.open_interest else None,
                premium_usd=round(premium(c), 2),
            )
            for c in unusual[:top_n]
        ],
    )
