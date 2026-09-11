"""Open a data session: pick the provider, check membership, and set up upgrade prompts.

Provider choice: explicit flag, then ``SOCAGENTS_PROVIDER``, then ``default_provider`` in the
user config, then SocSwift when an API key is stored, else Community. A lapsed or invalid
member key falls back to Community with a clear notice.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from socagents.core.config import Settings
from socagents.core.credentials import load_api_key
from socagents.core.crypto import PayloadCipher
from socagents.core.errors import ConfigError, ProviderError
from socagents.core.userconfig import UserConfig, load_user_config
from socagents.db.store import MEMBER_RETENTION_DAYS, Store
from socagents.growth import Upsell
from socagents.providers import PROVIDER_NAMES
from socagents.providers.base import MarketDataProvider
from socagents.providers.community import CommunityProvider
from socagents.providers.fixture import FixtureProvider
from socagents.providers.socswift import SocSwiftProvider
from socagents.socswift_client.client import MemberInfo, MembershipError, SocSwiftClient


@dataclass
class Session:
    settings: Settings
    config: UserConfig
    provider_name: str
    provider: MarketDataProvider
    upsell: Upsell
    member: MemberInfo | None = None
    notices: list[str] = field(default_factory=list)

    @property
    def is_member(self) -> bool:
        return self.member is not None

    def open_store(self) -> Store:
        cipher = PayloadCipher.from_keyring() if self.is_member else None
        store = Store(self.settings.db_path, cipher=cipher)
        store.purge_member_data(older_than_days=MEMBER_RETENTION_DAYS)
        return store

    async def aclose(self) -> None:
        await self.provider.aclose()


def purge_member_data(settings: Settings) -> tuple[int, int]:
    """Delete all stored member snapshots and reports. Return (snapshots, reports) removed.

    Runs on logout and whenever SocSwift rejects the stored key, so member data never outlives
    the membership. A temporary outage (rate limit, network) does not purge.
    """
    from socagents.desk.storage import DeskStore

    if not settings.db_path.exists():
        return 0, 0
    store = Store(settings.db_path)
    try:
        return store.purge_member_data(), DeskStore(store).purge_member()
    finally:
        store.close()


def resolve_provider_name(explicit: str | None, config: UserConfig) -> str:
    name = explicit or os.environ.get("SOCAGENTS_PROVIDER") or config.default_provider
    if name is None:
        name = "socswift" if load_api_key() else "community"
    if name not in PROVIDER_NAMES:
        raise ConfigError(
            f"Unknown data provider {name!r}. Choose one of: {', '.join(PROVIDER_NAMES)}."
        )
    return name


def resolve_model_spec(explicit: str | None, config: UserConfig, provider_name: str) -> str:
    spec = explicit or os.environ.get("SOCAGENTS_MODEL") or config.default_model
    if spec is None and provider_name == "fixture":
        spec = "fixture/scripted"
    if spec is None:
        raise ConfigError(
            "Choose a model with --model PROVIDER/MODEL (for example ollama/<model> or "
            "anthropic/<model>), set SOCAGENTS_MODEL, or run "
            "`socagents config set default_model PROVIDER/MODEL`."
        )
    return spec


async def open_session(
    settings: Settings, provider_name: str | None = None, *, utm_source: str = "cli"
) -> Session:
    config = load_user_config(settings.home)
    upsell = Upsell(enabled=config.upsell, source=utm_source)
    name = resolve_provider_name(provider_name, config)

    if name == "fixture":
        return Session(settings, config, name, FixtureProvider(), upsell)

    community = CommunityProvider(calendar_path=settings.home / "calendar.json")
    if name == "community":
        return Session(settings, config, name, community, upsell)

    key = load_api_key()
    if not key:
        await community.aclose()
        raise ConfigError(
            "No SocSwift API key. Members: run `socagents login`. Everyone else: use "
            "--provider community."
        )
    client = SocSwiftClient(key)
    try:
        member = await client.me()
    except (MembershipError, ProviderError) as exc:
        await client.aclose()
        if isinstance(exc, MembershipError):
            purge_member_data(settings)
        if provider_name == "socswift":
            await community.aclose()
            raise
        notice = f"{exc} Using Community mode (free, delayed data)."
        return Session(settings, config, "community", community, upsell, notices=[notice])
    provider = SocSwiftProvider(client, member, community)
    return Session(settings, config, name, provider, upsell, member=member)
