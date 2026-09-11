"""Market data providers: fixture (recorded), community (free public data), socswift (members).

Use :func:`socagents.session.open_session` to pick and open a provider.
"""

from socagents.providers.base import MarketDataProvider

PROVIDER_NAMES = ("community", "fixture", "socswift")

__all__ = ["PROVIDER_NAMES", "MarketDataProvider"]
