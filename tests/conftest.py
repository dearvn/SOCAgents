from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

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
    "SOCSWIFT_API_KEY",
    "SOCSWIFT_API_URL",
    "SOCAGENTS_PROVIDER",
)


class MemoryKeyring(KeyringBackend):
    """In-memory keyring so tests never touch the real OS keychain."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.items[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.items:
            raise PasswordDeleteError("not found")
        del self.items[(service, username)]


@pytest.fixture(autouse=True)
def memory_keyring() -> Iterator[MemoryKeyring]:
    ring = MemoryKeyring()
    previous = keyring.get_keyring()
    keyring.set_keyring(ring)
    yield ring
    keyring.set_keyring(previous)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SOCAGENTS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SOCAGENTS_PROVIDER", "fixture")


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
