"""Read-only market data tools. Outputs are compact summaries sized for model context."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints

from socagents.core.market import live_expirations
from socagents.providers.base import Bar, OptionContract
from socagents.tools.registry import ToolRegistry
from socagents.tools.sdk import Tool, ToolContext


def _normalize_symbol(value: object) -> object:
    return value.strip().upper() if isinstance(value, str) else value


Symbol = Annotated[
    str,
    BeforeValidator(_normalize_symbol),
    StringConstraints(pattern=r"^[A-Z][A-Z0-9.]{0,9}$"),
]


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Output(BaseModel):
    source: str
    as_of: datetime
    delayed_sec: int


# get_quote


class QuoteIn(_Input):
    symbols: list[Symbol] = Field(min_length=1, max_length=10, description="Ticker symbols.")


class QuoteView(BaseModel):
    symbol: str
    last: float
    prev_close: float
    change: float
    change_pct: float
    bid: float | None
    ask: float | None
    volume: int | None


class QuoteOut(_Output):
    quotes: list[QuoteView]


async def get_quote(args: QuoteIn, ctx: ToolContext) -> QuoteOut:
    qs = await ctx.provider.quotes(args.symbols)
    views = [
        QuoteView(
            symbol=q.symbol,
            last=q.last,
            prev_close=q.prev_close,
            change=round(q.last - q.prev_close, 4),
            change_pct=round((q.last / q.prev_close - 1) * 100, 4) if q.prev_close else 0.0,
            bid=q.bid,
            ask=q.ask,
            volume=q.volume,
        )
        for q in qs.quotes
    ]
    return QuoteOut(source=qs.source, as_of=qs.as_of, delayed_sec=qs.delayed_sec, quotes=views)


# get_option_chain_summary


class ChainSummaryIn(_Input):
    symbol: Symbol
    expiration: date | None = Field(
        default=None, description="Expiration date. Defaults to the nearest expiration."
    )


class StrikeStat(BaseModel):
    strike: float
    value: int


class ChainSummaryOut(_Output):
    symbol: str
    expiration: date
    available_expirations: list[date]
    underlying_price: float
    total_call_oi: int
    total_put_oi: int
    put_call_oi_ratio: float | None
    total_call_volume: int
    total_put_volume: int
    put_call_volume_ratio: float | None
    top_call_oi: list[StrikeStat]
    top_put_oi: list[StrikeStat]
    top_call_volume: list[StrikeStat]
    top_put_volume: list[StrikeStat]
    atm_strike: float
    atm_iv: float | None
    note: str


def _top(
    contracts: list[OptionContract], key: Literal["open_interest", "volume"], n: int = 3
) -> list[StrikeStat]:
    ranked = sorted(contracts, key=lambda c: getattr(c, key), reverse=True)[:n]
    return [StrikeStat(strike=c.strike, value=getattr(c, key)) for c in ranked]


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


async def get_option_chain_summary(args: ChainSummaryIn, ctx: ToolContext) -> ChainSummaryOut:
    full = await ctx.provider.option_chain(args.symbol)
    expirations = full.expirations()
    if args.expiration is not None:
        expiration = args.expiration
    else:
        expiration = (live_expirations(expirations, full.as_of) or expirations)[0]
    chain = await ctx.provider.option_chain(args.symbol, expiration)
    calls = [c for c in chain.contracts if c.right == "call"]
    puts = [c for c in chain.contracts if c.right == "put"]
    call_oi = sum(c.open_interest for c in calls)
    put_oi = sum(c.open_interest for c in puts)
    call_vol = sum(c.volume for c in calls)
    put_vol = sum(c.volume for c in puts)
    atm = min(chain.contracts, key=lambda c: abs(c.strike - chain.underlying_price))
    atm_ivs = [c.iv for c in chain.contracts if c.strike == atm.strike and c.iv is not None]
    return ChainSummaryOut(
        source=chain.source,
        as_of=chain.as_of,
        delayed_sec=chain.delayed_sec,
        symbol=chain.symbol,
        expiration=expiration,
        available_expirations=expirations,
        underlying_price=chain.underlying_price,
        total_call_oi=call_oi,
        total_put_oi=put_oi,
        put_call_oi_ratio=_ratio(put_oi, call_oi),
        total_call_volume=call_vol,
        total_put_volume=put_vol,
        put_call_volume_ratio=_ratio(put_vol, call_vol),
        top_call_oi=_top(calls, "open_interest"),
        top_put_oi=_top(puts, "open_interest"),
        top_call_volume=_top(calls, "volume"),
        top_put_volume=_top(puts, "volume"),
        atm_strike=atm.strike,
        atm_iv=round(sum(atm_ivs) / len(atm_ivs), 4) if atm_ivs else None,
        note="Open interest is reported once per day; intraday changes appear in volume.",
    )


# get_bars


class BarsIn(_Input):
    symbol: Symbol
    interval: Literal["5m"] = "5m"
    lookback: int = Field(default=78, ge=1, le=390, description="Number of bars.")


class BarsOut(_Output):
    symbol: str
    interval: str
    bar_count: int
    session_open: float
    last: float
    session_high: float
    session_low: float
    vwap: float
    change_from_open_pct: float
    last_bars: list[Bar]


async def get_bars(args: BarsIn, ctx: ToolContext) -> BarsOut:
    series = await ctx.provider.bars(args.symbol, args.interval, args.lookback)
    bars = series.bars
    if not bars:
        raise ValueError(f"No bars for {args.symbol}.")
    volume = sum(b.volume for b in bars)
    typical = sum((b.high + b.low + b.close) / 3 * b.volume for b in bars)
    vwap = typical / volume if volume else bars[-1].close
    first, last = bars[0].open, bars[-1].close
    return BarsOut(
        source=series.source,
        as_of=series.as_of,
        delayed_sec=series.delayed_sec,
        symbol=series.symbol,
        interval=series.interval,
        bar_count=len(bars),
        session_open=first,
        last=last,
        session_high=max(b.high for b in bars),
        session_low=min(b.low for b in bars),
        vwap=round(vwap, 4),
        change_from_open_pct=round((last / first - 1) * 100, 4) if first else 0.0,
        last_bars=bars[-12:],
    )


QUOTE_TOOL: Tool[QuoteIn, QuoteOut] = Tool(
    name="get_quote",
    description="Latest quote, day change, and volume for up to 10 symbols.",
    input_model=QuoteIn,
    output_model=QuoteOut,
    handler=get_quote,
)

CHAIN_TOOL: Tool[ChainSummaryIn, ChainSummaryOut] = Tool(
    name="get_option_chain_summary",
    description=(
        "Option chain summary for one expiration: open interest and volume totals, put/call "
        "ratios, top strikes by open interest and volume, and ATM implied volatility."
    ),
    input_model=ChainSummaryIn,
    output_model=ChainSummaryOut,
    handler=get_option_chain_summary,
    timeout_s=30.0,
)

BARS_TOOL: Tool[BarsIn, BarsOut] = Tool(
    name="get_bars",
    description="Intraday bars with session open, high, low, VWAP, and the last 12 bars.",
    input_model=BarsIn,
    output_model=BarsOut,
    handler=get_bars,
)


def register_market_tools(registry: ToolRegistry) -> ToolRegistry:
    for tool in (QUOTE_TOOL, CHAIN_TOOL, BARS_TOOL):
        registry.register(tool)
    return registry


def default_registry() -> ToolRegistry:
    return register_market_tools(ToolRegistry())
