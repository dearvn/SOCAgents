from __future__ import annotations

import io
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from socagents.agents.desk import run_desk
from socagents.core.config import Settings
from socagents.core.errors import ConfigError, ModelError, SocAgentsError
from socagents.core.timeutil import ET
from socagents.core.userconfig import UserConfig
from socagents.desk.graph import DeskEvent, DeskGraph
from socagents.desk.live import DeskLiveView
from socagents.desk.models import EDUCATIONAL_LABEL, DeskReport
from socagents.desk.render import render_report, shareable_markdown
from socagents.desk.roles import PROFILES, ROLES, parse_context, profile_roles
from socagents.desk.storage import DeskStore
from socagents.desk.structured import extract_json
from socagents.growth import Upsell
from socagents.model_gateway.scripted import ScriptedModel
from socagents.model_gateway.types import Message, ModelProvider, ModelResponse, ToolSpec, Usage
from socagents.providers.fixture import FixtureProvider
from socagents.session import Session
from socagents.socswift_client.client import MemberInfo


async def desk(
    settings: Settings,
    symbol: str = "SPY",
    profile: str = "standard",
    events: list[DeskEvent] | None = None,
    **kwargs: Any,
) -> DeskReport:
    return await run_desk(
        symbol=symbol,
        profile_name=profile,
        rounds=kwargs.pop("rounds", None),
        model_args=kwargs.pop("model_args", []),
        provider_name="fixture",
        settings=settings,
        on_event=events.append if events is not None else None,
        **kwargs,
    )


def session(
    settings: Settings, provider: FixtureProvider | None = None, member: MemberInfo | None = None
) -> Session:
    return Session(
        settings,
        UserConfig(),
        "fixture",
        provider or FixtureProvider(),
        Upsell(enabled=True, source="cli"),
        member=member,
    )


async def graph_run(
    settings: Settings,
    models: dict[str, ModelProvider],
    sess: Session | None = None,
    profile: str = "standard",
) -> DeskReport:
    sess = sess or session(settings)
    store = sess.open_store()
    try:
        return await DeskGraph(session=sess, store=store, models=models).run(
            "SPY", PROFILES[profile], PROFILES[profile].debate_rounds
        )
    finally:
        store.close()


def scripted_models(
    profile: str = "standard", **overrides: ModelProvider
) -> dict[str, ModelProvider]:
    chosen = PROFILES[profile]
    models: dict[str, ModelProvider] = {
        r: ScriptedModel() for r in profile_roles(chosen, chosen.debate_rounds)
    }
    models.update(overrides)
    return models


# end-to-end offline


async def test_standard_desk_offline(settings: Settings) -> None:
    events: list[DeskEvent] = []
    report = await desk(settings, events=events)

    assert report.mode == "community" and report.profile == "standard"
    assert [a.role for a in report.analysts] == [
        "dealer_positioning",
        "flow",
        "technical",
        "event_news",
    ]
    assert [(t.round, t.side) for t in report.debate] == [(1, "bull"), (1, "bear")]
    assert report.regime.startswith("positive gamma")
    assert report.key_levels and all(level.evidence for level in report.key_levels)
    assert {s.name for s in report.scenarios} == {"base", "bull", "bear"}
    assert report.missing_roles == []
    assert report.removed_unverified == []

    [idea] = report.ideas
    assert idea.idea.structure == "long_call"
    assert idea.idea.price_basis == "premium"
    assert idea.risk_check.mode == "educational"
    assert idea.label == EDUCATIONAL_LABEL
    assert idea.convertible is False
    assert idea.risk_check.max_loss_usd == pytest.approx(
        (idea.idea.est_entry_premium - (idea.idea.stop or 0)) * 100, abs=0.01
    )
    assert idea.risk_check.critique

    assert any("utm_source=cli" in n for n in report.notices)
    assert "headline(s) looked like instructions" in report.analysts[3].summary
    assert report.data_freshness.delayed is True
    assert report.usage.model_calls > 0

    types = [e.type for e in events]
    assert types[0] == "desk.started" and types[-1] == "report.completed"
    assert types.count("analyst.completed") == 4
    assert "debate.turn" in types and "strategist.idea" in types


