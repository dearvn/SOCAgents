"""Tool definitions. Every tool declares its schemas, risk class, trust, and timeout."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel

from socagents.model_gateway.types import ToolSpec
from socagents.providers.base import MarketDataProvider, Mode

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


class RiskClass(StrEnum):
    READ = "read"
    LOW = "low"
    HIGH = "high"
    CRITICAL = "critical"


class Trust(StrEnum):
    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


@dataclass(frozen=True)
class ToolContext:
    run_id: str
    provider: MarketDataProvider
    mode: Mode
    member: bool = False


@dataclass(frozen=True)
class Tool[InT: BaseModel, OutT: BaseModel]:
    name: str
    description: str
    input_model: type[InT]
    output_model: type[OutT]
    handler: Callable[[InT, ToolContext], Awaitable[OutT]]
    risk_class: RiskClass = RiskClass.READ
    trust: Trust = Trust.TRUSTED
    timeout_s: float = 10.0
    snapshot: bool = True
    member_only: bool = False
    upgrade_text: str | None = None

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name):
            raise ValueError(f"Invalid tool name {self.name!r}.")
        if self.timeout_s <= 0:
            raise ValueError("timeout_s must be positive.")

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=self.input_model.model_json_schema(),
        )
