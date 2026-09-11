"""Parse model specs and build providers.

``--model PROVIDER/MODEL`` sets every role; ``--model ROLE=PROVIDER/MODEL`` sets one role.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from socagents.core.config import Settings
from socagents.core.errors import ConfigError
from socagents.model_gateway.anthropic import AnthropicModel
from socagents.model_gateway.gemini import GeminiModel
from socagents.model_gateway.openai_compat import OpenAICompatibleModel
from socagents.model_gateway.scripted import ScriptedModel
from socagents.model_gateway.types import ModelProvider

PROVIDERS = ("anthropic", "openai", "google", "ollama", "fixture")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str

    def __str__(self) -> str:
        return f"{self.provider}/{self.model}"


@dataclass(frozen=True)
class ModelSelection:
    default: ModelSpec | None = None
    per_role: dict[str, ModelSpec] = field(default_factory=dict)

    def for_role(self, role: str) -> ModelSpec | None:
        return self.per_role.get(role, self.default)


def parse_model_spec(spec: str) -> ModelSpec:
    provider, sep, model = spec.strip().partition("/")
    if not sep or not provider or not model:
        raise ConfigError(f"Model must look like PROVIDER/MODEL, got {spec!r}.")
    if provider not in PROVIDERS:
        raise ConfigError(
            f"Unknown model provider {provider!r}. Choose one of: {', '.join(PROVIDERS)}."
        )
    return ModelSpec(provider=provider, model=model)


def parse_model_args(values: list[str]) -> ModelSelection:
    default: ModelSpec | None = None
    per_role: dict[str, ModelSpec] = {}
    for value in values:
        role, sep, spec = value.partition("=")
        if sep:
            if not _ROLE_RE.match(role):
                raise ConfigError(f"Invalid role name {role!r} in --model {value!r}.")
            per_role[role] = parse_model_spec(spec)
        else:
            default = parse_model_spec(value)
    return ModelSelection(default=default, per_role=per_role)


def create_model(spec: ModelSpec, settings: Settings) -> ModelProvider:
    if spec.provider == "anthropic":
        return AnthropicModel(spec.model)
    if spec.provider == "openai":
        return OpenAICompatibleModel(spec.model)
    if spec.provider == "google":
        return GeminiModel(spec.model)
    if spec.provider == "ollama":
        return OpenAICompatibleModel(
            spec.model,
            provider="ollama",
            base_url=settings.ollama_base_url,
            api_key="ollama",
            token_param="max_tokens",
        )
    if spec.provider == "fixture":
        return ScriptedModel(spec.model)
    raise ConfigError(f"Unknown model provider {spec.provider!r}.")