async def test_lite_profile(settings: Settings) -> None:
    report = await desk(settings, profile="lite")
    assert [a.role for a in report.analysts] == ["dealer_positioning", "technical"]
    assert report.debate == [] and report.ideas == []
    assert report.models.keys() == {"dealer_positioning", "technical", "desk_lead"}


async def test_bearish_symbol_gets_a_put(settings: Settings) -> None:
    report = await desk(settings, symbol="QQQ")
    assert [i.idea.right for i in report.ideas] == ["put"]


async def test_rounds_override(settings: Settings) -> None:
    report = await desk(settings, rounds=2)
    assert [t.round for t in report.debate] == [1, 1, 2, 2]


async def test_deep_profile_needs_membership(settings: Settings) -> None:
    with pytest.raises(ConfigError) as info:
        await desk(settings, profile="deep")
    assert info.value.code == "requires_membership"


async def test_unknown_profile_and_role(settings: Settings) -> None:
    with pytest.raises(ConfigError):
        await desk(settings, profile="turbo")
    with pytest.raises(ConfigError):
        await desk(settings, model_args=["chef=fixture/scripted"])


async def test_unknown_fixture_symbol(settings: Settings) -> None:
    with pytest.raises(ConfigError):
        await desk(settings, symbol="TSLA")


async def test_report_is_stored_and_found_by_prefix(settings: Settings) -> None:
    report = await desk(settings)
    sess = session(settings)
    store = sess.open_store()
    desk_store = DeskStore(store)
    try:
        assert desk_store.get_report(report.id) == report
        assert desk_store.get_report(report.desk_run_id[:12]) == report
        assert desk_store.list_reports()[0]["id"] == report.id
    finally:
        store.close()


# shareable output


async def test_shareable_view_strips_ideas(settings: Settings) -> None:
    report = await desk(settings)
    view = report.shareable_view()
    assert set(view) == {
        "symbol",
        "as_of",
        "mode",
        "profile",
        "regime",
        "key_levels",
        "scenarios",
        "data_freshness",
        "disclaimer",
        "made_with",
    }
    markdown = shareable_markdown(view)
    assert "Made with SOCAgents" in markdown
    assert "stop" not in markdown.lower() and "long_call" not in markdown


# failure handling


class FailingModel:
    provider = "test"
    model = "failing"

    async def complete(self, **kwargs: Any) -> ModelResponse:
        raise ModelError("provider down")

    async def aclose(self) -> None:
        return None


class ReplyModel:
    """Returns fixed texts in order, repeating the last."""

    provider = "test"
    model = "reply"

    def __init__(self, *texts: str, stop_reason: str = "end_turn") -> None:
        self.texts = list(texts)
        self.calls = 0
        self.stop_reason = stop_reason
        self.max_tokens: list[int] = []

    async def complete(
        self, *, system: str, messages: list[Message], tools: list[ToolSpec], max_tokens: int
    ) -> ModelResponse:
        self.calls += 1
        self.max_tokens.append(max_tokens)
        text = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        if callable(text):
            text = text(messages)
        return ModelResponse(
            text=text,
            model=self.model,
            usage=Usage(input_tokens=5, output_tokens=5),
            stop_reason=self.stop_reason,
        )

    async def aclose(self) -> None:
        return None


async def test_failed_analyst_is_reported_as_missing(settings: Settings) -> None:
    report = await graph_run(settings, scripted_models(flow=FailingModel()))
    assert report.missing_roles == ["flow"]
    assert "flow" not in [a.role for a in report.analysts]


async def test_all_analysts_failing_fails_the_desk(settings: Settings) -> None:
    models = scripted_models("lite", dealer_positioning=FailingModel(), technical=FailingModel())
    with pytest.raises(SocAgentsError) as info:
        await graph_run(settings, models, profile="lite")
    assert info.value.code == "desk_failed"


