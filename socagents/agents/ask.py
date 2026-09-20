"""Single-agent question answering and briefings over market data tools."""

from __future__ import annotations

import re
from collections.abc import Callable

from socagents.core.config import Settings
from socagents.core.errors import ConfigError
from socagents.model_gateway.pricing import load_prices
from socagents.model_gateway.router import create_model, parse_model_spec
from socagents.providers.fixture import FixtureProvider
from socagents.runtime.budget import Budget
from socagents.runtime.native import NativeLoopRuntime, RunResult
from socagents.session import open_session, resolve_model_spec
from socagents.templates.ask import ask_system_prompt, ask_user_message, brief_system_prompt
from socagents.templates.x import x_reply_system_prompt
from socagents.tools.catalog import full_registry
from socagents.tools.gateway import ToolGateway

_TICKER_RE = re.compile(r"(?<![A-Za-z0-9])\$?([A-Z]{1,5})(?![A-Za-z0-9])")
_NOT_TICKERS = frozenset(
    {
        "A", "I", "AM", "AN", "AND", "ARE", "AT", "ATM", "CPI", "DO", "DOES", "DTE", "EOD", "ET",
        "ETF", "FOMC", "FOR", "GEX", "HOW", "IN", "IS", "IT", "ITM", "IV", "ME", "MY", "NOW",
        "OF", "OI", "ON", "OR", "OTM", "PM", "THE", "TO", "TODAY", "US", "USD", "VWAP", "WHAT",
        "WHO", "WHY",
    }
)  # fmt: skip

# System prompt per run kind. Anything not listed gets the plain ask prompt.
_SYSTEM_PROMPTS: dict[str, Callable[..., str]] = {
    "brief": brief_system_prompt,
    "x_reply": x_reply_system_prompt,
}


def extract_symbols(text: str) -> list[str]:
    found = [m.group(1) for m in _TICKER_RE.finditer(text)]
    return list(dict.fromkeys(s for s in found if s not in _NOT_TICKERS))


async def run_ask(
    *,
    question: str,
    symbols: list[str],
    provider_name: str | None,
    model_spec: str | None,
    settings: Settings,
    max_steps: int = 6,
    kind: str = "ask",
    user_message: str | None = None,
) -> RunResult:
    wanted = [s.strip().upper() for s in symbols if s.strip()] or extract_symbols(question)
    if not wanted:
        raise ConfigError("No symbol found in the question. Pass one with --symbol, e.g. -s SPY.")
    session = await open_session(settings, provider_name)
    try:
        provider = session.provider
        if isinstance(provider, FixtureProvider):
            available = provider.available_symbols()
            missing = [s for s in wanted if s not in available]
            if missing:
                raise ConfigError(
                    f"No fixture data for {', '.join(missing)}. Available: {', '.join(available)}.",
                    code="symbol_not_found",
                )
        spec = resolve_model_spec(model_spec, session.config, session.provider_name)
        model = create_model(parse_model_spec(spec), settings)
        store = session.open_store()
        try:
            runtime = NativeLoopRuntime(
                model=model,
                gateway=ToolGateway(full_registry(), store, settings, upsell=session.upsell),
                store=store,
                settings=settings,
                budget=Budget(max_steps=max_steps),
                prices=load_prices(settings.home),
                role=kind,
            )
            system = _SYSTEM_PROMPTS.get(kind, ask_system_prompt)(
                mode=provider.mode, provider=provider.name
            )
            result = await runtime.run(
                kind=kind,
                system=system,
                user_message=user_message or ask_user_message(question, wanted),
                provider=provider,
                input_meta={
                    "question": question,
                    "symbols": wanted,
                    "provider": session.provider_name,
                },
                member=session.is_member,
            )
        finally:
            await model.aclose()
            store.close()
        if session.notices:
            result = result.model_copy(update={"notices": session.notices + result.notices})
        return result
    finally:
        await session.aclose()
