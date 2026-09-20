"""Community provider: free, delayed public data for personal use.

Sources:
- Cboe delayed quotes: quotes and option chains with greeks (delayed about 15 minutes).
- Yahoo Finance: intraday bars and headlines.
- An optional user calendar file, ``$SOCAGENTS_HOME/calendar.json``, for economic events.

The data runs on your machine for your own use. Respect each source's terms. SOCAgents and
SocSwift never republish it.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import httpx

from socagents import __version__
from socagents.core.errors import ProviderError, SymbolNotFound
from socagents.core.timeutil import ET, next_session_open, utcnow
from socagents.providers.base import (
    Bar,
    BarSeries,
    EconomicEvent,
    EventSet,
    Headline,
    HeadlineSet,
    Mode,
    OptionChain,
    OptionContract,
    Quote,
    QuoteSet,
)

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"
# The options file carries the full chain (about 6 MB for SPY); quotes use the small file.
CBOE_QUOTE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/{symbol}.json"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_RSS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline"
CBOE_SOURCE = "Cboe delayed quotes"
YAHOO_SOURCE = "Yahoo Finance"
DELAY_SEC = 900  # treat all free data as delayed up to 15 minutes
MAX_TEXT_BYTES = 2_000_000
BAR_INTERVALS = {"1m", "5m", "15m"}

INDEX_CBOE = {"SPX": "_SPX", "NDX": "_NDX", "RUT": "_RUT", "VIX": "_VIX", "XSP": "_XSP"}
INDEX_YAHOO = {"SPX": "^GSPC", "NDX": "^NDX", "RUT": "^RUT", "VIX": "^VIX"}
OCC_RE = re.compile(r"^(?P<root>[A-Z]{1,6})(?P<date>\d{6})(?P<right>[CP])(?P<strike>\d{8})$")


class CommunityProvider:
    name = "community"
    mode: Mode = "community"

    def __init__(
        self,
        *,
        calendar_path: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        cache_ttl_s: float = 60.0,
    ) -> None:
        self._calendar_path = calendar_path
        self._cache_ttl_s = cache_ttl_s
        self._cache: dict[str, tuple[float, Any]] = {}
        self._inflight: dict[str, asyncio.Future[Any]] = {}
        self._client = httpx.AsyncClient(
            timeout=25.0,
            transport=transport,
            follow_redirects=True,
            headers={
                "user-agent": f"socagents/{__version__} (+https://github.com/dearvn/SOCAgents)"
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(
        self, url: str, params: dict[str, str] | None = None, *, text: bool = False
    ) -> Any:
        key = f"{url}?{json.dumps(params, sort_keys=True)}"
        cached = self._cache.get(key)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]
        # Concurrent callers share one download. The shield keeps a caller's timeout from
        # cancelling the download for the others.
        pending = self._inflight.get(key)
        if pending is None:
            pending = asyncio.ensure_future(self._fetch(key, url, params, text=text))
            self._inflight[key] = pending
            pending.add_done_callback(lambda done: self._settle(key, done))
        return await asyncio.shield(pending)

    def _settle(self, key: str, done: asyncio.Future[Any]) -> None:
        self._inflight.pop(key, None)
        if not done.cancelled():
            done.exception()  # mark as retrieved when every caller has gone

    async def _fetch(self, key: str, url: str, params: dict[str, str] | None, *, text: bool) -> Any:
        host = httpx.URL(url).host
        try:
            response = await self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"{host} is unreachable ({type(exc).__name__}).", code="source_unavailable"
            ) from exc
        if response.status_code in (403, 404):
            raise SymbolNotFound(f"{host} has no data for this symbol.")
        if response.status_code == 429:
            raise ProviderError(
                f"{host} rate limit reached. Try again in a minute.", code="rate_limited"
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"{host} returned HTTP {response.status_code}.", code="source_unavailable"
            )
        if text:
            value: Any = response.text[:MAX_TEXT_BYTES]
        else:
            try:
                value = response.json()
            except ValueError as exc:
                raise ProviderError(
                    f"{host} returned invalid JSON.", code="source_unavailable"
                ) from exc
        self._cache[key] = (time.monotonic() + self._cache_ttl_s, value)
        return value

    async def _cboe(self, symbol: str, url: str = CBOE_URL) -> dict[str, Any]:
        doc: dict[str, Any] = await self._get(url.format(symbol=INDEX_CBOE.get(symbol, symbol)))
        return doc

    async def quotes(self, symbols: list[str]) -> QuoteSet:
        if not symbols:
            raise ProviderError("At least one symbol is required.")
        docs = await asyncio.gather(*(self._cboe(s.upper(), CBOE_QUOTE_URL) for s in symbols))
        return QuoteSet(
            source=CBOE_SOURCE,
            as_of=min(parse_cboe_timestamp(d) for d in docs),
            delayed_sec=DELAY_SEC,
            quotes=[parse_cboe_quote(d, s.upper()) for d, s in zip(docs, symbols, strict=True)],
        )

    async def option_chain(self, symbol: str, expiration: date | None = None) -> OptionChain:
        chain = parse_cboe_chain(await self._cboe(symbol.upper()), symbol.upper())
        if expiration is None:
            return chain
        contracts = [c for c in chain.contracts if c.expiration == expiration]
        if not contracts:
            raise ProviderError(
                f"No {symbol.upper()} contracts expire on {expiration.isoformat()}.",
                code="expiration_not_found",
            )
        return chain.model_copy(update={"contracts": contracts})

    async def bars(self, symbol: str, interval: str = "5m", lookback: int = 78) -> BarSeries:
        if interval not in BAR_INTERVALS:
            raise ProviderError(
                f"Interval must be one of {sorted(BAR_INTERVALS)}.", code="interval_not_available"
            )
        yahoo_symbol = INDEX_YAHOO.get(symbol.upper(), symbol.upper())
        doc = await self._get(
            YAHOO_CHART_URL.format(symbol=yahoo_symbol), {"interval": interval, "range": "1d"}
        )
        series = parse_yahoo_bars(doc, symbol.upper(), interval)
        return series.model_copy(update={"bars": series.bars[-lookback:]})

    async def headlines(self, symbol: str, limit: int = 10) -> HeadlineSet:
        yahoo_symbol = INDEX_YAHOO.get(symbol.upper(), symbol.upper())
        text = await self._get(
            YAHOO_RSS_URL, {"s": yahoo_symbol, "region": "US", "lang": "en-US"}, text=True
        )
        return HeadlineSet(
            source=YAHOO_SOURCE,
            as_of=utcnow(),
            delayed_sec=0,
            symbol=symbol.upper(),
            headlines=parse_rss(text)[:limit],
        )

    async def events(self, hours: int = 48) -> EventSet:
        now = utcnow()
        events: list[EconomicEvent] = []
        source = "no calendar configured"
        if self._calendar_path is not None and self._calendar_path.is_file():
            source = f"user calendar ({self._calendar_path.name})"
            try:
                raw = json.loads(self._calendar_path.read_text(encoding="utf-8"))
                events = [EconomicEvent.model_validate(e) for e in raw]
            except ValueError as exc:
                raise ProviderError(
                    f"Invalid calendar file: {exc}", code="invalid_calendar"
                ) from exc
        end = now + timedelta(hours=hours)
        if now.astimezone(ET).weekday() >= 5:  # Sat/Sun: a fixed window can miss Monday's open
            end = max(end, next_session_open(now) + timedelta(hours=hours))
        return EventSet(
            source=source,
            as_of=now,
            delayed_sec=0,
            events=[e for e in events if now <= e.time <= end],
        )


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _int(value: Any) -> int:
    number = _float(value)
    return int(number) if number is not None else 0


def parse_cboe_timestamp(doc: dict[str, Any]) -> datetime:
    """Time of the last trade. The file timestamp only says when Cboe wrote the file, which
    after the close or on a weekend is hours or days after the last trade."""
    data = doc.get("data") or {}
    for raw, fmt in (
        (data.get("last_trade_time"), "%Y-%m-%dT%H:%M:%S"),
        (doc.get("timestamp"), "%Y-%m-%d %H:%M:%S"),
    ):
        if isinstance(raw, str):
            try:
                return datetime.strptime(raw, fmt).replace(tzinfo=ET).astimezone(UTC)
            except ValueError:
                continue
    return utcnow()


def parse_cboe_quote(doc: dict[str, Any], symbol: str) -> Quote:
    data = doc.get("data") or {}
    last = _float(data.get("current_price"))
    if last is None:
        raise SymbolNotFound(f"Cboe has no quote for {symbol}.")
    return Quote(
        symbol=symbol,
        last=last,
        prev_close=_float(data.get("prev_day_close")) or last,
        bid=_float(data.get("bid")),
        ask=_float(data.get("ask")),
        volume=_int(data.get("volume")) or None,
    )


def parse_cboe_chain(doc: dict[str, Any], symbol: str) -> OptionChain:
    quote = parse_cboe_quote(doc, symbol)
    contracts: list[OptionContract] = []
    for item in (doc.get("data") or {}).get("options") or []:
        match = OCC_RE.match(str(item.get("option", "")))
        if not match:
            continue
        raw_date = match["date"]
        try:
            expiration = date(2000 + int(raw_date[:2]), int(raw_date[2:4]), int(raw_date[4:]))
        except ValueError:
            continue
        iv = _float(item.get("iv"))
        contracts.append(
            OptionContract(
                expiration=expiration,
                strike=int(match["strike"]) / 1000,
                right="call" if match["right"] == "C" else "put",
                bid=_float(item.get("bid")),
                ask=_float(item.get("ask")),
                last=_float(item.get("last_trade_price")),
                volume=_int(item.get("volume")),
                open_interest=_int(item.get("open_interest")),
                iv=iv if iv else None,
                delta=_float(item.get("delta")),
                gamma=_float(item.get("gamma")),
            )
        )
    if not contracts:
        raise SymbolNotFound(f"Cboe has no listed options for {symbol}.")
    return OptionChain(
        source=CBOE_SOURCE,
        as_of=parse_cboe_timestamp(doc),
        delayed_sec=DELAY_SEC,
        symbol=symbol,
        underlying_price=quote.last,
        contracts=contracts,
    )


def parse_yahoo_bars(doc: dict[str, Any], symbol: str, interval: str) -> BarSeries:
    chart = doc.get("chart") or {}
    results = chart.get("result") or []
    if not results:
        raise SymbolNotFound(f"Yahoo Finance has no bars for {symbol}.")
    result = results[0]
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    bars: list[Bar] = []
    for i, ts in enumerate(timestamps):
        values = [
            _float((quote.get(k) or [None] * len(timestamps))[i])
            for k in ("open", "high", "low", "close")
        ]
        if any(v is None for v in values):
            continue
        o, h, lo, c = (v for v in values if v is not None)
        volume = _int((quote.get("volume") or [0] * len(timestamps))[i])
        bars.append(
            Bar(ts=datetime.fromtimestamp(ts, UTC), open=o, high=h, low=lo, close=c, volume=volume)
        )
    if not bars:
        raise SymbolNotFound(f"Yahoo Finance returned no complete bars for {symbol}.")
    market_time = (result.get("meta") or {}).get("regularMarketTime")
    as_of = datetime.fromtimestamp(market_time, UTC) if market_time else bars[-1].ts
    return BarSeries(
        source=YAHOO_SOURCE,
        as_of=as_of,
        delayed_sec=DELAY_SEC,
        symbol=symbol,
        interval=interval,
        bars=bars,
    )


def parse_rss(text: str) -> list[Headline]:
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise ProviderError("Headline feed is not valid RSS.", code="source_unavailable") from exc
    headlines: list[Headline] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        published: datetime | None = None
        raw_date = item.findtext("pubDate")
        if raw_date:
            try:
                published = parsedate_to_datetime(raw_date).astimezone(UTC)
            except (TypeError, ValueError):
                published = None
        headlines.append(
            Headline(
                title=title[:300],
                source=YAHOO_SOURCE,
                url=(item.findtext("link") or "").strip() or None,
                published_at=published,
            )
        )
    return headlines