async def test_fabricated_levels_are_removed(settings: Settings) -> None:
    def lead(messages: list[Message]) -> str:
        ctx = parse_context(messages[0].content)
        return json.dumps(
            {
                "regime": "made up",
                "summary": "s",
                "key_levels": [
                    {"price": 12345.0, "kind": "invented", "evidence": ["snp_fake"]},
                    {"price": 1.0, "kind": "wrong number", "evidence": [ctx["spot_snapshot"]]},
                    {"price": ctx["spot"], "kind": "spot", "evidence": [ctx["spot_snapshot"]]},
                ],
                "scenarios": [
                    {
                        "name": "base",
                        "condition": "c",
                        "path": "p",
                        "evidence": ["snp_fake", ctx["spot_snapshot"]],
                    }
                ],
                "dissent": "",
            }
        )

    report = await graph_run(settings, scripted_models(desk_lead=ReplyModel(lead)))  # type: ignore[arg-type]
    assert [level.kind for level in report.key_levels] == ["spot"]
    assert len([r for r in report.removed_unverified if r.startswith("desk_lead")]) == 2
    assert report.scenarios[0].evidence == [report.key_levels[0].evidence[0]]


async def test_invalid_json_is_repaired_once(settings: Settings) -> None:
    valid = json.dumps(
        {"regime": "r", "summary": "s", "key_levels": [], "scenarios": [], "dissent": ""}
    )
    lead = ReplyModel("not json at all", valid)
    report = await graph_run(settings, scripted_models(desk_lead=lead))
    assert report.regime == "r"
    assert lead.calls == 2


async def test_lead_failure_fails_the_desk(settings: Settings) -> None:
    with pytest.raises(SocAgentsError) as info:
        await graph_run(settings, scripted_models(desk_lead=ReplyModel("nope", "still nope")))
    assert info.value.code == "desk_failed"


async def test_each_call_gets_the_roles_output_limit(settings: Settings) -> None:
    valid = json.dumps(
        {"regime": "r", "summary": "s", "key_levels": [], "scenarios": [], "dissent": ""}
    )
    lead = ReplyModel(valid)
    await graph_run(settings, scripted_models(desk_lead=lead))
    assert lead.max_tokens == [ROLES["desk_lead"].max_output_tokens]


async def test_truncated_analyst_is_missing_and_not_repaired(settings: Settings) -> None:
    flow = ReplyModel('{"stance": "bullish", "summary": "cut', stop_reason="length")
    report = await graph_run(settings, scripted_models(flow=flow))
    assert report.missing_roles == ["flow"]
    assert flow.calls == 1


async def test_truncated_lead_fails_the_desk_without_a_repair(settings: Settings) -> None:
    lead = ReplyModel("{}", stop_reason="max_tokens")
    with pytest.raises(SocAgentsError) as info:
        await graph_run(settings, scripted_models(desk_lead=lead))
    assert info.value.code == "desk_failed"
    assert lead.calls == 1


class PromptRecorder(ScriptedModel):
    """The offline model, recording each call's first user message."""

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []

    async def complete(self, **kwargs: Any) -> ModelResponse:
        self.prompts.append(kwargs["messages"][0].content)
        return await super().complete(**kwargs)


async def test_closed_market_reaches_the_prompts_and_the_report(settings: Settings) -> None:
    # The fixture's last trade is Thursday 2026-09-10 13:45 ET, so the market reads as closed.
    technical = PromptRecorder()
    report = await graph_run(settings, scripted_models(technical=technical))
    assert report.market is not None and report.market.status == "closed"
    assert report.market.last_session == date(2026, 9, 10)
    assert "for the next session" in technical.prompts[0]
    assert parse_context(technical.prompts[0])["market"]["last_session"] == "2026-09-10 (Thursday)"


async def test_kill_switch(tmp_path: Path) -> None:
    from socagents.core.config import KillSwitches

    off = Settings(home=tmp_path, kill_switches=KillSwitches(agents=False))
    with pytest.raises(SocAgentsError) as info:
        await graph_run(off, scripted_models())
    assert info.value.code == "agents_disabled"


# member mode


