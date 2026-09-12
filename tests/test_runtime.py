from __future__ import annotations

import asyncio
from typing import Any

from socagents.core.config import KillSwitches, Settings
from socagents.db.store import Store
from socagents.model_gateway.scripted import ScriptedModel
from socagents.model_gateway.types import Message, ModelResponse, ToolCall, ToolSpec, Usage
from socagents.providers.fixture import FixtureProvider
from socagents.runtime.budget import Budget
from socagents.runtime.native import NativeLoopRuntime, RunResult
from socagents.runtime.states import RunStatus
from socagents.templates.ask import ask_user_message
from socagents.tools.gateway import ToolGateway
from socagents.tools.market import default_registry


class FakeModel:
    """Scriptable model: returns the given responses in order, repeating the last one."""

    def __init__(self, *responses: ModelResponse, provider: str = "test", delay: float = 0) -> None:
        self.provider = provider
        self.model = "fake"
        self._responses = list(responses)
        self._delay = delay

    async def complete(
        self, *, system: str, messages: list[Message], tools: list[ToolSpec], max_tokens: int
    ) -> ModelResponse:
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]

    async def aclose(self) -> None:
        return None


def quote_call() -> ModelResponse:
    return ModelResponse(
        text="",
        model="fake",
        tool_calls=[ToolCall(id="c", name="get_quote", arguments={"symbols": ["SPY"]})],
    )


def answer(text: str = "done", usage: Usage | None = None) -> ModelResponse:
    return ModelResponse(text=text, model="fake", usage=usage or Usage())


async def run(
    model: Any,
    store: Store,
    settings: Settings,
    budget: Budget | None = None,
    symbols: tuple[str, ...] = ("SPY",),
) -> RunResult:
    runtime = NativeLoopRuntime(
        model=model,
        gateway=ToolGateway(default_registry(), store, settings),
        store=store,
        settings=settings,
        budget=budget,
    )
    return await runtime.run(
        kind="ask",
        system="sys",
        user_message=ask_user_message("Where is it?", list(symbols)),
        provider=FixtureProvider(),
        input_meta={"symbols": list(symbols)},
    )


async def test_truncated_reply_fails_the_run_without_running_its_tools(store, settings) -> None:
    for stop_reason in ("max_tokens", "length", "MAX_TOKENS"):
        cut = quote_call().model_copy(update={"stop_reason": stop_reason})
        budget = Budget(max_tokens_per_call=100)
        result = await run(FakeModel(cut, answer()), store, settings, budget)
        assert result.status is RunStatus.FAILED
        assert result.error is not None and result.error.code == "output_truncated"
        assert "100-token" in result.error.message
        assert result.snapshots == []


async def test_scripted_run_completes_with_cited_snapshots(store, settings) -> None:
    result = await run(ScriptedModel(), store, settings)

    assert result.status is RunStatus.COMPLETED
    assert result.steps == 2
    assert len(result.snapshots) == 3
    assert result.answer is not None
    assert all(s.id in result.answer for s in result.snapshots)
    assert "581.20" in result.answer
    assert "largest call OI above at 585" in result.answer
    assert result.data_delay_sec == 900

    row = store.get_run(result.run_id)
    assert row["status"] == "completed"
    assert row["started_at"] and row["finished_at"]
    types = [e["type"] for e in store.list_events(result.run_id)]
    assert types[0] == "run.started"
    assert types[-1] == "run.completed"
    assert types.count("tool.completed") == 3
    checkpoint = store.latest_checkpoint(result.run_id)
    assert checkpoint is not None
    assert [m["role"] for m in checkpoint["messages"]][:2] == ["user", "assistant"]


async def test_tool_failures_reach_the_model(store, settings) -> None:
    result = await run(ScriptedModel(), store, settings, symbols=("TSLA",))
    assert result.status is RunStatus.COMPLETED
    assert "Missing data" in (result.answer or "")
    assert result.snapshots == []


async def test_step_budget_fails_the_run(store, settings) -> None:
    result = await run(FakeModel(quote_call()), store, settings, Budget(max_steps=3))
    assert result.status is RunStatus.FAILED
    assert result.error is not None and result.error.code == "budget_exceeded"
    assert result.steps == 3


async def test_token_budget_fails_the_run(store, settings) -> None:
    heavy = answer(usage=Usage(input_tokens=10, output_tokens=10_000))
    result = await run(FakeModel(heavy), store, settings, Budget(max_output_tokens=100))
    assert result.status is RunStatus.FAILED
    assert result.error is not None and result.error.code == "budget_exceeded"


async def test_wall_clock_timeout_expires_the_run(store, settings) -> None:
    result = await run(FakeModel(answer(), delay=1), store, settings, Budget(max_seconds=0.05))
    assert result.status is RunStatus.EXPIRED
    assert store.get_run(result.run_id)["status"] == "expired"


async def test_agents_kill_switch_fails_the_run(store, tmp_path) -> None:
    off = Settings(home=tmp_path, kill_switches=KillSwitches(agents=False))
    result = await run(ScriptedModel(), store, off)
    assert result.status is RunStatus.FAILED
    assert result.error is not None and result.error.code == "agents_disabled"
    assert result.steps == 0


async def test_unknown_price_makes_cost_unknown(store, settings) -> None:
    result = await run(
        FakeModel(answer(usage=Usage(input_tokens=5)), provider="anthropic"), store, settings
    )
    assert result.status is RunStatus.COMPLETED
    assert result.cost_usd is None
    assert result.input_tokens == 5


async def test_free_provider_cost_is_zero(store, settings) -> None:
    result = await run(ScriptedModel(), store, settings)
    assert result.cost_usd == 0.0
