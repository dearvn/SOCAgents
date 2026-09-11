"""Intraday technical indicators. Pure functions over bars."""

from __future__ import annotations

from itertools import pairwise
from typing import Literal

from pydantic import BaseModel

from socagents.providers.base import Bar


class Technicals(BaseModel):
    bar_count: int
    last: float
    vwap: float
    above_vwap: bool
    ema_fast: float
    ema_slow: float
    rsi14: float | None
    atr14: float | None
    session_high: float
    session_low: float
    opening_range_high: float
    opening_range_low: float
    trend: Literal["up", "down", "sideways"]


def vwap(bars: list[Bar]) -> float:
    volume = sum(b.volume for b in bars)
    if volume == 0:
        return bars[-1].close
    return sum((b.high + b.low + b.close) / 3 * b.volume for b in bars) / volume


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder's RSI. ``None`` until there are ``period + 1`` closes."""
    if len(closes) <= period:
        return None
    changes = [b - a for a, b in pairwise(closes)]
    gains = [max(c, 0.0) for c in changes]
    losses = [max(-c, 0.0) for c in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, loss in zip(gains[period:], losses[period:], strict=True):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def atr(bars: list[Bar], period: int = 14) -> float | None:
    if len(bars) <= period:
        return None
    ranges = [
        max(b.high - b.low, abs(b.high - prev.close), abs(b.low - prev.close))
        for prev, b in pairwise(bars)
    ]
    value = sum(ranges[:period]) / period
    for r in ranges[period:]:
        value = (value * (period - 1) + r) / period
    return value


def compute_technicals(bars: list[Bar], *, opening_bars: int = 6) -> Technicals:
    if not bars:
        raise ValueError("No bars.")
    closes = [b.close for b in bars]
    session_vwap = vwap(bars)
    fast = ema(closes, 9)[-1]
    slow = ema(closes, 21)[-1]
    last = closes[-1]
    opening = bars[:opening_bars]
    if last > session_vwap and fast > slow:
        trend: Literal["up", "down", "sideways"] = "up"
    elif last < session_vwap and fast < slow:
        trend = "down"
    else:
        trend = "sideways"
    rsi14 = rsi(closes)
    atr14 = atr(bars)
    return Technicals(
        bar_count=len(bars),
        last=last,
        vwap=round(session_vwap, 4),
        above_vwap=last >= session_vwap,
        ema_fast=round(fast, 4),
        ema_slow=round(slow, 4),
        rsi14=round(rsi14, 2) if rsi14 is not None else None,
        atr14=round(atr14, 4) if atr14 is not None else None,
        session_high=max(b.high for b in bars),
        session_low=min(b.low for b in bars),
        opening_range_high=max(b.high for b in opening),
        opening_range_low=min(b.low for b in opening),
        trend=trend,
    )