def fresh_fixture(tmp_path: Path) -> FixtureProvider:
    """SPY fixture shifted to today, marked real-time, so execution checks apply."""
    source = json.loads(
        (Path(__file__).parents[1] / "socagents/providers/fixtures/SPY.json").read_text()
    )
    now = datetime.now(UTC).replace(microsecond=0)
    shift = timedelta(days=(now.astimezone(ET).date() - date(2026, 9, 10)).days)

    def move(value: str) -> str:
        return (datetime.fromisoformat(value) + shift).isoformat()

    source["meta"].update({"as_of": now.isoformat(), "delayed_sec": 0})
    for c in source["chain"]:
        c["expiration"] = (date.fromisoformat(c["expiration"]) + shift).isoformat()
    for bar in source["bars"]["bars"]:
        bar["ts"] = move(bar["ts"])
    for event in source["events"]:
        event["time"] = move(event["time"])
    (tmp_path / "SPY.json").write_text(json.dumps(source))
    return FixtureProvider(data_dir=tmp_path)


async def test_member_desk_runs_execution_checks(settings: Settings, tmp_path: Path) -> None:
    member = MemberInfo(user_id="u1", entitlements=["agents", "execution_preorder"])
    (tmp_path / "data").mkdir()
    sess = session(settings, fresh_fixture(tmp_path / "data"), member=member)
    report = await graph_run(settings, scripted_models(), sess=sess)

    assert report.mode == "member"
    assert not any("utm_source" in n for n in report.notices)
    [idea] = report.ideas
    assert idea.risk_check.mode == "execution"
    assert idea.label is None
    assert idea.risk_check.decision == "allow"
    assert idea.convertible is True

    with pytest.raises(SocAgentsError) as info:
        report.shareable_view()
    assert info.value.code == "member_data_not_shareable"

    store = sess.open_store()
    try:
        raw = store.connection.execute("SELECT report FROM desk_reports").fetchone()["report"]
        assert raw.startswith("enc:v1:")
        assert DeskStore(store).get_report(report.id) == report
        assert DeskStore(store).purge_member() == 1
        assert DeskStore(store).list_reports() == []
    finally:
        store.close()


