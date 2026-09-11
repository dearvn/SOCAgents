from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from socagents.core.config import KillSwitches, Settings
from socagents.db.store import Store
from socagents.providers.fixture import FixtureProvider
from socagents.tools.sdk import ToolContext

ENV_VARS = (
    "AGENTS",
    "AGENT_ORDERS",
    "AGENT_LIVE",
    "AGENT_SCHEDULER",
    "SOCAGENTS_MODEL",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "OLLAMA_BASE_URL",
)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SOCAGENTS_HOME", str(tmp_path / "home"))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(home=tmp_path / "home", kill_switches=KillSwitches())


@pytest.fixture
def store() -> Iterator[Store]:
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture
def ctx(store: Store) -> ToolContext:
    run_id = store.create_run(kind="test", mode="community", model="test/test", input={})
    return ToolContext(run_id=run_id, provider=FixtureProvider(), mode="community")
