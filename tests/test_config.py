from __future__ import annotations

from pathlib import Path

import pytest

from socagents.core.config import KillSwitches, Settings, env_flag


@pytest.mark.parametrize(
    ("raw", "expected"), [("1", True), ("on", True), ("0", False), ("False", False), ("", True)]
)
def test_env_flag(monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool) -> None:
    monkeypatch.setenv("FLAG", raw)
    assert env_flag("FLAG", True) is expected


def test_env_flag_rejects_unknown_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLAG", "maybe")
    with pytest.raises(ValueError):
        env_flag("FLAG", True)


def test_kill_switch_defaults_keep_orders_off() -> None:
    switches = KillSwitches.from_env()
    assert switches == KillSwitches(
        agents=True, agent_orders=False, agent_live=False, agent_scheduler=False
    )


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SOCAGENTS_HOME", str(tmp_path))
    monkeypatch.setenv("AGENTS", "0")
    settings = Settings.from_env()
    assert settings.db_path == tmp_path / "socagents.db"
    assert settings.kill_switches.agents is False
