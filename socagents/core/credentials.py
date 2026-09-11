"""SocSwift API key storage: the ``SOCSWIFT_API_KEY`` environment variable or the OS keychain."""

from __future__ import annotations

import os
from typing import Literal

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from socagents.core.errors import ConfigError

SERVICE = "socagents"
ACCOUNT = "socswift_api_key"
ENV_VAR = "SOCSWIFT_API_KEY"


def load_api_key() -> str | None:
    value = os.environ.get(ENV_VAR)
    if value:
        return value.strip()
    try:
        stored = keyring.get_password(SERVICE, ACCOUNT)
    except KeyringError:
        return None
    return stored or None


def key_source() -> Literal["env", "keychain"] | None:
    if os.environ.get(ENV_VAR):
        return "env"
    try:
        return "keychain" if keyring.get_password(SERVICE, ACCOUNT) else None
    except KeyringError:
        return None


def save_api_key(key: str) -> None:
    try:
        keyring.set_password(SERVICE, ACCOUNT, key.strip())
    except KeyringError as exc:
        raise ConfigError(
            f"No OS keychain is available ({type(exc).__name__}). "
            f"Set {ENV_VAR} in the environment instead."
        ) from exc


def delete_api_key() -> bool:
    try:
        keyring.delete_password(SERVICE, ACCOUNT)
    except PasswordDeleteError:
        return False
    except KeyringError:
        return False
    return True
