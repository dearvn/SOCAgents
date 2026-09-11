"""Market data providers: fixture (recorded), community (free public data), socswift (members)."""

from __future__ import annotations

from socagents.core.errors import ConfigError
from socagents.providers.base import MarketDataProvider
from socagents.providers.fixture import FixtureProvider

PROVIDER_NAMES = ("fixture", "community", "socswift")


def create_provider(name: str) -> MarketDataProvider:
    if name == "fixture":
        return FixtureProvider()
    if name == "community":
        raise ConfigError(
            "The Community provider (free delayed public data) arrives in v0.1. "
            "Use --provider fixture for now."
        )
    if name == "socswift":
        raise ConfigError(
            "The SocSwift member provider arrives in v0.1. Use --provider fixture for now."
        )
    raise ConfigError(
        f"Unknown data provider {name!r}. Choose one of: {', '.join(PROVIDER_NAMES)}."
    )
