"""X OAuth 2.0 user-context credentials.

Read from the environment first, then the OS keychain, the same order as the SocSwift key.
Access tokens are short lived and never stored. Refresh tokens rotate on every use, so a
rotated token is written back to the keychain and the old one stops working immediately.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import httpx
import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from socagents import __version__
from socagents.core.credentials import SERVICE
from socagents.core.errors import ConfigError
from socagents.core.timeutil import utcnow

TOKEN_URL = "https://api.x.com/2/oauth2/token"
REFRESH_MARGIN = timedelta(seconds=60)
SCOPES = "tweet.read tweet.write users.read offline.access"

FIELDS = ("client_id", "client_secret", "refresh_token", "access_token")
ENV_VARS = {name: f"X_{name.upper()}" for name in FIELDS}
ACCOUNTS = {name: f"x_{name}" for name in FIELDS}


def _read(field: str) -> str | None:
    value = os.environ.get(ENV_VARS[field])
    if value and value.strip():
        return value.strip()
    try:
        stored = keyring.get_password(SERVICE, ACCOUNTS[field])
    except KeyringError:
        return None
    return stored.strip() if stored else None


def source(field: str) -> Literal["env", "keychain"] | None:
    if os.environ.get(ENV_VARS[field]):
        return "env"
    try:
        return "keychain" if keyring.get_password(SERVICE, ACCOUNTS[field]) else None
    except KeyringError:
        return None


def save(field: str, value: str) -> None:
    try:
        keyring.set_password(SERVICE, ACCOUNTS[field], value.strip())
    except KeyringError as exc:
        raise ConfigError(
            f"No OS keychain is available ({type(exc).__name__}). "
            f"Set {ENV_VARS[field]} in the environment instead."
        ) from exc


def forget() -> int:
    """Delete every stored X credential. Returns how many were removed."""
    removed = 0
    for field in FIELDS:
        try:
            keyring.delete_password(SERVICE, ACCOUNTS[field])
        except (PasswordDeleteError, KeyringError):
            continue
        removed += 1
    return removed


@dataclass(frozen=True)
class XCredentials:
    client_id: str = ""
    client_secret: str | None = None
    refresh_token: str = ""
    access_token: str | None = None

    @property
    def can_refresh(self) -> bool:
        return bool(self.client_id and self.refresh_token)


def load_credentials() -> XCredentials:
    creds = XCredentials(
        client_id=_read("client_id") or "",
        client_secret=_read("client_secret"),
        refresh_token=_read("refresh_token") or "",
        access_token=_read("access_token"),
    )
    if creds.can_refresh or creds.access_token:
        return creds
    raise ConfigError(
        "No X credentials. Run `socagents x login`, or set X_CLIENT_ID and X_REFRESH_TOKEN "
        f"(scopes: {SCOPES}). A short-lived X_ACCESS_TOKEN alone is enough for a dry run.",
        code="x_no_credentials",
    )


class TokenProvider:
    """Hands out a bearer token, refreshing it shortly before it expires.

    A bare ``X_ACCESS_TOKEN`` with no refresh token is used as given and never refreshed:
    it expires in about two hours, which is fine for one dry run and not for a long poll.
    """

    def __init__(
        self,
        creds: XCredentials,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        token_url: str = TOKEN_URL,
        timeout_s: float = 15.0,
    ) -> None:
        self._creds = creds
        self._url = token_url
        self._static = None if creds.can_refresh else creds.access_token
        self._token: str | None = self._static
        self._expires_at: datetime | None = None
        self._client = httpx.AsyncClient(
            timeout=timeout_s,
            transport=transport,
            headers={"user-agent": f"socagents/{__version__}"},
        )
        self.notices: list[str] = []

    async def aclose(self) -> None:
        await self._client.aclose()

    async def token(self) -> str:
        if self._static is not None:
            return self._static
        fresh_enough = self._expires_at is not None and utcnow() < self._expires_at - REFRESH_MARGIN
        if self._token is not None and fresh_enough:
            return self._token
        return await self._refresh()

    async def _refresh(self) -> str:
        form = {"grant_type": "refresh_token", "refresh_token": self._creds.refresh_token}
        headers = {}
        if self._creds.client_secret:
            pair = f"{self._creds.client_id}:{self._creds.client_secret}".encode()
            headers["authorization"] = f"Basic {base64.b64encode(pair).decode()}"
        else:
            form["client_id"] = self._creds.client_id
        try:
            response = await self._client.post(self._url, data=form, headers=headers)
        except httpx.HTTPError as exc:
            raise ConfigError(
                f"X token endpoint is unreachable ({type(exc).__name__}).", code="x_unavailable"
            ) from exc
        if response.status_code >= 400:
            raise ConfigError(
                f"X rejected the refresh token (HTTP {response.status_code}). Refresh tokens "
                "rotate on every use and are single use. Run `socagents x login` again.",
                code="x_auth",
            )
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise ConfigError("X returned no access token.", code="x_auth")
        self._token = str(token)
        self._expires_at = utcnow() + timedelta(seconds=int(payload.get("expires_in", 7200)))
        self._rotate(payload.get("refresh_token"))
        return self._token

    def _rotate(self, new_refresh: str | None) -> None:
        if not new_refresh or new_refresh == self._creds.refresh_token:
            return
        was_env = source("refresh_token") == "env"
        self._creds = XCredentials(
            client_id=self._creds.client_id,
            client_secret=self._creds.client_secret,
            refresh_token=new_refresh,
            access_token=None,
        )
        try:
            save("refresh_token", new_refresh)
        except ConfigError:
            self.notices.append(
                "X rotated the refresh token but there is no keychain to store it in. The next "
                "run will fail: capture the new token or re-run `socagents x login`."
            )
            return
        if was_env:
            self.notices.append(
                f"X rotated the refresh token. {ENV_VARS['refresh_token']} in your environment "
                "is now stale; the new token went to the keychain. Unset the variable."
            )
