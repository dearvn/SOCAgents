"""Client for the SocSwift Agent API v1 (``/api/agent/v1``).

Every response carries ``as_of`` (UTC) and ``delayed``. Membership is checked on every call.
SOCAgents refuses API keys with the ``full_access`` scope: create a scoped key instead.

The payload shapes here follow the draft v1 contract and will track SocSwift's published
OpenAPI document.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from socagents import __version__
from socagents.core.errors import ProviderError, SocAgentsError

DEFAULT_BASE_URL = "https://trade.socswift.com/api/agent/v1"
FORBIDDEN_SCOPES = {"full_access"}


class MembershipError(SocAgentsError):
    code = "membership_required"


class MemberInfo(BaseModel):
    model_config = ConfigDict(extra="allow")

    user_id: str
    plan: str | None = None
    entitlements: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)
    sim: bool = False


class SocSwiftClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 15.0,
    ) -> None:
        self._base = (base_url or os.environ.get("SOCSWIFT_API_URL") or DEFAULT_BASE_URL).rstrip(
            "/"
        )
        self._client = httpx.AsyncClient(
            timeout=timeout_s,
            transport=transport,
            headers={
                "authorization": f"Bearer {api_key}",
                "user-agent": f"socagents/{__version__}",
                "accept": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = await self._client.get(f"{self._base}{path}", params=params)
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"SocSwift is unreachable ({type(exc).__name__}).", code="socswift_unavailable"
            ) from exc
        if response.status_code == 401:
            raise MembershipError(
                "The SocSwift API key is invalid or revoked. Run `socagents login`.",
                code="invalid_api_key",
            )
        if response.status_code in (402, 403):
            raise MembershipError("This needs an active SocSwift membership.")
        if response.status_code == 404:
            raise ProviderError(f"SocSwift has no data for {path}.", code="symbol_not_found")
        if response.status_code == 429:
            raise ProviderError(
                "SocSwift rate limit reached. Try again shortly.", code="rate_limited"
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"SocSwift returned HTTP {response.status_code}.", code="socswift_unavailable"
            )
        try:
            data: dict[str, Any] = response.json()
        except ValueError as exc:
            raise ProviderError(
                "SocSwift returned a non-JSON response.", code="socswift_unavailable"
            ) from exc
        return data

    async def me(self) -> MemberInfo:
        info = MemberInfo.model_validate(await self._get("/me"))
        if FORBIDDEN_SCOPES & set(info.scopes):
            raise MembershipError(
                "SOCAgents refuses full_access API keys. Create a scoped read-only key in "
                "SocSwift and run `socagents login` again.",
                code="full_access_key_rejected",
            )
        return info

    async def quotes(self, symbols: list[str]) -> dict[str, Any]:
        return await self._get("/quotes", {"symbols": ",".join(symbols)})

    async def bars(self, symbol: str, interval: str, lookback: int) -> dict[str, Any]:
        return await self._get(f"/bars/{symbol}", {"interval": interval, "lookback": lookback})

    async def option_chain(self, symbol: str, expiration: str | None = None) -> dict[str, Any]:
        params = {"expiration": expiration} if expiration else None
        return await self._get(f"/options/{symbol}/chain", params)

    async def gex(self, symbol: str) -> dict[str, Any]:
        return await self._get(f"/gex/{symbol}")

    async def flow(self, symbol: str, dte: str = "0-1", limit: int = 10) -> dict[str, Any]:
        return await self._get(f"/flow/{symbol}", {"dte": dte, "limit": limit})

    async def hedge_flow(self, family: str) -> dict[str, Any]:
        return await self._get(f"/hedge-flow/{family}")
