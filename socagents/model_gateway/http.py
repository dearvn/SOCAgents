"""Shared HTTP plumbing for hosted model APIs: retries, errors, and client lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import httpx

from socagents.core.errors import ModelError

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


class HttpModelBase:
    provider: str = "http"
    model: str

    def __init__(
        self,
        *,
        timeout_s: float = 120.0,
        max_retries: int = 2,
        backoff_s: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(timeout=timeout_s, transport=transport)
        self._max_retries = max_retries
        self._backoff_s = backoff_s

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post_json(
        self, url: str, *, headers: dict[str, str], body: dict[str, Any]
    ) -> dict[str, Any]:
        attempt = 0
        while True:
            try:
                response = await self._client.post(url, headers=headers, json=body)
            except httpx.HTTPError as exc:
                if attempt < self._max_retries:
                    await self._sleep(attempt, None)
                    attempt += 1
                    continue
                raise ModelError(f"{self.provider} request failed: {type(exc).__name__}") from exc
            if response.status_code in RETRY_STATUS and attempt < self._max_retries:
                await self._sleep(attempt, response.headers.get("retry-after"))
                attempt += 1
                continue
            if response.status_code >= 400:
                detail = _error_text(response)
                raise ModelError(
                    f"{self.provider} returned HTTP {response.status_code}: {detail}",
                    code="model_http_error",
                )
            try:
                data: dict[str, Any] = response.json()
            except ValueError as exc:
                raise ModelError(f"{self.provider} returned a non-JSON response.") from exc
            return data

    async def _sleep(self, attempt: int, retry_after: str | None) -> None:
        delay = self._backoff_s * (2**attempt)
        if retry_after is not None:
            with contextlib.suppress(ValueError):
                delay = min(max(float(retry_after), delay), 30.0)
        if delay > 0:
            await asyncio.sleep(delay)


def _error_text(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:300]
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict) and "message" in error:
        return str(error["message"])[:300]
    if isinstance(error, str):
        return error[:300]
    return str(data)[:300]
