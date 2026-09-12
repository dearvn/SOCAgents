from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from socagents.agents.desk import run_desk, run_replay
from socagents.cli.main import app
from socagents.core.config import Settings
from socagents.core.errors import ConfigError, SocAgentsError
from socagents.core.userconfig import UserConfig
from socagents.db.store import Store
from socagents.desk.models import REPLAY_LABEL, DeskReport
from socagents.desk.replay import ReplayBook, compare_reports
from socagents.growth import Upsell
from socagents.model_gateway.types import ToolCall
from socagents.providers.fixture import FixtureProvider
from socagents.session import Session
from socagents.tools.catalog import full_registry
from socagents.tools.gateway import ToolGateway
from socagents.tools.sdk import ToolContext

runner = CliRunner()


async def original_run(settings: Settings) -> DeskReport:
    return await run_desk(
        symbol="SPY",
        profile_name="standard",
        rounds=None,
        model_args=[],
        provider_name="fixture",
        settings=settings,
    )


def no_data_session(settings: Settings, tmp_path: Path) -> Session:
    """A session whose provider has no data, so any live fetch would fail."""
    empty = tmp_path / "empty"
    empty.mkdir(exist_ok=True)
    return Session(
        settings,
        UserConfig(),
        "fixture",
        FixtureProvider(data_dir=empty),
        Upsell(enabled=False, source="cli"),
    )


async def replay(settings: Settings, tmp_path: Path, ref: str, **kwargs: Any) -> DeskReport:
    _, result = await run_replay(
        ref=ref,
        model_args=kwargs.pop("model_args", []),
        settings=settings,
        session=no_data_session(settings, tmp_path),
        **kwargs,
    )
    return result


async def test_replay_serves_recorded_data_only(settings: Settings, tmp_path: Path) -> None:
    first = await original_run(settings)
    again = await replay(settings, tmp_path, first.id)
    assert again.replay_of == first.id and again.id != first.id
    assert again.regime == first.regime
    assert [lvl.price for lvl in again.key_levels] == [lvl.price for lvl in first.key_levels]
    assert again.models == first.models
    assert again.missing_roles == []
    [idea] = again.ideas
    assert idea.convertible is False
    assert idea.label == REPLAY_LABEL
    assert idea.risk_check.mode == "educational"


async def test_replay_with_another_profile(settings: Settings, tmp_path: Path) -> None:
    first = await original_run(settings)
    again = await replay(settings, tmp_path, first.id[:12], profile_name="lite")
    assert again.profile == "lite" and again.debate == [] and again.ideas == []
    rows = {name: (before, after) for name, before, after in compare_reports(first, again)}
    assert rows["regime"][0] == rows["regime"][1]
    assert rows["flow"] == (rows["flow"][0], "missing")
    assert rows["ideas"][1] == "none"


async def test_replay_of_unknown_report(settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as info:
        await replay(settings, tmp_path, "rpt_nope")
    assert info.value.code == "report_not_found"


def test_replay_book_needs_stored_data() -> None:
    store = Store(":memory:")
    with pytest.raises(SocAgentsError) as info:
        ReplayBook.from_store(store, "desk_missing")
    store.close()
    assert info.value.code == "replay_unavailable"


class EmptyBook:
    def lookup(self, tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
        return None


async def test_gateway_refuses_calls_that_were_not_recorded(
    store: Store, settings: Settings, ctx: ToolContext
) -> None:
    gateway = ToolGateway(full_registry(), store, settings, replay=EmptyBook())
    result = await gateway.call(
        ctx, ToolCall(id="c", name="get_quote", arguments={"symbols": ["SPY"]})
    )
    assert result.error_code == "not_recorded"


def test_replay_command() -> None:
    first = runner.invoke(app, ["desk", "SPY", "--json", "--no-live"])
    report_id = json.loads(first.output)["id"]

    shown = runner.invoke(app, ["replay", report_id])
    assert shown.exit_code == 0, shown.output
    assert f"Replay of {report_id}" in shown.output
    assert "regime" in shown.output

    as_json = runner.invoke(app, ["replay", report_id, "--json"])
    data = json.loads(as_json.output)
    assert data["original"] == report_id
    assert data["replay"]["replay_of"] == report_id

    missing = runner.invoke(app, ["replay", "rpt_nope"])
    assert missing.exit_code == 2
