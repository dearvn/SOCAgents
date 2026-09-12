"""SOC Desk orchestration.

Analysts run in parallel, then the bull/bear debate, the Strategist, the deterministic risk
engine (with an advisory Risk Officer critique), and the Desk Lead. Every role is a normal
agent run through the tool gateway, so tool calls are validated, audited, and snapshotted.
Key levels are verified against the snapshots they cite before the report is saved.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from socagents.core.errors import AgentsDisabled, ConfigError, ModelError, SocAgentsError
from socagents.core.ids import new_id
from socagents.core.market import MarketSession, market_session
from socagents.core.timeutil import ET, utcnow
from socagents.db.store import Store
from socagents.desk.models import (
    EDUCATIONAL_LABEL,
    REPLAY_LABEL,
    AnalystReport,
    CritiqueOutput,
    DataFreshness,
    DebateArgument,
    DebateTurn,
    DeskIdea,
    DeskReport,
    IdeaRiskCheck,
    LeadOutput,
    ReviewedIdea,
    SkillRef,
    StrategistOutput,
    UsageSummary,
)
from socagents.desk.roles import ROLES, Profile, profile_roles, system_prompt, user_message
from socagents.desk.storage import DeskStore
from socagents.desk.structured import parse_or_repair
from socagents.desk.verify import known_evidence, numbers_in, verify_levels
from socagents.growth import MEMBER_NOTE, attributed_url
from socagents.model_gateway.pricing import Price, cost_usd
from socagents.model_gateway.types import ModelProvider, ToolCall
from socagents.risk.engine import RiskContext, RiskDecision, RiskProfile, RiskReason, evaluate
from socagents.runtime.budget import Budget
from socagents.runtime.native import NativeLoopRuntime
from socagents.runtime.states import RunStatus
from socagents.session import Session
from socagents.skills import Skill
from socagents.tools.catalog import registry_for
from socagents.tools.gateway import RecordedData, SnapshotRef, ToolGateway
from socagents.tools.sdk import Tool, ToolContext

PREORDER_ENTITLEMENT = "execution_preorder"
MAX_IDEAS = 2


@dataclass(frozen=True)
class DeskEvent:
    type: str
    role: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


EventSink = Callable[[DeskEvent], None]


class DeskGraph:
    def __init__(
        self,
        *,
        session: Session,
        store: Store,
        models: dict[str, ModelProvider],
        prices: dict[str, Price] | None = None,
        on_event: EventSink | None = None,
        risk_profile: RiskProfile | None = None,
        max_seconds: float = 900.0,
        replay: RecordedData | None = None,
        replay_of: str | None = None,
        skills: list[Skill] | None = None,
        external_tools: dict[str, list[Tool[Any, Any]]] | None = None,
    ) -> None:
        self._replay = replay
        self._replay_of = replay_of
        self._skills = skills or []
        self._external_tools = external_tools or {}
        self._session = session
        self._settings = session.settings
        self._store = store
        self._desk_store = DeskStore(store)
        self._models = models
        self._prices = prices or {}
        self._on_event = on_event
        self._risk_profile = risk_profile or RiskProfile()
        self._max_seconds = max_seconds
        self._snapshots: dict[str, SnapshotRef] = {}
        self._payloads: dict[str, dict[str, Any]] = {}
        self._trusted: set[str] = set()
        self._usage = UsageSummary()
        self._notices: list[str] = []
        self._removed: list[str] = []
        self._symbol = ""
        self._mode = "community"
        self._desk_run_id = ""
        self._as_of: datetime = utcnow()
        self._market: MarketSession | None = None

    # events

    def _emit(self, type: str, role: str | None = None, **data: Any) -> None:
        if self._on_event is not None:
            self._on_event(DeskEvent(type=type, role=role, data=data))

    def _observe(self, type: str, payload: dict[str, Any]) -> None:
        role = payload.get("role")
        self._emit(f"role.{type}", role, **{k: v for k, v in payload.items() if k != "role"})

    # entry point

    async def run(self, symbol: str, profile: Profile, rounds: int) -> DeskReport:
        if not self._settings.kill_switches.agents:
            raise AgentsDisabled("Agents are disabled (AGENTS=0).")
        mode = "member" if self._session.is_member else "community"
        if profile.member_only and mode != "member":
            raise ConfigError(
                f"The {profile.name} profile needs SocSwift member data (dealer hedge flow). "
                f"See {attributed_url('cli', 'prompt')}",
                code="requires_membership",
            )
        roles = profile_roles(profile, rounds)
        missing_models = [r for r in roles if r not in self._models]
        if missing_models:
            raise ConfigError(f"No model configured for: {', '.join(missing_models)}.")
        self._symbol, self._mode = symbol, mode
        self._desk_run_id = self._desk_store.create_run(
            symbol=symbol,
            profile=profile.name,
            mode=mode,
            models={r: f"{self._models[r].provider}/{self._models[r].model}" for r in roles},
        )
        for skill in self._skills:
            self._store.audit(
                actor="desk",
                action="skill.applied",
                detail={
                    "desk_run_id": self._desk_run_id,
                    "skill": skill.name,
                    "source": skill.source,
                    "sha256": skill.content_hash,
                    "applies_to": skill.manifest.applies_to,
                },
            )
        self._emit(
            "desk.started",
            symbol=symbol,
            profile=profile.name,
            mode=mode,
            roles=roles,
            skills=[s.name for s in self._skills],
        )
        try:
            async with asyncio.timeout(self._max_seconds):
                report = await self._run(profile, rounds, roles)
        except TimeoutError as exc:
            self._fail("wall_clock_timeout")
            raise SocAgentsError(
                "The desk run exceeded its time budget.", code="wall_clock_timeout"
            ) from exc
        except SocAgentsError as exc:
            self._fail(f"{exc.code}: {exc}")
            raise
        except Exception as exc:
            self._fail(f"internal_error: {type(exc).__name__}")
            raise
        self._desk_store.finish_run(self._desk_run_id, status="completed", usage=self._usage)
        self._emit("report.completed", report_id=report.id, usage=self._usage.model_dump())
        return report

    def _fail(self, error: str) -> None:
        self._desk_store.finish_run(
            self._desk_run_id, status="failed", usage=self._usage, error=error
        )
        self._emit("desk.failed", error=error)

    # pipeline

    async def _run(self, profile: Profile, rounds: int, roles: list[str]) -> DeskReport:
        spot, spot_snapshot = await self._context_quote()
        # A replay judges the session at the time of its recorded data, not today.
        self._market = market_session(self._as_of, self._as_of if self._replay else None)
        base = {
            "symbol": self._symbol,
            "mode": self._mode,
            "spot": spot,
            "spot_snapshot": spot_snapshot,
            "as_of": self._as_of.isoformat(),
            "market": self._market.context(),
        }

        results = await asyncio.gather(*(self._analyst(name, base) for name in profile.analysts))
        reports = [r for r in results if r is not None]
        missing = [n for n, r in zip(profile.analysts, results, strict=True) if r is None]
        if not reports:
            raise SocAgentsError("Every analyst failed, so there is no report.", code="desk_failed")
        report_dicts = [r.model_dump(mode="json") for r in reports]

        debate: list[DebateTurn] = []
        for round_no in range(1, rounds + 1):
            for side in ("bull", "bear"):
                argument = await self._role_output(
                    side,
                    {
                        **base,
                        "reports": report_dicts,
                        "round": round_no,
                        "debate": [t.model_dump(mode="json") for t in debate],
                    },
                    DebateArgument,
                    f"Round {round_no}: make the {side} case for {self._symbol}.",
                )
                if argument is None:
                    missing.append(f"{side} (round {round_no})")
                    continue
                turn = DebateTurn(
                    side=side,
                    round=round_no,
                    argument=argument.argument,
                    evidence=known_evidence(argument.evidence, set(self._payloads)),
                )
                debate.append(turn)
                self._desk_store.save_turn(
                    self._desk_run_id, round=round_no, side=side, turn=turn, mode=self._mode
                )
                self._emit("debate.turn", side, round=round_no, argument=turn.argument)
        debate_dicts = [t.model_dump(mode="json") for t in debate]

        ideas: list[DeskIdea] = []
        no_trade: str | None = None
        if profile.strategist:
            output = await self._role_output(
                "strategist",
                {**base, "reports": report_dicts, "debate": debate_dicts, "max_ideas": MAX_IDEAS},
                StrategistOutput,
                f"Propose trade ideas for {self._symbol}, or say why there is no trade.",
            )
            if output is None:
                missing.append("strategist")
            else:
                ideas, no_trade = output.ideas[:MAX_IDEAS], output.no_trade_reason
        reviewed = self._review(ideas)

        if reviewed and profile.llm_risk_critique:
            critique = await self._role_output(
                "risk_officer",
                {
                    **base,
                    "ideas": [r.idea.model_dump(mode="json") for r in reviewed],
                    "risk_checks": [r.risk_check.model_dump(mode="json") for r in reviewed],
                },
                CritiqueOutput,
                "Critique each idea. The deterministic decision is final.",
            )
            if critique is None:
                missing.append("risk_officer")
            else:
                for note in critique.critiques:
                    if 0 <= note.index < len(reviewed):
                        reviewed[note.index].risk_check.critique = note.critique
        for idx, item in enumerate(reviewed):
            self._desk_store.save_idea(
                self._desk_run_id, idx=idx, idea=item, convertible=item.convertible, mode=self._mode
            )
            self._emit(
                "strategist.idea",
                "strategist",
                contract=item.idea.contract,
                display=item.risk_check.display,
            )

        lead = await self._role_output(
            "desk_lead",
            {
                **base,
                "reports": report_dicts,
                "debate": debate_dicts,
                "ideas": [r.model_dump(mode="json") for r in reviewed],
                "no_trade_reason": no_trade,
            },
            LeadOutput,
            f"Write the Desk Report for {self._symbol}.",
        )
        if lead is None:
            raise SocAgentsError("The Desk Lead could not write the report.", code="desk_failed")

        numbers = self._numbers()
        levels, removed = verify_levels(lead.key_levels, numbers, owner="desk_lead")
        known = set(numbers)
        scenarios = [
            s.model_copy(update={"evidence": known_evidence(s.evidence, known)})
            for s in lead.scenarios
        ]

        notices = list(self._session.notices) + list(dict.fromkeys(self._notices))
        if self._mode == "community":
            line = self._session.upsell.line(MEMBER_NOTE)
            if line:
                notices.append(line)

        report = DeskReport(
            id=new_id("rpt"),
            desk_run_id=self._desk_run_id,
            symbol=self._symbol,
            as_of=self._as_of,
            mode="member" if self._mode == "member" else "community",
            profile=profile.name,
            regime=lead.regime,
            summary=lead.summary,
            key_levels=levels,
            scenarios=scenarios,
            ideas=reviewed,
            no_trade_reason=no_trade,
            dissent=lead.dissent,
            analysts=reports,
            debate=debate,
            data_freshness=self._freshness(),
            removed_unverified=self._removed + removed,
            missing_roles=missing,
            notices=notices,
            models={r: f"{self._models[r].provider}/{self._models[r].model}" for r in roles},
            usage=self._usage,
            created_at=utcnow(),
            replay_of=self._replay_of,
            skills=[
                SkillRef(name=s.name, source=s.source, sha256=s.content_hash) for s in self._skills
            ],
            market=self._market,
        )
        self._desk_store.save_report(report)
        return report

    async def _context_quote(self) -> tuple[float, str]:
        store = self._store
        run_id = store.create_run(
            kind="desk.context",
            mode=self._session.provider.mode,
            model="none",
            input={"desk_run_id": self._desk_run_id},
        )
        store.transition_run(run_id, RunStatus.RUNNING)
        gateway = ToolGateway(
            registry_for(["get_quote"]), store, self._settings, replay=self._replay
        )
        ctx = ToolContext(
            run_id=run_id,
            provider=self._session.provider,
            mode=self._session.provider.mode,
            member=self._session.is_member,
        )
        result = await gateway.call(
            ctx,
            ToolCall(id=new_id("call"), name="get_quote", arguments={"symbols": [self._symbol]}),
        )
        if not result.ok or result.snapshot is None:
            error = result.content.get("error", {})
            store.transition_run(run_id, RunStatus.FAILED, error=error)
            raise SocAgentsError(
                f"No quote for {self._symbol}: {error.get('message', 'unknown error')}",
                code=result.error_code or "no_quote",
            )
        store.transition_run(run_id, RunStatus.COMPLETED, output={"snapshot": result.snapshot.id})
        self._snapshots[result.snapshot.id] = result.snapshot
        self._payloads.update(gateway.payloads)
        self._trusted.update(gateway.trusted)
        if result.snapshot.as_of is not None:
            self._as_of = result.snapshot.as_of
        quote = result.content["data"]["quotes"][0]
        return float(quote["last"]), result.snapshot.id

    async def _analyst(self, name: str, base: dict[str, Any]) -> AnalystReport | None:
        role = ROLES[name]
        session = (
            "the next session (the market is closed, so the data is from the last session)"
            if self._market is not None and not self._market.is_open
            else "today's session"
        )
        report = await self._role_output(
            name,
            base,
            AnalystReport,
            f"Analyze {self._symbol} for {session} as the {role.title}.",
        )
        if report is None:
            return None
        numbers = self._numbers()
        known = set(numbers)
        levels, removed = verify_levels(report.key_levels, numbers, owner=name)
        self._removed.extend(removed)
        report = report.model_copy(
            update={
                "role": name,
                "key_levels": levels,
                "evidence": known_evidence(report.evidence, known),
                "signals": [
                    s.model_copy(update={"evidence": known_evidence(s.evidence, known)})
                    for s in report.signals
                ],
            }
        )
        self._desk_store.save_analyst_report(
            self._desk_run_id, role=name, run_id=None, report=report, mode=self._mode
        )
        self._emit(
            "analyst.completed",
            name,
            stance=report.stance,
            confidence=report.confidence,
            summary=report.summary,
        )
        return report

    async def _role_output[T: BaseModel](
        self, name: str, context: dict[str, Any], output_model: type[T], task: str
    ) -> T | None:
        role = ROLES[name]
        model = self._models[name]
        tool_names = role.tools + (role.member_tools if self._session.is_member else ())
        registry = registry_for(tool_names)
        for tool in self._external_tools.get(name, []):
            registry.register(tool)
        gateway = ToolGateway(registry, self._store, self._settings, replay=self._replay)
        runtime = NativeLoopRuntime(
            model=model,
            gateway=gateway,
            store=self._store,
            settings=self._settings,
            budget=Budget(
                max_steps=role.max_steps,
                max_output_tokens=role.max_output_tokens * role.max_steps,
                max_seconds=300.0,
                max_tokens_per_call=role.max_output_tokens,
            ),
            prices=self._prices,
            role=name,
            observer=self._observe,
        )
        applied = [s for s in self._skills if name in s.manifest.applies_to]
        if applied:
            context = {**context, "skills": [s.context_entry() for s in applied]}
        system = system_prompt(
            role,
            symbol=self._symbol,
            mode=self._mode,
            output_model=output_model,
            with_skills=bool(applied),
        )
        self._emit("role.started", name)
        result = await runtime.run(
            kind=f"desk.{name}",
            system=system,
            user_message=user_message(task, context),
            provider=self._session.provider,
            input_meta={"desk_run_id": self._desk_run_id, "role": name},
            member=self._session.is_member,
        )
        self._usage.add(
            calls=result.steps,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost=result.cost_usd,
        )
        for snapshot in result.snapshots:
            self._snapshots[snapshot.id] = snapshot
        self._payloads.update(gateway.payloads)
        self._trusted.update(gateway.trusted)
        self._notices.extend(result.notices)
        if result.status is not RunStatus.COMPLETED:
            self._emit(
                "role.failed", name, error=result.error.model_dump() if result.error else None
            )
            return None
        try:
            parsed, repair = await parse_or_repair(
                model,
                system=system,
                text=result.answer or "",
                output_model=output_model,
                max_tokens=role.max_output_tokens,
            )
        except ModelError as exc:
            self._emit("role.failed", name, error={"code": exc.code, "message": str(exc)})
            return None
        if repair.input_tokens or repair.output_tokens:
            cost = cost_usd(self._prices, model.provider, model.model, repair)
            self._usage.add(
                calls=1,
                input_tokens=repair.input_tokens,
                output_tokens=repair.output_tokens,
                cost=cost,
            )
            self._store.record_usage(
                run_id=result.run_id,
                provider=model.provider,
                model=model.model,
                role=f"{name}.repair",
                input_tokens=repair.input_tokens,
                output_tokens=repair.output_tokens,
                cost_usd=cost,
            )
        self._emit("role.completed", name)
        return parsed

    # deterministic risk step

    def _review(self, ideas: list[DeskIdea]) -> list[ReviewedIdea]:
        freshness = self._freshness()
        live = self._mode == "member" and not freshness.delayed and self._replay is None
        check_mode = "execution" if live else "educational"
        ctx = RiskContext(
            today=self._as_of.astimezone(ET).date(),
            check_mode=check_mode,
            data_age_sec=freshness.oldest_snapshot_sec or 0,
            delayed=freshness.delayed,
        )
        known = set(self._payloads)
        reviewed: list[ReviewedIdea] = []
        for raw in ideas:
            idea = raw.model_copy(
                update={"symbol": self._symbol, "evidence": known_evidence(raw.evidence, known)}
            )
            try:
                decision = evaluate(idea.to_trade_idea(), self._risk_profile, ctx)
                if not self._trusted.intersection(idea.evidence):
                    decision = RiskDecision(
                        decision="deny",
                        check_mode=ctx.check_mode,
                        reasons=[
                            RiskReason(
                                code="no_trusted_evidence",
                                message="The idea cites no trusted data snapshot.",
                            )
                        ],
                        suggested_fixes=[],
                        profile_version=self._risk_profile.version,
                        max_loss_usd=decision.max_loss_usd,
                    )
            except ValidationError as exc:
                decision = RiskDecision(
                    decision="deny",
                    check_mode=ctx.check_mode,
                    reasons=[RiskReason(code="invalid_idea", message=str(exc)[:200])],
                    suggested_fixes=[],
                    profile_version=self._risk_profile.version,
                    max_loss_usd=None,
                )
            convertible, reason = self._convertible(idea, decision)
            reviewed.append(
                ReviewedIdea(
                    idea=idea,
                    risk_check=IdeaRiskCheck(
                        mode=decision.check_mode,
                        decision=decision.decision,
                        display=decision.display,
                        reasons=decision.reasons,
                        suggested_fixes=decision.suggested_fixes,
                        max_loss_usd=decision.max_loss_usd,
                    ),
                    convertible=convertible,
                    not_convertible_reason=reason,
                    label=self._label(decision.check_mode),
                )
            )
        return reviewed

    def _label(self, check_mode: str) -> str | None:
        if self._replay is not None:
            return REPLAY_LABEL
        return EDUCATIONAL_LABEL if check_mode == "educational" else None

    def _convertible(self, idea: DeskIdea, decision: RiskDecision) -> tuple[bool, str | None]:
        if self._replay is not None:
            return False, "Replays compare models and prompts on recorded data; never convertible."
        member = self._session.member
        if self._mode != "member" or member is None:
            return False, "Community ideas are educational and never convertible."
        if idea.legs > 1:
            return False, "Multi-leg ideas are analysis only."
        if not decision.executable:
            return False, "The risk check did not pass on real-time data."
        if PREORDER_ENTITLEMENT not in member.entitlements:
            return False, "Needs the SocSwift pre-order entitlement."
        return True, None

    # helpers

    def _numbers(self) -> dict[str, list[float]]:
        return {sid: numbers_in(payload) for sid, payload in self._payloads.items()}

    def _freshness(self) -> DataFreshness:
        now = utcnow()
        ages = [int((now - s.as_of).total_seconds()) for s in self._snapshots.values() if s.as_of]
        delayed = any((s.delayed_sec or 0) > 0 for s in self._snapshots.values())
        return DataFreshness(oldest_snapshot_sec=max(ages) if ages else None, delayed=delayed)
