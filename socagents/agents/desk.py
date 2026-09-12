"""Run SOC Desk: open a session, pick models per role, and run the desk graph."""

from __future__ import annotations

from socagents.core.config import Settings
from socagents.core.crypto import PayloadCipher
from socagents.core.errors import ConfigError
from socagents.db.store import Store
from socagents.desk.graph import DeskGraph, EventSink
from socagents.desk.models import DeskReport
from socagents.desk.replay import ReplayBook
from socagents.desk.roles import PROFILES, ROLES, Profile, profile_roles
from socagents.desk.storage import DeskStore
from socagents.external_mcp import ExternalMCPHub
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
from socagents.skills import resolve_skills


def _profile(name: str) -> Profile:
    profile = PROFILES.get(name)
    if profile is None:
        raise ConfigError(f"Unknown profile {name!r}. Choose one of: {', '.join(PROFILES)}.")
    return profile


def _build_models(
    models: dict[str, ModelProvider],
    roles: list[str],
    model_args: list[str],
    session: Session,
    settings: Settings,
    previous: dict[str, str] | None = None,
) -> None:
    """Fill ``models`` per role: --model args, then ``previous`` (for replays), then defaults.

    ``models`` is filled in place so the caller can close whatever was created on failure.
    """
    selection = parse_model_args(model_args)
    unknown = sorted(set(selection.per_role) - set(ROLES))
    if unknown:
        raise ConfigError(
            f"Unknown role(s) in --model: {', '.join(unknown)}. Roles: {', '.join(ROLES)}."
        )
    previous = previous or {}
    fallback: ModelSpec | None = None
    cache: dict[str, ModelProvider] = {}
    for role in roles:
        spec = selection.per_role.get(role) or selection.default
        if spec is None and role in previous:
            spec = parse_model_spec(previous[role])
        if spec is None:
            if fallback is None:
                fallback = parse_model_spec(
                    resolve_model_spec(None, session.config, session.provider_name)
                )
            spec = fallback
        key = str(spec)
        if key not in cache:
            cache[key] = create_model(spec, settings)
        models[role] = cache[key]


async def _close_models(models: dict[str, ModelProvider]) -> None:
    for model in {id(m): m for m in models.values()}.values():
        await model.aclose()


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
    skills: list[str] | None = None,
) -> DeskReport:
    """Run the desk. Skills enabled in the user config apply, plus any named in ``skills``."""
    profile = _profile(profile_name)
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
        enabled = session.config.skills
        chosen = resolve_skills(settings.home, [*enabled, *(skills or [])], enabled=enabled)
        _build_models(models, profile_roles(profile, debate_rounds), model_args, session, settings)
        store = session.open_store()
        async with ExternalMCPHub(settings.home) as hub:
            session.notices.extend(hub.notices)
            graph = DeskGraph(
                session=session,
                store=store,
                models=models,
                prices=load_prices(settings.home),
                on_event=on_event,
                skills=chosen,
                external_tools=hub.tools_by_role(),
            )
            return await graph.run(symbol, profile, debate_rounds)
    finally:
        await _close_models(models)
        if store is not None:
            store.close()
        if owns_session:
            await session.aclose()


def find_report(settings: Settings, ref: str) -> DeskReport:
    store = Store(settings.db_path, cipher=PayloadCipher.from_keyring(create=False))
    try:
        report = DeskStore(store).get_report(ref)
    finally:
        store.close()
    if report is None:
        raise ConfigError(
            f"No single Desk Report matches {ref!r}. See `socagents report list`.",
            code="report_not_found",
        )
    return report


async def run_replay(
    *,
    ref: str,
    model_args: list[str],
    settings: Settings,
    profile_name: str | None = None,
    rounds: int | None = None,
    on_event: EventSink | None = None,
    session: Session | None = None,
    utm_source: str = "cli",
    skills: list[str] | None = None,
) -> tuple[DeskReport, DeskReport]:
    """Re-run a stored desk on its recorded data. Returns (original, replay).

    Roles keep the original run's models unless ``model_args`` overrides them, and the run
    keeps the original's skills unless ``skills`` is given (``["none"]`` for no skills).
    Replaying a member run needs an active membership, because it reads member data.
    """
    original = find_report(settings, ref)
    profile = _profile(profile_name or original.profile)
    if rounds is None:
        same_profile = profile.name == original.profile
        rounds = (
            max((t.round for t in original.debate), default=0)
            if same_profile
            else profile.debate_rounds
        )
    owns_session = session is None
    if session is None:
        provider = "socswift" if original.mode == "member" else "community"
        session = await open_session(settings, provider, utm_source=utm_source)
    models: dict[str, ModelProvider] = {}
    store = None
    try:
        if original.mode == "member" and not session.is_member:
            raise ConfigError(
                "Replaying a member run needs an active SocSwift membership.",
                code="requires_membership",
            )
        names = [s.name for s in original.skills] if skills is None else skills
        names = [n for n in names if n != "none"]
        chosen = resolve_skills(settings.home, names, enabled=session.config.skills)
        roles = profile_roles(profile, rounds)
        _build_models(models, roles, model_args, session, settings, previous=original.models)
        store = session.open_store()
        graph = DeskGraph(
            session=session,
            store=store,
            models=models,
            prices=load_prices(settings.home),
            on_event=on_event,
            replay=ReplayBook.from_store(store, original.desk_run_id),
            replay_of=original.id,
            skills=chosen,
        )
        replay = await graph.run(original.symbol, profile, rounds)
        return original, replay
    finally:
        await _close_models(models)
        if store is not None:
            store.close()
        if owns_session:
            await session.aclose()
