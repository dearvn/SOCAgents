"""SocSwift member provider: real-time SocSwift data through the Agent API.

Headlines and the event calendar come from the Community provider until SocSwift serves them.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from socagents.core.timeutil import iso, utcnow
from socagents.providers.base import (
    Bar,
    BarSeries,
    EventSet,
    HeadlineSet,
    MarketDataProvider,
    Mode,
    OptionChain,
    OptionContract,
    Quote,
    QuoteSet,
)
from socagents.socswift_client.client import MemberInfo, SocSwiftClient

SOURCE = "SocSwift"


def meta(payload: dict[str, Any]) -> dict[str, Any]:
    delayed = bool(payload.get("delayed", False))
    return {
        "source": SOURCE,
        "as_of": payload.get("as_of") or iso(utcnow()),
        "delayed_sec": int(payload.get("delayed_sec", 900 if delayed else 0)),
    }


class SocSwiftProvider:
    name = "socswift"
    mode: Mode = "member"

    def __init__(
        self, client: SocSwiftClient, member: MemberInfo, fallback: MarketDataProvider
    ) -> None:
        self.client = client
        self.member = member
        self._fallback = fallback

    async def aclose(self) -> None:
        await self.client.aclose()
        await self._fallback.aclose()

    async def quotes(self, symbols: list[str]) -> QuoteSet:
        data = await self.client.quotes(symbols)
        return QuoteSet(
            **meta(data), quotes=[Quote.model_validate(q) for q in data.get("quotes", [])]
        )

    async def option_chain(self, symbol: str, expiration: date | None = None) -> OptionChain:
        data = await self.client.option_chain(
            symbol, expiration.isoformat() if expiration else None
        )
        return OptionChain(
            **meta(data),
            symbol=symbol.upper(),
            underlying_price=float(data["underlying_price"]),
            contracts=[OptionContract.model_validate(c) for c in data.get("contracts", [])],
        )

    async def bars(self, symbol: str, interval: str = "5m", lookback: int = 78) -> BarSeries:
        data = await self.client.bars(symbol, interval, lookback)
        return BarSeries(
            **meta(data),
            symbol=symbol.upper(),
            interval=interval,
            bars=[Bar.model_validate(b) for b in data.get("bars", [])],
        )

    async def headlines(self, symbol: str, limit: int = 10) -> HeadlineSet:
        return await self._fallback.headlines(symbol, limit)

    async def events(self, hours: int = 48) -> EventSet:
        return await self._fallback.events(hours)

    async def member_gex(self, symbol: str) -> dict[str, Any]:
        return await self.client.gex(symbol)

    async def member_flow(self, symbol: str, dte: str, limit: int) -> dict[str, Any]:
        return await self.client.flow(symbol, dte, limit)

    async def member_hedge_flow(self, family: str) -> dict[str, Any]:
        return await self.client.hedge_flow(family)
