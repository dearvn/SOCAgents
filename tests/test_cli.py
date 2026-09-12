from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from socagents import __version__
from socagents.agents.ask import extract_symbols
from socagents.cli.main import app
from socagents.core.credentials import load_api_key
from socagents.socswift_client.client import MemberInfo, MembershipError

runner = CliRunner()


def test_definition_of_done_command() -> None:
    result = runner.invoke(app, ["ask", "SPY", "--provider", "fixture"])
    assert result.exit_code == 0, result.output
    assert "581.20" in result.output
    assert "Sources" in result.output
    assert "(synthetic)" in result.output
    assert "Not investment advice" in result.output


def test_ask_json_output() -> None:
    result = runner.invoke(app, ["ask", "Compare SPY and QQQ", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"] == "completed"
    assert data["model"] == "fixture/scripted"
    assert {s["tool"] for s in data["snapshots"]} == {
        "get_quote",
        "get_option_chain_summary",
        "get_bars",
    }


def test_ask_symbol_option() -> None:
    result = runner.invoke(app, ["ask", "where is it positioned?", "-s", "qqq", "--json"])
    assert result.exit_code == 0, result.output
    assert "QQQ" in json.loads(result.output)["answer"]


def test_unknown_symbol_is_a_usage_error() -> None:
    result = runner.invoke(app, ["ask", "TSLA"])
    assert result.exit_code == 2
    assert "symbol_not_found" in result.output


def test_question_without_symbol() -> None:
    result = runner.invoke(app, ["ask", "what is going on today?"])
    assert result.exit_code == 2
    assert "--symbol" in result.output


def test_community_provider_needs_a_model() -> None:
    result = runner.invoke(app, ["ask", "SPY", "--provider", "community"])
    assert result.exit_code == 2
    assert "Choose a model" in result.output


def test_agents_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTS", "0")
    result = runner.invoke(app, ["ask", "SPY"])
    assert result.exit_code == 1
    assert "agents_disabled" in result.output


def test_invalid_kill_switch_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTS", "maybe")
    result = runner.invoke(app, ["ask", "SPY"])
    assert result.exit_code == 2
    assert "config_error" in result.output


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_doctor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-value")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "Fixture data" in result.output
    assert "not reachable" in result.output
    assert "sk-secret-value" not in result.output


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("SPY", ["SPY"]),
        ("Where is $SPY vs QQQ today?", ["SPY", "QQQ"]),
        ("what is the GEX at ATM for SPY and SPY", ["SPY"]),
        ("what is going on", []),
    ],
)
def test_extract_symbols(text: str, expected: list[str]) -> None:
    assert extract_symbols(text) == expected


# desk, reports, config, membership


def desk_json(*args: str) -> dict[str, Any]:
    result = runner.invoke(app, ["desk", "SPY", "--json", *args])
    assert result.exit_code == 0, result.output
    data: dict[str, Any] = json.loads(result.output)
    return data


def test_desk_command_renders_report() -> None:
    result = runner.invoke(app, ["desk", "SPY", "--no-live"])
    assert result.exit_code == 0, result.output
    assert "SOC Desk · SPY · standard" in result.output
    assert "Educational" in result.output
    assert "Not investment advice" in result.output


def test_desk_json_and_profiles() -> None:
    data = desk_json("--profile", "lite")
    assert data["profile"] == "lite" and data["ideas"] == []
    assert runner.invoke(app, ["desk", "SPY", "--profile", "turbo"]).exit_code == 2
    deep = runner.invoke(app, ["desk", "SPY", "--profile", "deep"])
    assert deep.exit_code == 2 and "requires_membership" in deep.output


def test_report_list_show_and_export(tmp_path: Path) -> None:
    report_id = desk_json()["id"]
    listing = runner.invoke(app, ["report", "list"])
    assert report_id in listing.output
    shown = runner.invoke(app, ["report", "show", report_id[:12]])
    assert shown.exit_code == 0 and "Key levels" in shown.output
    assert "Debate" not in shown.output
    full = runner.invoke(app, ["report", "show", report_id, "--full"])
    assert full.exit_code == 0 and "Debate" in full.output

    exported = runner.invoke(app, ["report", "export", report_id])
    assert exported.exit_code == 0
    assert "Made with SOCAgents" in exported.output
    assert "stop" not in exported.output.lower()

    out = tmp_path / "report.json"
    result = runner.invoke(
        app, ["report", "export", report_id, "--format", "json", "--out", str(out)]
    )
    assert result.exit_code == 0
    assert "ideas" not in json.loads(out.read_text())

    missing = runner.invoke(app, ["report", "show", "rpt_missing"])
    assert missing.exit_code == 2


def test_config_commands_and_upsell_opt_out() -> None:
    assert "upsell = True" in runner.invoke(app, ["config", "list"]).output
    assert runner.invoke(app, ["config", "set", "upsell", "false"]).exit_code == 0
    assert runner.invoke(app, ["config", "get", "upsell"]).output.strip() == "False"
    assert runner.invoke(app, ["config", "set", "nope", "1"]).exit_code == 2
    assert not any("utm_source" in n for n in desk_json()["notices"])


def test_brief_command() -> None:
    result = runner.invoke(app, ["brief", "--symbols", "SPY,QQQ", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"] == "completed"
    assert "QQQ" in data["answer"]


def test_login_whoami_logout(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_check(key: str) -> MemberInfo:
        if key != "sk_good":
            raise MembershipError("This needs an active SocSwift membership.")
        return MemberInfo(user_id="u1", plan="market_intelligence", entitlements=["agents"])

    monkeypatch.setattr("socagents.cli.main._check_key", fake_check)
    bad = runner.invoke(app, ["login", "--api-key", "sk_bad", "--no-browser"])
    assert bad.exit_code == 2 and load_api_key() is None

    good = runner.invoke(app, ["login", "--api-key", "sk_good", "--no-browser"])
    assert good.exit_code == 0 and "market_intelligence" in good.output
    assert load_api_key() == "sk_good"

    assert "Member mode" in runner.invoke(app, ["whoami"]).output
    out = runner.invoke(app, ["logout"])
    assert out.exit_code == 0 and "Removed the API key" in out.output
    assert load_api_key() is None
    assert "Community mode" in runner.invoke(app, ["whoami"]).output


def test_login_without_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    result = runner.invoke(app, ["login", "--api-key", "sk_x", "--no-browser", "--no-verify"])
    assert result.exit_code == 0
    assert load_api_key() == "sk_x"
