"""Encryption at rest for member data stored by the CLI.

The data key lives in the OS keychain. Deleting it on logout makes any leftover ciphertext
unreadable. Without a keychain, member payloads are not persisted at all.
"""

from __future__ import annotations

import keyring
from cryptography.fernet import Fernet, InvalidToken
from keyring.errors import KeyringError, PasswordDeleteError

from socagents.core.credentials import SERVICE

DATA_KEY_ACCOUNT = "member_data_key"
PREFIX = "enc:v1:"


class PayloadCipher:
    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    @classmethod
    def from_keyring(cls, *, create: bool = True) -> PayloadCipher | None:
        try:
            key = keyring.get_password(SERVICE, DATA_KEY_ACCOUNT)
            if not key and create:
                key = Fernet.generate_key().decode()
                keyring.set_password(SERVICE, DATA_KEY_ACCOUNT, key)
        except KeyringError:
            return None
        return cls(key.encode()) if key else None

    def encrypt(self, text: str) -> str:
        return PREFIX + self._fernet.encrypt(text.encode()).decode()

    def decrypt(self, token: str) -> str | None:
        try:
            return self._fernet.decrypt(token.removeprefix(PREFIX).encode()).decode()
        except InvalidToken:
            return None


def is_encrypted(text: str) -> bool:
    return text.startswith(PREFIX)


def delete_data_key() -> None:
    try:
        keyring.delete_password(SERVICE, DATA_KEY_ACCOUNT)
    except (PasswordDeleteError, KeyringError):
        return
