"""Error types. Every error carries a stable machine-readable code."""

from __future__ import annotations


class SocAgentsError(Exception):
    code: str = "error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class ConfigError(SocAgentsError):
    code = "config_error"


class ModelError(SocAgentsError):
    code = "model_error"


class ProviderError(SocAgentsError):
    code = "provider_error"


class SymbolNotFound(ProviderError):
    code = "symbol_not_found"


class BudgetExceeded(SocAgentsError):
    code = "budget_exceeded"


class AgentsDisabled(SocAgentsError):
    code = "agents_disabled"


class InvalidTransition(SocAgentsError):
    code = "invalid_transition"
