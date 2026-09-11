"""Native tool-use loop for single agents. Checkpoints after every step."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import BaseModel, Field

from socagents.core.config import Settings
from socagents.core.errors import BudgetExceeded, SocAgentsError
from socagents.db.store import Store
from socagents.model_gateway.pricing import Price, cost_usd
from socagents.model_gateway.types import Message, ModelProvider
from socagents.providers.base import MarketDataProvider
from socagents.runtime.budget import Budget, UsageMeter
from socagents.runtime.states import RunStatus
from socagents.tools.gateway import SnapshotRef, ToolGateway
from socagents.tools.sdk import ToolContext

Observer = Callable[[str, dict[str, Any]], None]


class RunError(BaseModel):
    code: str
    message: str


class RunResult(BaseModel):
    run_id: str
    status: RunStatus
    mode: str
    model: str
    answer: str | None = None
    error: RunError | None = None
    snapshots: list[SnapshotRef] = Field(default_factory=list)
    notices: list[str] = Field(default_factory=list)
    steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None

    @property
    def data_delay_sec(self) -> int | None:
        delays = [s.delayed_sec for s in self.snapshots if s.delayed_sec is not None]
        return max(delays) if delays else None


class AgentRuntime(Protocol):
    async def run(
        self,
        *,
        kind: str,
        system: str,
        user_message: str,
        provider: MarketDataProvider,
        input_meta: dict[str, Any],
        member: bool = False,
    ) -> RunResult: ...


class NativeLoopRuntime:
    def __init__(
        self,
        *,
        model: ModelProvider,
        gateway: ToolGateway,
        store: Store,
        settings: Settings,
        budget: Budget | None = None,
        prices: dict[str, Price] | None = None,
        role: str = "agent",
        observer: Observer | None = None,
    ) -> None:
        self._model = model
        self._gateway = gateway
        self._store = store
        self._settings = settings
        self._budget = budget or Budget()
        self._prices = prices or {}
        self._role = role
        self._observer = observer

    @property
    def model_name(self) -> str:
        return f"{self._model.provider}/{self._model.model}"

    def _emit(self, run_id: str, type: str, payload: dict[str, Any]) -> None:
        self._store.add_event(run_id, type, payload)
        if self._observer is not None:
            self._observer(type, {"role": self._role, "run_id": run_id, **payload})

    async def run(
        self,
        *,
        kind: str,
        system: str,
        user_message: str,
        provider: MarketDataProvider,
        input_meta: dict[str, Any],
        member: bool = False,
    ) -> RunResult:
        store = self._store
        run_id = store.create_run(
            kind=kind, mode=provider.mode, model=self.model_name, input=input_meta
        )
        store.transition_run(run_id, RunStatus.RUNNING)
        self._emit(run_id, "run.started", {"kind": kind, "model": self.model_name})

        ctx = ToolContext(run_id=run_id, provider=provider, mode=provider.mode, member=member)
        meter = UsageMeter()
        snapshots: dict[str, SnapshotRef] = {}
        notices: list[str] = []
        status = RunStatus.COMPLETED
        answer: str | None = None
        error: RunError | None = None

        try:
            if not self._settings.kill_switches.agents:
                raise SocAgentsError("Agents are disabled (AGENTS=0).", code="agents_disabled")
            async with asyncio.timeout(self._budget.max_seconds):
                answer = await self._loop(
                    run_id, system, user_message, ctx, meter, snapshots, notices
                )
        except TimeoutError:
            status = RunStatus.EXPIRED
            error = RunError(
                code="wall_clock_timeout", message=f"Run exceeded {self._budget.max_seconds:g}s."
            )
        except SocAgentsError as exc:
            status = RunStatus.FAILED
            error = RunError(code=exc.code, message=str(exc))
        except Exception as exc:
            store.transition_run(
                run_id,
                RunStatus.FAILED,
                error={"code": "internal_error", "message": type(exc).__name__},
            )
            raise
        finally:
            store.set_run_usage(
                run_id,
                steps=meter.steps,
                input_tokens=meter.input_tokens,
                output_tokens=meter.output_tokens,
                cost_usd=meter.cost_usd,
            )

        snapshot_ids = list(snapshots)
        if status is RunStatus.COMPLETED:
            store.transition_run(
                run_id, status, output={"answer": answer, "snapshot_ids": snapshot_ids}
            )
        else:
            store.transition_run(run_id, status, error=error.model_dump() if error else None)
        self._emit(
            run_id,
            f"run.{status.value}",
            {"snapshot_ids": snapshot_ids, "error": error.model_dump() if error else None},
        )

        return RunResult(
            run_id=run_id,
            status=status,
            mode=provider.mode,
            model=self.model_name,
            answer=answer,
            error=error,
            snapshots=list(snapshots.values()),
            notices=notices,
            steps=meter.steps,
            input_tokens=meter.input_tokens,
            output_tokens=meter.output_tokens,
            cost_usd=meter.cost_usd,
        )

    async def _loop(
        self,
        run_id: str,
        system: str,
        user_message: str,
        ctx: ToolContext,
        meter: UsageMeter,
        snapshots: dict[str, SnapshotRef],
        notices: list[str],
    ) -> str:
        messages = [Message(role="user", content=user_message)]
        tools = self._gateway.registry.specs()
        for step in range(self._budget.max_steps):
            response = await self._model.complete(
                system=system,
                messages=messages,
                tools=tools,
                max_tokens=self._budget.max_tokens_per_call,
            )
            cost = cost_usd(self._prices, self._model.provider, self._model.model, response.usage)
            meter.add(response.usage, cost)
            self._store.record_usage(
                run_id=run_id,
                provider=self._model.provider,
                model=self._model.model,
                role=self._role,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cost_usd=cost,
            )
            messages.append(
                Message(role="assistant", content=response.text, tool_calls=response.tool_calls)
            )
            self._emit(
                run_id,
                "model.completed",
                {
                    "step": step,
                    "tool_calls": [c.name for c in response.tool_calls],
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "cost_usd": cost,
                },
            )
            meter.check(self._budget)

            if not response.tool_calls:
                return response.text

            for call in response.tool_calls:
                self._emit(run_id, "tool.started", {"tool": call.name, "id": call.id})
                result = await self._gateway.call(ctx, call)
                if result.snapshot is not None:
                    snapshots[result.snapshot.id] = result.snapshot
                if result.notice:
                    notices.append(result.notice)
                messages.append(
                    Message(
                        role="tool",
                        tool_call_id=call.id,
                        name=call.name,
                        content=result.to_message_content(),
                        is_error=not result.ok,
                    )
                )
                self._emit(
                    run_id,
                    "tool.completed",
                    {
                        "tool": call.name,
                        "id": call.id,
                        "ok": result.ok,
                        "error_code": result.error_code,
                        "snapshot_id": result.snapshot.id if result.snapshot else None,
                    },
                )
            self._store.save_checkpoint(
                run_id, step, {"messages": [m.model_dump(mode="json") for m in messages]}
            )

        raise BudgetExceeded(f"Step limit reached ({self._budget.max_steps}) without an answer.")