async def test_member_without_preorder_entitlement(settings: Settings, tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    sess = session(
        settings,
        fresh_fixture(tmp_path / "data"),
        member=MemberInfo(user_id="u1", entitlements=["agents"]),
    )
    report = await graph_run(settings, scripted_models(), sess=sess)
    assert report.ideas[0].convertible is False
    assert "entitlement" in (report.ideas[0].not_convertible_reason or "")


class NoEvidenceStrategist(ScriptedModel):
    """The offline strategist, with every idea's evidence removed."""

    async def complete(self, **kwargs: Any) -> ModelResponse:
        response = await super().complete(**kwargs)
        try:
            data = json.loads(response.text or "")
        except json.JSONDecodeError:
            return response
        if not isinstance(data, dict) or "ideas" not in data:
            return response
        for idea in data["ideas"]:
            idea["evidence"] = []
        return ModelResponse(text=json.dumps(data), model=response.model, usage=response.usage)


async def test_ideas_without_trusted_evidence_are_denied(
    settings: Settings, tmp_path: Path
) -> None:
    member = MemberInfo(user_id="u1", entitlements=["agents", "execution_preorder"])
    (tmp_path / "data").mkdir()
    sess = session(settings, fresh_fixture(tmp_path / "data"), member=member)
    report = await graph_run(
        settings, scripted_models(strategist=NoEvidenceStrategist()), sess=sess
    )
    [idea] = report.ideas
    assert idea.risk_check.decision == "deny"
    # B1: an option idea with no evidence at all fails the stricter
    # get_option_quote match, not the old generic "no trusted snapshot" check
    # (which a citation of ANY trusted snapshot — e.g. the shared spot quote —
    # used to satisfy without proving anything about this idea's own contract).
    assert [r.code for r in idea.risk_check.reasons] == ["no_matching_quote"]
    assert idea.convertible is False


class WrongStrikeStrategist(ScriptedModel):
    """The offline strategist's idea, but claiming a strike the cited
    get_option_quote snapshot never quoted — B1's "invented contract" shape."""

    async def complete(self, **kwargs: Any) -> ModelResponse:
        response = await super().complete(**kwargs)
        try:
            data = json.loads(response.text or "")
        except json.JSONDecodeError:
            return response
        if not isinstance(data, dict) or "ideas" not in data:
            return response
        for idea in data["ideas"]:
            idea["strike"] = (idea["strike"] or 0) + 50
        return ModelResponse(text=json.dumps(data), model=response.model, usage=response.usage)


async def test_idea_with_a_strike_the_quote_never_gave_is_denied(
    settings: Settings, tmp_path: Path
) -> None:
    member = MemberInfo(user_id="u1", entitlements=["agents", "execution_preorder"])
    (tmp_path / "data").mkdir()
    sess = session(settings, fresh_fixture(tmp_path / "data"), member=member)
    report = await graph_run(
        settings, scripted_models(strategist=WrongStrikeStrategist()), sess=sess
    )
    [idea] = report.ideas
    assert idea.risk_check.decision == "deny"
    assert [r.code for r in idea.risk_check.reasons] == ["no_matching_quote"]
    assert idea.convertible is False


class WrongPremiumStrategist(ScriptedModel):
    """The offline strategist's idea, contract untouched, but est_entry_premium
    far from the cited quote's mid — B1's "invented premium" shape, and
    C11's basis-enforcement path."""

    async def complete(self, **kwargs: Any) -> ModelResponse:
        response = await super().complete(**kwargs)
        try:
            data = json.loads(response.text or "")
        except json.JSONDecodeError:
            return response
        if not isinstance(data, dict) or "ideas" not in data:
            return response
        for idea in data["ideas"]:
            idea["est_entry_premium"] = round(float(idea["est_entry_premium"] or 0) * 3 + 1, 2)
        return ModelResponse(text=json.dumps(data), model=response.model, usage=response.usage)


async def test_idea_with_premium_far_from_the_quotes_mid_is_denied(
    settings: Settings, tmp_path: Path
) -> None:
    member = MemberInfo(user_id="u1", entitlements=["agents", "execution_preorder"])
    (tmp_path / "data").mkdir()
    sess = session(settings, fresh_fixture(tmp_path / "data"), member=member)
    report = await graph_run(
        settings, scripted_models(strategist=WrongPremiumStrategist()), sess=sess
    )
    [idea] = report.ideas
    assert idea.risk_check.decision == "deny"
    assert [r.code for r in idea.risk_check.reasons] == ["premium_not_verified"]
    assert idea.convertible is False


# rendering and helpers


async def test_render_and_live_view(settings: Settings) -> None:
    events: list[DeskEvent] = []
    report = await desk(settings, events=events)
    view = DeskLiveView("SPY", "standard", profile_roles(PROFILES["standard"], 1))
    for event in events:
        view.handle(event)
    out = io.StringIO()
    console = Console(file=out, width=140)
    console.print(view)
    console.print(render_report(report))
    text = out.getvalue()
    assert "done" in text and "Dealer Positioning Analyst" in text
    assert "Key levels" in text and "Educational" in text


async def test_short_and_full_report_views(settings: Settings) -> None:
    report = await desk(settings)

    def show(full: bool) -> str:
        out = io.StringIO()
        Console(file=out, width=160).print(render_report(report, full=full))
        return out.getvalue()

    short, full = show(False), show(True)
    assert "market closed · last session Thu 2026-09-10, 13:45 ET" in short
    assert "Analysts: " in short and "Key levels" in short and "Educational" in short
    assert f"socagents report show {report.id} --full" in short
    assert "Debate" not in short and "Dealer Positioning Analyst" not in short
    assert "snp_" not in short and "risk officer:" not in short
    assert "Debate" in full and "Dealer Positioning Analyst" in full and "snp_" in full
    assert "risk officer:" in full and "--full" not in full


def test_extract_json() -> None:
    assert extract_json('Here:\n```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('text {"a": {"b": 2}} tail') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        extract_json("no json")
    with pytest.raises(ValueError):
        extract_json("[1, 2]")
