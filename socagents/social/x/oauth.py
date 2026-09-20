"""The OAuth 2.0 PKCE dance that produces the refresh token `socagents x login` stores.

X issues refresh tokens only through an authorization-code flow with a browser in it, so
this opens the consent page, catches the redirect on a local port, and swaps the code for
tokens. Run it once per bot account, and again whenever a refresh token is lost.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, cast
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from socagents import __version__
from socagents.core.errors import ConfigError
from socagents.social.x.auth import SCOPES, TOKEN_URL

AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8723/callback"

_PAGE = (
    "<html><body style='font:16px system-ui;padding:3rem'>"
    "<h2>{title}</h2><p>{body}</p></body></html>"
)


@dataclass(frozen=True)
class Pkce:
    verifier: str
    challenge: str


def new_pkce() -> Pkce:
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode()).digest()
    return Pkce(verifier, base64.urlsafe_b64encode(digest).decode().rstrip("="))


def authorize_url(
    *, client_id: str, redirect_uri: str, state: str, challenge: str, scope: str = SCOPES
) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


class _CallbackServer(HTTPServer):
    expected_path: str = "/callback"
    expected_state: str = ""
    code: str | None = None
    error: str | None = None


class _CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        """Keep the console clean: the CLI prints what matters."""

    def do_GET(self) -> None:
        server = cast(_CallbackServer, self.server)
        parsed = urlparse(self.path)
        if parsed.path != server.expected_path:
            self._reply(404, "Not this one", "Waiting for the X redirect.")
            return
        params = parse_qs(parsed.query)
        state = (params.get("state") or [""])[0]
        if error := (params.get("error") or [""])[0]:
            server.error = (params.get("error_description") or [error])[0]
            self._reply(400, "Authorization failed", server.error)
            return
        if state != server.expected_state:
            server.error = "The redirect carried the wrong state value."
            self._reply(400, "Authorization failed", server.error)
            return
        server.code = (params.get("code") or [""])[0]
        self._reply(200, "Authorized", "You can close this tab and go back to the terminal.")

    def _reply(self, status: int, title: str, body: str) -> None:
        page = _PAGE.format(title=title, body=body).encode()
        self.send_response(status)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)


def wait_for_code(*, redirect_uri: str, state: str, timeout_s: int = 300) -> str:
    """Serve the redirect URI until X sends the code back, or give up."""
    parsed = urlparse(redirect_uri)
    server = _CallbackServer((parsed.hostname or "127.0.0.1", parsed.port or 80), _CallbackHandler)
    server.expected_path = parsed.path or "/"
    server.expected_state = state
    server.timeout = 1.0
    deadline = time.monotonic() + timeout_s
    try:
        while server.code is None and server.error is None:
            if time.monotonic() > deadline:
                raise ConfigError(
                    f"No redirect from X within {timeout_s}s. Check that {redirect_uri} is "
                    "registered as a callback URI on the app, character for character.",
                    code="x_auth_timeout",
                )
            server.handle_request()
    finally:
        server.server_close()
    if server.error:
        raise ConfigError(f"X refused the authorization: {server.error}", code="x_auth")
    if not server.code:
        raise ConfigError("X returned no authorization code.", code="x_auth")
    return server.code


async def exchange_code(
    *,
    code: str,
    client_id: str,
    client_secret: str | None,
    redirect_uri: str,
    verifier: str,
    token_url: str = TOKEN_URL,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Swap the authorization code for an access token and a refresh token."""
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
        "client_id": client_id,
    }
    headers = {}
    if client_secret:
        pair = f"{client_id}:{client_secret}".encode()
        headers["authorization"] = f"Basic {base64.b64encode(pair).decode()}"
    async with httpx.AsyncClient(
        timeout=15.0, transport=transport, headers={"user-agent": f"socagents/{__version__}"}
    ) as client:
        try:
            response = await client.post(token_url, data=form, headers=headers)
        except httpx.HTTPError as exc:
            raise ConfigError(
                f"X token endpoint is unreachable ({type(exc).__name__}).", code="x_unavailable"
            ) from exc
    if response.status_code >= 400:
        raise ConfigError(
            f"X rejected the authorization code (HTTP {response.status_code}): "
            f"{response.text[:200]}",
            code="x_auth",
        )
    payload: dict[str, Any] = response.json()
    if not payload.get("refresh_token"):
        raise ConfigError(
            "X returned no refresh token. The app must request the offline.access scope.",
            code="x_auth",
        )
    return payload
