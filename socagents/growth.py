"""Upgrade prompts and attribution links.

Rules: one factual line with what SocSwift adds and an attributed link; at most once per CLI
session or MCP conversation; never inside a trade idea or risk decision; can be turned off
with ``socagents config set upsell false``.
"""

from __future__ import annotations

from urllib.parse import urlencode

LANDING_URL = "https://socswift.com/agents"

MEMBER_NOTE = (
    "Member data would add live 0DTE institutional flow, SocSwift's real-time GEX regime, "
    "and dealer hedge flow."
)


def attributed_url(source: str, medium: str, base: str = LANDING_URL) -> str:
    return f"{base}?{urlencode({'ref': 'socagents', 'utm_source': source, 'utm_medium': medium})}"


class Upsell:
    def __init__(self, *, enabled: bool, source: str) -> None:
        self._enabled = enabled
        self._source = source
        self._shown = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def line(self, what: str) -> str | None:
        """Return the upgrade line the first time it is needed, then ``None``."""
        if not self._enabled or self._shown:
            return None
        self._shown = True
        return f"{what} Available to SocSwift members: {attributed_url(self._source, 'prompt')}"
