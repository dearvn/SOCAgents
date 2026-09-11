"""Runtime settings read from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434/v1"


def env_flag(name: str, default: bool) -> bool:
    """Read a boolean environment variable. Unknown values fail loudly."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} must be one of 1/0, true/false, yes/no, on/off; got {raw!r}")


@dataclass(frozen=True)
class KillSwitches:
    """Global switches. Orders, live trading, and the scheduler stay off until later phases."""

    agents: bool = True
    agent_orders: bool = False
    agent_live: bool = False
    agent_scheduler: bool = False

    @classmethod
    def from_env(cls) -> KillSwitches:
        return cls(
            agents=env_flag("AGENTS", True),
            agent_orders=env_flag("AGENT_ORDERS", False),
            agent_live=env_flag("AGENT_LIVE", False),
            agent_scheduler=env_flag("AGENT_SCHEDULER", False),
        )


@dataclass(frozen=True)
class Settings:
    home: Path
    kill_switches: KillSwitches
    ollama_base_url: str = DEFAULT_OLLAMA_BASE_URL

    @property
    def db_path(self) -> Path:
        return self.home / "socagents.db"

    @classmethod
    def from_env(cls) -> Settings:
        home = Path(os.environ.get("SOCAGENTS_HOME") or Path.home() / ".socagents").expanduser()
        return cls(
            home=home,
            kill_switches=KillSwitches.from_env(),
            ollama_base_url=os.environ.get("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL),
        )
