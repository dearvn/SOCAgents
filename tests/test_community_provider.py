from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from socagents.core.errors import ProviderError, SymbolNotFound
from socagents.providers.community import (
    CommunityProvider,
    parse_cboe_chain,
    parse_rss,
    parse_yahoo_bars,
)

CBOE_DOC = {
    "timestamp": "2026-09-10 13:45:00",
    "data": {
        "current_price": 581.2,
        "prev_day_close": 578.77,
        "bid": 581.19,
        "ask": 581.21,
        "volume": 1_000_000,
        "options": [
            {
                "option": "SPY260910C00582000",
                "bid": 0.2,
                "ask": 0.22,
                "iv": 0.14,
                "open_interest": 1000,
                "volume": 500,
                "delta": 0.4,
                "gamma": 0.08,
                "last_trade_price": 0.21,
            },
            {
                "option": "SPY260911P00580500",
                "bid": 1.0,
                "ask": 1.1,
                "iv": 0.0,
                "open_interest": 2000.0,
                "volume": None,
                "delta": -0.3,
                "gamma": 0.05,
            },
            {"option": "NOT_AN_OCC_SYMBOL"},
        ],
    },
}

YAHOO_DOC = {
    "chart": {
        "result": [
            {
                "meta": {"regularMarketTime": 1789062300},
                "timestamp": [1789047000, 1789047300, 1789047600],
                "indicators": {
                    "quote": [
                        {
                            "open": [580.0, 580.5, None],
                            "high": [581.0, 581.2, None],
                            "low": [579.5, 580.1, None],
                            "close": [580.5, 581.0, None],
                            "volume": [1000, 2000, None],
                        }
                    ]
                },
            }
        ],
        "error": None,
    }
}

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>t</title>
<item><title>Stocks edge higher</title><link>https://example.com/a</link>
<pubDate>Thu, 10 Sep 2026 16:05:00 +0000</pubDate></item>
<item><title>  </title></item>
<item><title>Yields steady</title><pubDate>not a date</pubDate></item>
</channel></rss>"""


class Router:
    def __init__(self, routes: dict[str, httpx.Response]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for fragment, response in self.routes.items():
            if fragment in str(request.url):
                return response
        return httpx.Response(404)


def provider(routes: dict[str, httpx.Response], **kwargs: Any) -> tuple[CommunityProvider, Router]:
    router = Router(routes)
    return CommunityProvider(transport=httpx.MockTransport(router), **kwargs), router


def test_parse_cboe_chain() -> None:
    chain = parse_cboe_chain(CBOE_DOC, "SPY")
    assert chain.as_of == datetime(2026, 9, 10, 17, 45, tzinfo=UTC)
    assert chain.underlying_price == 581.2
    assert len(chain.contracts) == 2
    call, put = chain.contracts
    assert (call.expiration, call.strike, call.right) == (date(2026, 9, 10), 582.0, "call")
    assert call.gamma == 0.08
    assert (put.strike, put.right, put.volume, put.iv) == (580.5, "put", 0, None)


def test_parse_cboe_chain_without_options() -> None:
    with pytest.raises(SymbolNotFound):
        parse_cboe_chain({"data": {"current_price": 1.0, "options": []}}, "X")


def test_parse_yahoo_bars_skips_incomplete_rows() -> None:
    series = parse_yahoo_bars(YAHOO_DOC, "SPY", "5m")
    assert len(series.bars) == 2
    assert series.bars[0].ts == datetime.fromtimestamp(1789047000, UTC)
    assert series.as_of == datetime.fromtimestamp(1789062300, UTC)
    assert series.delayed_sec == 900


def test_parse_yahoo_error() -> None:
    with pytest.raises(SymbolNotFound):
        parse_yahoo_bars({"chart": {"result": None, "error": {"code": "Not Found"}}}, "X", "5m")


def test_parse_rss() -> None:
    items = parse_rss(RSS)
    assert [h.title for h in items] == ["Stocks edge higher", "Yields steady"]
    assert items[0].published_at == datetime(2026, 9, 10, 16, 5, tzinfo=UTC)
    assert items[1].published_at is None


def test_parse_rss_rejects_garbage() -> None:
    with pytest.raises(ProviderError):
        parse_rss("<rss><unclosed>")


async def test_quotes_and_chain_share_one_cached_request() -> None:
    p, router = provider({"/options/SPY.json": httpx.Response(200, json=CBOE_DOC)})
    quotes = await p.quotes(["spy"])
    chain = await p.option_chain("SPY", date(2026, 9, 10))
    await p.aclose()
    assert quotes.quotes[0].last == 581.2
    assert quotes.source == "Cboe delayed quotes"
    assert len(chain.contracts) == 1
    assert len(router.requests) == 1


async def test_index_symbols_map_to_source_symbols() -> None:
    p, router = provider(
        {
            "/options/_SPX.json": httpx.Response(200, json=CBOE_DOC),
            "GSPC": httpx.Response(200, json=YAHOO_DOC),
        }
    )
    await p.quotes(["SPX"])
    await p.bars("SPX")
    await p.aclose()
    assert "_SPX.json" in str(router.requests[0].url)
    assert router.requests[1].url.path.endswith("/chart/^GSPC")


async def test_bars_and_headlines() -> None:
    p, router = provider(
        {
            "/chart/SPY": httpx.Response(200, json=YAHOO_DOC),
            "rss/2.0/headline": httpx.Response(200, text=RSS),
        }
    )
    series = await p.bars("SPY", "5m", lookback=1)
    headlines = await p.headlines("SPY", limit=1)
    await p.aclose()
    assert len(series.bars) == 1
    assert [h.title for h in headlines.headlines] == ["Stocks edge higher"]
    assert router.requests[0].url.params["interval"] == "5m"


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(429), "rate_limited"),
        (httpx.Response(500), "source_unavailable"),
        (httpx.Response(404), "symbol_not_found"),
    ],
)
async def test_http_errors_map_to_codes(response: httpx.Response, code: str) -> None:
    p, _ = provider({"/options/SPY.json": response})
    with pytest.raises(ProviderError) as info:
        await p.quotes(["SPY"])
    await p.aclose()
    assert info.value.code == code


async def test_network_errors_map_to_source_unavailable() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    p = CommunityProvider(transport=httpx.MockTransport(boom))
    with pytest.raises(ProviderError) as info:
        await p.option_chain("SPY")
    await p.aclose()
    assert info.value.code == "source_unavailable"


async def test_invalid_interval() -> None:
    p, _ = provider({})
    with pytest.raises(ProviderError):
        await p.bars("SPY", "4h")
    await p.aclose()


async def test_events_come_from_the_user_calendar(tmp_path: Path) -> None:
    soon = datetime.now(UTC) + timedelta(hours=2)
    later = datetime.now(UTC) + timedelta(days=10)
    path = tmp_path / "calendar.json"
    path.write_text(
        json.dumps(
            [
                {"time": soon.isoformat(), "name": "CPI", "importance": "high"},
                {"time": later.isoformat(), "name": "Far away"},
            ]
        )
    )
    p, _ = provider({}, calendar_path=path)
    events = await p.events(48)
    empty = await CommunityProvider().events(48)
    await p.aclose()
    assert [e.name for e in events.events] == ["CPI"]
    assert empty.events == [] and empty.source == "no calendar configured"
