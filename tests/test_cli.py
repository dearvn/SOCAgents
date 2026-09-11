from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from socagents import __version__
from socagents.agents.ask import extract_symbols
from socagents.cli.main import app

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


def test_community_provider_not_yet_available() -> None:
    result = runner.invoke(app, ["ask", "SPY", "--provider", "community"])
    assert result.exit_code == 2
    assert "v0.1" in result.output


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
