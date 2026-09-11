"""Run SOC Desk: open a session, pick models per role, and run the desk graph."""

from __future__ import annotations

from socagents.core.config import Settings
from socagents.core.errors import ConfigError
from socagents.desk.graph import DeskGraph, EventSink
from socagents.desk.models import DeskReport
from socagents.desk.roles import PROFILES, ROLES, profile_roles
from socagents.model_gateway.pricing import load_prices
from socagents.model_gateway.router import (
    ModelSpec,
    create_model,
    parse_model_args,
    parse_model_spec,
)
from socagents.model_gateway.types import ModelProvider
from socagents.providers.fixture import FixtureProvider
from socagents.session import Session, open_session, resolve_model_spec


async def run_desk(
    *,
    symbol: str,
    profile_name: str,
    rounds: int | None,
    model_args: list[str],
    provider_name: str | None,
    settings: Settings,
    on_event: EventSink | None = None,
    session: Session | None = None,
    utm_source: str = "cli",
) -> DeskReport:
    profile = PROFILES.get(profile_name)
    if profile is None:
        raise ConfigError(
            f"Unknown profile {profile_name!r}. Choose one of: {', '.join(PROFILES)}."
        )
    debate_rounds = profile.debate_rounds if rounds is None else rounds
    owns_session = session is None
    session = session or await open_session(settings, provider_name, utm_source=utm_source)
    models: dict[str, ModelProvider] = {}
    store = None
    try:
        symbol = symbol.strip().upper()
        if (
            isinstance(session.provider, FixtureProvider)
            and symbol not in session.provider.available_symbols()
        ):
            raise ConfigError(
                f"No fixture data for {symbol}. Available: "
                f"{', '.join(session.provider.available_symbols())}.",
                code="symbol_not_found",
            )
        roles = profile_roles(profile, debate_rounds)
        selection = parse_model_args(model_args)
        unknown = sorted(set(selection.per_role) - set(ROLES))
        if unknown:
            raise ConfigError(
                f"Unknown role(s) in --model: {', '.join(unknown)}. Roles: {', '.join(ROLES)}."
            )
        default: ModelSpec | None = selection.default
        if default is None and any(r not in selection.per_role for r in roles):
            default = parse_model_spec(
                resolve_model_spec(None, session.config, session.provider_name)
            )
        cache: dict[str, ModelProvider] = {}
        for role in roles:
            spec = selection.per_role.get(role, default)
            assert spec is not None
            key = str(spec)
            if key not in cache:
                cache[key] = create_model(spec, settings)
            models[role] = cache[key]
        store = session.open_store()
        graph = DeskGraph(
            session=session,
            store=store,
            models=models,
            prices=load_prices(settings.home),
            on_event=on_event,
        )
        return await graph.run(symbol, profile, debate_rounds)
    finally:
        for model in {id(m): m for m in models.values()}.values():
            await model.aclose()
        if store is not None:
            store.close()
        if owns_session:
            await session.aclose()
