"""X credentials, in the two shapes the developer portal hands out.

OAuth 1.0a: the four values on the app's "Keys and tokens" tab. They never expire and need
no browser flow, but they belong to the account that owns the app.

OAuth 2.0: a rotating refresh token from the PKCE flow in :mod:`socagents.social.x.oauth`.
More setup, and it can authorize a separate bot account.

Both are read from the environment first, then the OS keychain, like the SocSwift key. When
a full OAuth 1.0a set is present it wins, because nothing about it can go stale mid-cron.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

import httpx
import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from socagents import __version__
from socagents.core.credentials import SERVICE
from socagents.core.errors import ConfigError
from socagents.core.timeutil import utcnow
from socagents.social.x.oauth1 import authorization_header

TOKEN_URL = "https://api.x.com/2/oauth2/token"
REFRESH_MARGIN = timedelta(seconds=60)
SCOPES = "tweet.read tweet.write users.read offline.access"

OAUTH1_FIELDS = ("api_key", "api_secret", "access_token", "access_token_secret")
OAUTH2_FIELDS = ("client_id", "client_secret", "refresh_token", "oauth2_access_token")
FIELDS = OAUTH1_FIELDS + OAUTH2_FIELDS
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


def shadowed(field: str) -> bool:
    """True when an environment variable is hiding a value that is also in the keychain."""
    if not os.environ.get(ENV_VARS[field]):
        return False
    try:
        return bool(keyring.get_password(SERVICE, ACCOUNTS[field]))
    except KeyringError:
        return False


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


class Authorizer(Protocol):
    """Signs one request. The client does not care which OAuth version is behind it."""

    kind: str

    async def header(self, method: str, url: str, params: dict[str, Any] | None) -> str: ...

    async def aclose(self) -> None: ...

    @property
    def notices(self) -> list[str]: ...


# OAuth 1.0a


@dataclass(frozen=True)
class OAuth1Credentials:
    api_key: str
    api_secret: str
    access_token: str
    access_token_secret: str


class OAuth1Auth:
    kind = "oauth1"

    def __init__(self, creds: OAuth1Credentials) -> None:
        self._creds = creds

    async def header(self, method: str, url: str, params: dict[str, Any] | None) -> str:
        return authorization_header(
            method=method,
            url=url,
            params=params,
            consumer_key=self._creds.api_key,
            consumer_secret=self._creds.api_secret,
            token=self._creds.access_token,
            token_secret=self._creds.access_token_secret,
        )

    async def aclose(self) -> None:
        """Nothing to close: signing is local and needs no network."""

    @property
    def notices(self) -> list[str]:
        return []


def load_oauth1() -> OAuth1Credentials | None:
    values = {field: _read(field) for field in OAUTH1_FIELDS}
    if not all(values.values()):
        return None
    return OAuth1Credentials(
        api_key=str(values["api_key"]),
        api_secret=str(values["api_secret"]),
        access_token=str(values["access_token"]),
        access_token_secret=str(values["access_token_secret"]),
    )


# OAuth 2.0


@dataclass(frozen=True)
class XCredentials:
    client_id: str = ""
    client_secret: str | None = None
    refresh_token: str = ""
    access_token: str | None = None

    @property
    def can_refresh(self) -> bool:
        return bool(self.client_id and self.refresh_token)


def load_oauth2() -> XCredentials | None:
    creds = XCredentials(
        client_id=_read("client_id") or "",
        client_secret=_read("client_secret"),
        refresh_token=_read("refresh_token") or "",
        access_token=_read("oauth2_access_token"),
    )
    return creds if creds.can_refresh or creds.access_token else None


class TokenProvider:
    """Hands out a bearer token, refreshing it shortly before it expires.

    A bare ``X_OAUTH2_ACCESS_TOKEN`` with no refresh token is used as given and never
    refreshed: it expires in about two hours, fine for one dry run and not for a long poll.
    """

    kind = "oauth2"

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
        self._notices: list[str] = []

    @property
    def notices(self) -> list[str]:
        return self._notices

    async def aclose(self) -> None:
        await self._client.aclose()

    async def header(self, method: str, url: str, params: dict[str, Any] | None) -> str:
        return f"Bearer {await self.token()}"

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
                "rotate on every use and are single use. Run `socagents x auth` again.",
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
            self._notices.append(
                "X rotated the refresh token but there is no keychain to store it in. The next "
                "run will fail: capture the new token or re-run `socagents x auth`."
            )
            return
        if was_env:
            self._notices.append(
                f"X rotated the refresh token. {ENV_VARS['refresh_token']} in your environment "
                "is now stale; the new token went to the keychain. Unset the variable."
            )


def load_authorizer() -> Authorizer:
    """OAuth 1.0a when a full set is stored, otherwise OAuth 2.0."""
    if (oauth1 := load_oauth1()) is not None:
        return OAuth1Auth(oauth1)
    if (oauth2 := load_oauth2()) is not None:
        return TokenProvider(oauth2)
    raise ConfigError(
        "No X credentials. Either run `socagents x login` with the four values from the "
        "app's Keys and tokens tab (API key and secret, access token and secret), or run "
        f"`socagents x auth` for the OAuth 2.0 browser flow (scopes: {SCOPES}).",
        code="x_no_credentials",
    )
