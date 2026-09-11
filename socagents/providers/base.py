"""Market data provider interface and the data models every provider returns."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Protocol

from pydantic import BaseModel, Field

Mode = Literal["community", "member"]


class DataMeta(BaseModel):
    """Provenance attached to every data set: where it came from, when, and how delayed."""

    source: str
    as_of: datetime
    delayed_sec: int = Field(ge=0)


class Quote(BaseModel):
    symbol: str
    last: float
    prev_close: float
    bid: float | None = None
    ask: float | None = None
    volume: int | None = None


class QuoteSet(DataMeta):
    quotes: list[Quote]


class OptionContract(BaseModel):
    expiration: date
    strike: float
    right: Literal["call", "put"]
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    volume: int = 0
    open_interest: int = 0
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None


class OptionChain(DataMeta):
    symbol: str
    underlying_price: float
    contracts: list[OptionContract]

    def expirations(self) -> list[date]:
        return sorted({c.expiration for c in self.contracts})


class Bar(BaseModel):
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


class BarSeries(DataMeta):
    symbol: str
    interval: str
    bars: list[Bar]


class Headline(BaseModel):
    title: str
    source: str
    url: str | None = None
    published_at: datetime | None = None


class HeadlineSet(DataMeta):
    symbol: str
    headlines: list[Headline]


class EconomicEvent(BaseModel):
    time: datetime
    name: str
    importance: Literal["low", "medium", "high"] = "medium"
    country: str = "US"


class EventSet(DataMeta):
    events: list[EconomicEvent]


class MarketDataProvider(Protocol):
    name: str
    mode: Mode

    async def quotes(self, symbols: list[str]) -> QuoteSet: ...

    async def option_chain(self, symbol: str, expiration: date | None = None) -> OptionChain: ...

    async def bars(self, symbol: str, interval: str = "5m", lookback: int = 78) -> BarSeries: ...

    async def headlines(self, symbol: str, limit: int = 10) -> HeadlineSet: ...

    async def events(self, hours: int = 48) -> EventSet: ...

    async def aclose(self) -> None: ...
