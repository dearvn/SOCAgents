"""Single-agent question answering over market data tools."""

from __future__ import annotations

import os
import re

from socagents.core.config import Settings
from socagents.core.errors import ConfigError
from socagents.db.store import Store
from socagents.model_gateway.pricing import load_prices
from socagents.model_gateway.router import create_model, parse_model_spec
from socagents.providers import create_provider
from socagents.providers.fixture import FixtureProvider
from socagents.runtime.budget import Budget
from socagents.runtime.native import NativeLoopRuntime, RunResult
from socagents.templates.ask import ask_system_prompt, ask_user_message
from socagents.tools.gateway import ToolGateway
from socagents.tools.market import default_registry

_TICKER_RE = re.compile(r"(?<![A-Za-z0-9])\$?([A-Z]{1,5})(?![A-Za-z0-9])")
_NOT_TICKERS = frozenset(
    {
        "A",
        "I",
        "AM",
        "AN",
        "AND",
        "ARE",
        "AT",
        "ATM",
        "CPI",
        "DO",
        "DOES",
        "DTE",
        "EOD",
        "ET",
        "ETF",
        "FOMC",
        "FOR",
        "GEX",
        "HOW",
        "IN",
        "IS",
        "IT",
        "ITM",
        "IV",
        "ME",
        "MY",
        "NOW",
        "OF",
        "OI",
        "ON",
        "OR",
        "OTM",
        "PM",
        "THE",
        "TO",
        "TODAY",
        "US",
        "USD",
        "VWAP",
        "WHAT",
        "WHO",
        "WHY",
    }
)


def extract_symbols(text: str) -> list[str]:
    found = [m.group(1) for m in _TICKER_RE.finditer(text)]
    return list(dict.fromkeys(s for s in found if s not in _NOT_TICKERS))


async def run_ask(
    *,
    question: str,
    symbols: list[str],
    provider_name: str,
    model_spec: str | None,
    settings: Settings,
    max_steps: int = 6,
) -> RunResult:
    provider = create_provider(provider_name)
    wanted = [s.strip().upper() for s in symbols if s.strip()] or extract_symbols(question)
    if not wanted:
        raise ConfigError("No symbol found in the question. Pass one with --symbol, e.g. -s SPY.")
    if isinstance(provider, FixtureProvider):
        available = provider.available_symbols()
        missing = [s for s in wanted if s not in available]
        if missing:
            raise ConfigError(
                f"No fixture data for {', '.join(missing)}. Available: {', '.join(available)}.",
                code="symbol_not_found",
            )

    spec_text = model_spec or os.environ.get("SOCAGENTS_MODEL")
    if spec_text is None and provider_name == "fixture":
        spec_text = "fixture/scripted"
    if spec_text is None:
        raise ConfigError("Choose a model with --model PROVIDER/MODEL, e.g. ollama/<model>.")

    model = create_model(parse_model_spec(spec_text), settings)
    store = Store(settings.db_path)
    try:
        runtime = NativeLoopRuntime(
            model=model,
            gateway=ToolGateway(default_registry(), store, settings),
            store=store,
            settings=settings,
            budget=Budget(max_steps=max_steps),
            prices=load_prices(settings.home),
            role="ask",
        )
        return await runtime.run(
            kind="ask",
            system=ask_system_prompt(mode=provider.mode, provider=provider.name),
            user_message=ask_user_message(question, wanted),
            provider=provider,
            input_meta={"question": question, "symbols": wanted, "provider": provider_name},
        )
    finally:
        await model.aclose()
        store.close()
