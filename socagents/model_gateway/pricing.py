"""Cost accounting. Prices are user-configured so the package never ships stale numbers.

Create ``$SOCAGENTS_HOME/pricing.json`` to enable cost tracking, for example::

    {"anthropic/<model>": {"input_per_mtok": 3.0, "output_per_mtok": 15.0}}

Tokens are always tracked. Cost is ``None`` when a model has no configured price.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from socagents.core.errors import ConfigError
from socagents.model_gateway.types import Usage

FREE_PROVIDERS = {"fixture", "ollama"}


@dataclass(frozen=True)
class Price:
    input_per_mtok: float
    output_per_mtok: float


def load_prices(home: Path) -> dict[str, Price]:
    path = home / "pricing.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {
            spec: Price(float(p["input_per_mtok"]), float(p["output_per_mtok"]))
            for spec, p in raw.items()
        }
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ConfigError(f"Invalid pricing file {path}: {exc}") from exc


def cost_usd(prices: dict[str, Price], provider: str, model: str, usage: Usage) -> float | None:
    if provider in FREE_PROVIDERS:
        return 0.0
    price = prices.get(f"{provider}/{model}")
    if price is None:
        return None
    return (
        usage.input_tokens * price.input_per_mtok + usage.output_tokens * price.output_per_mtok
    ) / 1_000_000
