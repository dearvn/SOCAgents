from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from test_x_flow import NOW, QQQ, SPXW, TODAY, TSLA, ListSource, live

from socagents.core.config import Settings
from socagents.core.errors import ConfigError
from socagents.social.discord.flow import (
    WEBHOOK_ENV,
    DiscordError,
    DiscordWebhook,
    compose_discord,
    run_discord_flow_once,
    webhook_url_from_env,
)
from socagents.social.x.flow import FlowPrint

HOOK = "https://discord.com/api/webhooks/123/abc-DEF_9"


def poster(sent: list[dict], *, status: int = 200) -> DiscordWebhook:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["wait"] == "true"
        sent.append(json.loads(request.content))
        if status != 200:
            return httpx.Response(status, json={"message": "nope"})
        return httpx.Response(200, json={"id": f"m{len(sent)}"})

    return DiscordWebhook(HOOK, transport=httpx.MockTransport(handler))


def test_every_line_keeps_its_cashtag() -> None:
    text, used, _ = compose_discord([QQQ, TSLA, SPXW], today=TODAY)
    assert len(used) == 3
    assert "$TSLA 380P" in text and "$SPXW 7790C" in text and "$QQQ 750P" in text
    assert text.splitlines()[0].startswith("🔴 $255K PUT ASK")


def test_compose_stays_under_discord_limit() -> None:
    many = [
        FlowPrint(**{**TSLA.__dict__, "id": f"p{i}", "premium": 100_000 + i}) for i in range(60)
    ]
    text, used, _ = compose_discord(many, today=TODAY, max_lines=60, header="h", footer="f")
    assert len(text) <= 2000
    assert 10 < len(used) < 60


async def test_dry_run_drafts_and_writes_no_state(settings: Settings) -> None:
    source = ListSource([TSLA, SPXW])
    first = await run_discord_flow_once(settings=settings, source=source, now=NOW)
    second = await run_discord_flow_once(settings=settings, source=source, now=NOW)
    assert first.reason == second.reason == "drafted"
    assert first.text == second.text
    assert first.text.splitlines()[0] == "**Options Flow · Fri 10/9**"
    assert first.text.endswith("-# Not investment advice.")


async def test_posting_needs_the_kill_switch(settings: Settings) -> None:
    with pytest.raises(ConfigError) as caught:
        await run_discord_flow_once(settings=settings, source=ListSource([TSLA]), post=True)
    assert caught.value.code == "social_disabled"


async def test_header_once_a_day_then_plain_follow_ups(tmp_path: Path) -> None:
    settings = live(tmp_path)
    sent: list[dict] = []
    source = ListSource([TSLA])
    hook = poster(sent)
    try:
        first = await run_discord_flow_once(
            settings=settings, source=source, post=True, webhook=hook, now=NOW
        )
        again = await run_discord_flow_once(
            settings=settings, source=source, post=True, webhook=hook, now=NOW
        )
        source.prints.append(
            FlowPrint(**{**SPXW.__dict__, "created_at": NOW + timedelta(minutes=10)})
        )
        later = await run_discord_flow_once(
            settings=settings,
            source=source,
            post=True,
            webhook=hook,
            now=NOW + timedelta(minutes=15),
        )
        source.prints.append(FlowPrint(**{**QQQ.__dict__, "created_at": NOW + timedelta(days=3)}))
        monday = await run_discord_flow_once(
            settings=settings,
            source=source,
            post=True,
            webhook=hook,
            now=NOW + timedelta(days=3, minutes=5),
        )
    finally:
        await hook.aclose()

    assert first.reason == "posted" and first.message_id == "m1" and first.first_of_day
    assert again.reason == "no_new_flow"
    assert later.reason == "posted" and not later.first_of_day
    assert monday.first_of_day
    assert len(sent) == 3
    assert sent[0]["allowed_mentions"] == {"parse": []}
    assert sent[0]["content"].startswith("**Options Flow · Fri 10/9**\n")
    assert sent[1]["content"].startswith("🔴 $219K CALL BID | $SPXW")
    assert "Not investment advice" not in sent[1]["content"]
    assert sent[2]["content"].startswith("**Options Flow · Mon 10/12**\n")


async def test_a_rejected_webhook_leaves_the_flow_for_the_next_cycle(tmp_path: Path) -> None:
    settings = live(tmp_path)
    broken = poster([], status=404)
    try:
        with pytest.raises(DiscordError) as caught:
            await run_discord_flow_once(
                settings=settings, source=ListSource([TSLA]), post=True, webhook=broken, now=NOW
            )
    finally:
        await broken.aclose()
    assert caught.value.code == "discord_webhook_rejected"

    sent: list[dict] = []
    hook = poster(sent)
    try:
        retry = await run_discord_flow_once(
            settings=settings, source=ListSource([TSLA]), post=True, webhook=hook, now=NOW
        )
    finally:
        await hook.aclose()
    assert retry.reason == "posted" and retry.first_of_day


async def test_rate_limit_is_waited_out_once() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"retry_after": 0.01})
        return httpx.Response(200, json={"id": "m9"})

    hook = DiscordWebhook(HOOK, transport=httpx.MockTransport(handler))
    try:
        assert await hook.send("hi") == "m9"
    finally:
        await hook.aclose()
    assert calls == 2


def test_webhook_url_is_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(WEBHOOK_ENV, raising=False)
    with pytest.raises(ConfigError, match="not set"):
        webhook_url_from_env()
    monkeypatch.setenv(WEBHOOK_ENV, "https://example.com/hook")
    with pytest.raises(ConfigError, match="does not look like"):
        webhook_url_from_env()
    monkeypatch.setenv(WEBHOOK_ENV, HOOK)
    assert webhook_url_from_env() == HOOK
