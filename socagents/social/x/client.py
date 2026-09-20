"""Client for the X API v2 endpoints the reply bot needs: me, mentions, and post.

Every call costs money. X bills per post read and per post created, and a post that
contains a link costs more than ten times one that does not, which is why
:mod:`socagents.social.x.compose` strips links out of replies.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel

from socagents import __version__
from socagents.core.errors import SocAgentsError
from socagents.social.x.auth import TokenProvider

DEFAULT_BASE_URL = "https://api.x.com/2"

# X pay-per-use rates, used only to estimate what a cycle cost. Update when X changes them.
READ_USD = 0.005
POST_USD = 0.015
POST_WITH_LINK_USD = 0.20

TWEET_FIELDS = "created_at,author_id,conversation_id,referenced_tweets,lang"
USER_FIELDS = "username,name"


class XError(SocAgentsError):
    code = "x_error"


class XAuthError(XError):
    code = "x_auth"


class XAccessError(XError):
    code = "x_access"


class XRateLimited(XError):
    code = "x_rate_limited"

    def __init__(self, message: str, *, reset_at: int | None = None) -> None:
        super().__init__(message)
        self.reset_at = reset_at


class XUser(BaseModel):
    id: str
    username: str
    name: str | None = None


class Mention(BaseModel):
    id: str
    author_id: str
    text: str
    created_at: datetime
    conversation_id: str | None = None
    lang: str | None = None
    username: str | None = None
    is_retweet: bool = False

    @property
    def handle(self) -> str:
        return f"@{self.username}" if self.username else self.author_id


def _parse_mention(tweet: dict[str, Any], users: dict[str, dict[str, Any]]) -> Mention:
    referenced = tweet.get("referenced_tweets") or []
    author_id = str(tweet.get("author_id", ""))
    user = users.get(author_id, {})
    return Mention(
        id=str(tweet["id"]),
        author_id=author_id,
        text=str(tweet.get("text", "")),
        created_at=datetime.fromisoformat(str(tweet["created_at"]).replace("Z", "+00:00")),
        conversation_id=tweet.get("conversation_id"),
        lang=tweet.get("lang"),
        username=user.get("username"),
        is_retweet=any(r.get("type") == "retweeted" for r in referenced),
    )


class XClient:
    def __init__(
        self,
        tokens: TokenProvider,
        *,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 15.0,
    ) -> None:
        self._tokens = tokens
        self._base = (base_url or os.environ.get("X_API_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=timeout_s,
            transport=transport,
            headers={
                "user-agent": f"socagents/{__version__}",
                "accept": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()
        await self._tokens.aclose()

    @property
    def notices(self) -> list[str]:
        return self._tokens.notices

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = await self._tokens.token()
        try:
            response = await self._client.request(
                method,
                f"{self._base}{path}",
                params=params,
                json=json,
                headers={"authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise XError(f"X is unreachable ({type(exc).__name__}).", code="x_unavailable") from exc
        if response.status_code == 401:
            raise XAuthError("X rejected the access token. Run `socagents x login` again.")
        if response.status_code == 403:
            raise XAccessError(f"{_forbidden_hint(method)} X said: {_detail(response)}")
        if response.status_code == 429:
            reset = response.headers.get("x-rate-limit-reset")
            raise XRateLimited(
                "X rate limit reached.", reset_at=int(reset) if reset and reset.isdigit() else None
            )
        if response.status_code >= 400:
            raise XError(f"X returned HTTP {response.status_code}: {_detail(response)}")
        if not response.content:
            return {}
        try:
            data: dict[str, Any] = response.json()
        except ValueError as exc:
            raise XError("X returned a non-JSON response.") from exc
        return data

    async def me(self) -> XUser:
        payload = await self._request("GET", "/users/me", params={"user.fields": USER_FIELDS})
        data = payload.get("data") or {}
        if not data.get("id"):
            raise XError("X returned no account for this token.")
        return XUser.model_validate(data)

    async def mentions(
        self, user_id: str, *, since_id: str | None = None, max_results: int = 25
    ) -> list[Mention]:
        """Mentions of ``user_id``, oldest first so the cursor only ever moves forward."""
        params: dict[str, Any] = {
            "max_results": max(5, min(max_results, 100)),
            "tweet.fields": TWEET_FIELDS,
            "expansions": "author_id",
            "user.fields": USER_FIELDS,
        }
        if since_id:
            params["since_id"] = since_id
        payload = await self._request("GET", f"/users/{user_id}/mentions", params=params)
        tweets = payload.get("data") or []
        users = {str(u["id"]): u for u in (payload.get("includes") or {}).get("users", [])}
        return sorted((_parse_mention(t, users) for t in tweets), key=lambda m: int(m.id))

    async def post(self, text: str, *, in_reply_to: str | None = None) -> str:
        body: dict[str, Any] = {"text": text}
        if in_reply_to:
            body["reply"] = {"in_reply_to_tweet_id": in_reply_to}
        payload = await self._request("POST", "/tweets", json=body)
        tweet_id = (payload.get("data") or {}).get("id")
        if not tweet_id:
            raise XError("X accepted the post but returned no id.")
        return str(tweet_id)


def _forbidden_hint(method: str) -> str:
    """403 means different things for a read and a write, so say the right one."""
    if method.upper() == "GET":
        return (
            "X refused the read (403). The legacy Free tier is write-only: it cannot read "
            "posts or mentions at all. Enable pay-per-use billing in the developer portal."
        )
    return (
        "X refused the post (403). Set the app's user authentication to Read and Write, then "
        "regenerate its tokens: tokens issued before that change keep the old scope."
    )


def _detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200] or "(no body)"
    for key in ("detail", "title", "error_description", "reason"):
        if isinstance(payload, dict) and payload.get(key):
            return str(payload[key])[:200]
    return str(payload)[:200]
