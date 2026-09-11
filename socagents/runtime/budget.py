"""Per-run budgets on steps, tokens, cost, and wall-clock time."""

from __future__ import annotations

from dataclasses import dataclass

from socagents.core.errors import BudgetExceeded
from socagents.model_gateway.types import Usage


@dataclass(frozen=True)
class Budget:
    max_steps: int = 6
    max_input_tokens: int = 150_000
    max_output_tokens: int = 8_000
    max_cost_usd: float | None = None
    max_seconds: float = 120.0
    max_tokens_per_call: int = 2_048


@dataclass
class UsageMeter:
    steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = 0.0

    def add(self, usage: Usage, cost: float | None) -> None:
        self.steps += 1
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        # One call with an unknown price makes the run total unknown.
        self.cost_usd = None if cost is None or self.cost_usd is None else self.cost_usd + cost

    def check(self, budget: Budget) -> None:
        if self.input_tokens > budget.max_input_tokens:
            raise BudgetExceeded(f"Input token budget exceeded ({self.input_tokens}).")
        if self.output_tokens > budget.max_output_tokens:
            raise BudgetExceeded(f"Output token budget exceeded ({self.output_tokens}).")
        if (
            budget.max_cost_usd is not None
            and self.cost_usd is not None
            and self.cost_usd > budget.max_cost_usd
        ):
            raise BudgetExceeded(f"Cost budget exceeded (${self.cost_usd:.4f}).")
