from __future__ import annotations

from typing import Any

import httpx
import pytest

from socagents.core.config import Settings
from socagents.core.credentials import save_api_key
from socagents.core.errors import ConfigError, ProviderError
from socagents.core.userconfig import UserConfig
from socagents.db.store import Store
from socagents.growth import Upsell
from socagents.model_gateway.types import ToolCall
from socagents.providers.community import CommunityProvider
from socagents.providers.fixture import FixtureProvider
from socagents.providers.socswift import SocSwiftProvider
from socagents.session import open_session, resolve_model_spec, resolve_provider_name
from socagents.socswift_client.client import MemberInfo, MembershipError, SocSwiftClient
from socagents.tools.catalog import full_registry
from socagents.tools.gateway import ToolGateway
from socagents.tools.sdk import ToolContext

ME = {
    "user_id": "u1",
    "plan": "market_intelligence",
    "entitlements": ["agents"],
    "scopes": ["read"],
}
AS_OF = "2026-09-10T17:45:00Z"


def api(
    routes: dict[str, httpx.Response], seen: list[httpx.Request] | None = None
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        for path, response in routes.items():
            if request.url.path.endswith(path):
                return response
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def client(
    routes: dict[str, httpx.Response], seen: list[httpx.Request] | None = None
) -> SocSwiftClient:
    return SocSwiftClient(
        "k", base_url="https://api.test/api/agent/v1", transport=api(routes, seen)
    )


# client


async def test_me_sends_bearer_key() -> None:
    seen: list[httpx.Request] = []
    c = client({"/me": httpx.Response(200, json=ME)}, seen)
    info = await c.me()
    await c.aclose()
    assert info.plan == "market_intelligence"
    assert seen[0].headers["authorization"] == "Bearer k"
    assert str(seen[0].url) == "https://api.test/api/agent/v1/me"


async def test_full_access_keys_are_refused() -> None:
    c = client({"/me": httpx.Response(200, json={**ME, "scopes": ["full_access"]})})
    with pytest.raises(MembershipError) as info:
        await c.me()
    await c.aclose()
    assert info.value.code == "full_access_key_rejected"


@pytest.mark.parametrize(
    ("status", "error", "code"),
    [
        (401, MembershipError, "invalid_api_key"),
        (403, MembershipError, "membership_required"),
        (429, ProviderError, "rate_limited"),
        (500, ProviderError, "socswift_unavailable"),
    ],
)
async def test_error_mapping(status: int, error: type[Exception], code: str) -> None:
    c = client({"/me": httpx.Response(status)})
    with pytest.raises(error) as info:
        await c.me()
    await c.aclose()
    assert info.value.code == code  # type: ignore[attr-defined]


def test_base_url_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOCSWIFT_API_URL", "https://staging.test/api/agent/v1/")
    assert SocSwiftClient("k")._base == "https://staging.test/api/agent/v1"


# provider and member tools


def member_provider() -> SocSwiftProvider:
    routes = {
        "/quotes": httpx.Response(
            200,
            json={
                "as_of": AS_OF,
                "delayed": False,
                "quotes": [{"symbol": "SPY", "last": 581.2, "prev_close": 578.77}],
            },
        ),
        "/options/SPY/chain": httpx.Response(
            200,
            json={
                "as_of": AS_OF,
                "delayed": False,
                "underlying_price": 581.2,
                "contracts": [
                    {
                        "expiration": "2026-09-10",
                        "strike": 582,
                        "right": "call",
                        "open_interest": 10,
                        "gamma": 0.1,
                    }
                ],
            },
        ),
        "/bars/SPY": httpx.Response(
            200,
            json={
                "as_of": AS_OF,
                "delayed": True,
                "bars": [
                    {"ts": AS_OF, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10}
                ],
            },
        ),
        "/gex/SPY": httpx.Response(
            200, json={"as_of": AS_OF, "delayed": False, "regime": "positive", "call_wall": 585}
        ),
    }
    return SocSwiftProvider(client(routes), MemberInfo.model_validate(ME), FixtureProvider())


async def test_member_provider_parses_payloads() -> None:
    p = member_provider()
    quotes = await p.quotes(["SPY"])
    chain = await p.option_chain("SPY")
    bars = await p.bars("SPY")
    headlines = await p.headlines("SPY")
    await p.aclose()
    assert quotes.source == "SocSwift" and quotes.delayed_sec == 0
    assert chain.contracts[0].gamma == 0.1
    assert bars.delayed_sec == 900
    assert headlines.source == "fixture (synthetic)"


async def test_member_tool_returns_member_data(store: Store, settings: Settings) -> None:
    p = member_provider()
    run_id = store.create_run(kind="t", mode="member", model="m", input={})
    gateway = ToolGateway(full_registry(), store, settings)
    result = await gateway.call(
        ToolContext(run_id=run_id, provider=p, mode="member", member=True),
        ToolCall(id="c", name="socswift_gex", arguments={"symbol": "SPY"}),
    )
    await p.aclose()
    assert result.ok
    assert result.content["data"]["data"] == {"regime": "positive", "call_wall": 585}


async def test_member_tools_upsell_once_in_community(
    store: Store, settings: Settings, ctx: ToolContext
) -> None:
    gateway = ToolGateway(
        full_registry(), store, settings, upsell=Upsell(enabled=True, source="cli")
    )
    first = await gateway.call(
        ctx, ToolCall(id="a", name="socswift_flow", arguments={"symbol": "SPY"})
    )
    second = await gateway.call(
        ctx, ToolCall(id="b", name="socswift_gex", arguments={"symbol": "SPY"})
    )
    assert first.error_code == second.error_code == "requires_membership"
    assert first.notice is not None and "utm_source=cli" in first.notice
    assert "0DTE" in first.notice
    assert second.notice is None


# session


class FakeClient:
    fail: Exception | None = None

    def __init__(self, key: str, **kwargs: Any) -> None:
        self.key = key

    async def me(self) -> MemberInfo:
        if FakeClient.fail is not None:
            raise FakeClient.fail
        return MemberInfo.model_validate(ME)

    async def aclose(self) -> None:
        return None


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> type[FakeClient]:
    FakeClient.fail = None
    monkeypatch.setattr("socagents.session.SocSwiftClient", FakeClient)
    return FakeClient


async def test_session_uses_fixture_from_environment(settings: Settings) -> None:
    session = await open_session(settings)
    assert isinstance(session.provider, FixtureProvider) and not session.is_member


async def test_session_defaults_to_socswift_with_a_stored_key(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, fake_client: type[FakeClient]
) -> None:
    monkeypatch.delenv("SOCAGENTS_PROVIDER")
    save_api_key("sk_test")
    session = await open_session(settings)
    await session.aclose()
    assert isinstance(session.provider, SocSwiftProvider)
    assert session.is_member


def seed_member_snapshot(settings: Settings) -> None:
    store = Store(settings.db_path)
    try:
        run_id = store.create_run(kind="t", mode="member", model="m", input={})
        store.save_snapshot(
            run_id=run_id,
            tool="socswift_gex",
            args={},
            payload={"regime": "positive"},
            source="SocSwift",
            as_of=AS_OF,
            delayed_sec=0,
            mode="member",
            trust="trusted",
        )
    finally:
        store.close()


def member_snapshot_count(settings: Settings) -> int:
    store = Store(settings.db_path)
    try:
        row = store.connection.execute(
            "SELECT COUNT(*) FROM data_snapshots WHERE mode = 'member'"
        ).fetchone()
        return int(row[0])
    finally:
        store.close()


async def test_lapsed_key_falls_back_to_community_and_purges_member_data(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, fake_client: type[FakeClient]
) -> None:
    monkeypatch.delenv("SOCAGENTS_PROVIDER")
    save_api_key("sk_test")
    seed_member_snapshot(settings)
    fake_client.fail = MembershipError("This needs an active SocSwift membership.")
    session = await open_session(settings)
    await session.aclose()
    assert isinstance(session.provider, CommunityProvider)
    assert "Community mode" in session.notices[0]
    assert member_snapshot_count(settings) == 0


async def test_outage_falls_back_without_purging(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, fake_client: type[FakeClient]
) -> None:
    monkeypatch.delenv("SOCAGENTS_PROVIDER")
    save_api_key("sk_test")
    seed_member_snapshot(settings)
    fake_client.fail = ProviderError("SocSwift is unavailable.", code="socswift_unavailable")
    session = await open_session(settings)
    await session.aclose()
    assert isinstance(session.provider, CommunityProvider)
    assert member_snapshot_count(settings) == 1


async def test_explicit_socswift_does_not_fall_back(
    settings: Settings, fake_client: type[FakeClient]
) -> None:
    save_api_key("sk_test")
    fake_client.fail = MembershipError("lapsed")
    with pytest.raises(MembershipError):
        await open_session(settings, "socswift")


async def test_socswift_without_a_key(settings: Settings) -> None:
    with pytest.raises(ConfigError):
        await open_session(settings, "socswift")


def test_resolve_provider_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOCAGENTS_PROVIDER")
    assert resolve_provider_name(None, UserConfig()) == "community"
    assert resolve_provider_name(None, UserConfig(default_provider="fixture")) == "fixture"
    assert resolve_provider_name("community", UserConfig(default_provider="fixture")) == "community"
    with pytest.raises(ConfigError):
        resolve_provider_name("bloomberg", UserConfig())


def test_resolve_model_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    config = UserConfig(default_model="ollama/from-config")
    assert resolve_model_spec("anthropic/explicit", config, "community") == "anthropic/explicit"
    assert resolve_model_spec(None, config, "community") == "ollama/from-config"
    monkeypatch.setenv("SOCAGENTS_MODEL", "openai/from-env")
    assert resolve_model_spec(None, config, "community") == "openai/from-env"
    monkeypatch.delenv("SOCAGENTS_MODEL")
    assert resolve_model_spec(None, UserConfig(), "fixture") == "fixture/scripted"
    with pytest.raises(ConfigError):
        resolve_model_spec(None, UserConfig(), "community")
