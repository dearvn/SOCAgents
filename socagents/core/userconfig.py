"""User preferences stored in ``$SOCAGENTS_HOME/config.json``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from socagents.core.errors import ConfigError

CONFIG_FILE = "config.json"


class UserConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    upsell: bool = True
    telemetry: bool = False
    default_provider: Literal["community", "fixture", "socswift"] | None = None
    default_model: str | None = None
    default_profile: Literal["lite", "standard", "deep"] = "standard"
    skills: list[str] = Field(default_factory=list)


def load_user_config(home: Path) -> UserConfig:
    path = home / CONFIG_FILE
    if not path.is_file():
        return UserConfig()
    try:
        return UserConfig.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, ValidationError) as exc:
        raise ConfigError(f"Invalid config file {path}: {exc}") from exc


def save_user_config(home: Path, config: UserConfig) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / CONFIG_FILE
    path.write_text(config.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def set_config_value(config: UserConfig, key: str, raw: str) -> UserConfig:
    if key not in UserConfig.model_fields:
        raise ConfigError(
            f"Unknown setting {key!r}. Settings: {', '.join(UserConfig.model_fields)}."
        )
    value: object = raw
    if key == "skills":
        value = [s.strip() for s in raw.split(",") if s.strip()]
    elif raw.lower() in {"none", "null", ""}:
        value = None
    elif raw.lower() in {"true", "on", "yes", "1"}:
        value = True
    elif raw.lower() in {"false", "off", "no", "0"}:
        value = False
    try:
        return config.model_copy(update={key: value}, deep=True).model_validate(
            {**config.model_dump(), key: value}
        )
    except ValidationError as exc:
        raise ConfigError(f"Invalid value for {key}: {raw!r}.") from exc
