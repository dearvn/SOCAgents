from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from socagents.cli.main import app
from socagents.core.config import KillSwitches, Settings
from socagents.core.errors import ConfigError
from socagents.social.x.auth import TokenProvider, XCredentials
from socagents.social.x.client import XClient
from socagents.social.x.compose import weighted_len
from socagents.social.x.flow import (
    FlowPrint,
    classify,
    compose_flow,
    flow_line,
    fmt_expiration,
    fmt_premium,
    impact_label,
    run_flow_once,
)
from socagents.social.x.flow_source import JsonFileSource, PostgresFlowSource

runner = CliRunner()

NOW = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)
TODAY = date(2026, 10, 9)

TSLA = FlowPrint(
    id="a1",
    symbol="TSLA",
    expiration="2027-01-15",
    strike=380.0,
    option_type="PUT",
    direction="ASK",
    premium=255_000,
    price=27.45,
    created_at=NOW - timedelta(minutes=5),
    slug="Bearish Pressure",
)
SPXW = FlowPrint(
    id="a2",
    symbol="SPXW",
    expiration="2026-10-23",
    strike=7790.0,
    option_type="CALL",
    direction="BID",
    premium=219_000,
    price=78.0,
    created_at=NOW - timedelta(minutes=4),
    slug="Hedge / Weak Bearish",
)
QQQ = FlowPrint(
    id="a3",
    symbol="QQQ",
    expiration="2026-10-16",
    strike=750.0,
    option_type="PUT",
    direction="BID",
    premium=216_000,
    price=5.39,
    created_at=NOW - timedelta(minutes=3),
    slug="Hedge / Weak Bullish",
)


class ListSource:
    def __init__(self, prints: list[FlowPrint]) -> None:
        self.prints = prints
        self.calls: list[datetime] = []

    async def fetch(self, *, since: datetime, min_premium: float, limit: int) -> list[FlowPrint]:
        self.calls.append(since)
        keep = [p for p in self.prints if p.created_at > since and p.premium >= min_premium]
        return sorted(keep, key=lambda p: p.premium, reverse=True)[:limit]


def x_poster(posts: list[str], replies: list[str | None] | None = None) -> XClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        posts.append(body["text"])
        if replies is not None:
            replies.append((body.get("reply") or {}).get("in_reply_to_tweet_id"))
        return httpx.Response(201, json={"data": {"id": f"t{len(posts)}"}})

    tokens = TokenProvider(XCredentials(access_token="static"))
    return XClient(tokens, base_url="https://api.test/2", transport=httpx.MockTransport(handler))


def live(tmp_path: Path) -> Settings:
    return Settings(home=tmp_path / "home", kill_switches=KillSwitches(agent_social=True))


# format


def test_lines_match_the_desk_format() -> None:
    assert flow_line(TSLA, today=TODAY) == (
        "🔴 $255K PUT ASK | $TSLA 380P 1/15/27 @27.45 → Bearish"
    )
    assert flow_line(SPXW, today=TODAY) == (
        "🔴 $219K CALL BID | $SPXW 7790C 10/23 @78.00 → Hedge/Weak Bearish"
    )
    assert flow_line(QQQ, today=TODAY) == (
        "🟢 $216K PUT BID | $QQQ 750P 10/16 @5.39 → Hedge/Weak Bullish"
    )


def test_premium_strike_and_expiration_shapes() -> None:
    assert fmt_premium(1_250_000) == "$1.2M"
    assert fmt_premium(2_000_000) == "$2M"
    assert fmt_premium(99_600) == "$100K"
    assert fmt_expiration("20261023", today=TODAY) == "10/23"
    assert fmt_expiration("oddly shaped", today=TODAY) == "oddly shaped"
    p = FlowPrint(**{**TSLA.__dict__, "strike": 752.5})
    assert "752.5P" in flow_line(p, today=TODAY)


@pytest.mark.parametrize(
    ("option_type", "direction", "premium", "label"),
    [
        ("CALL", "ASK", 600_000, "Aggressive Bullish"),
        ("CALL", "ASK", 150_000, "Bullish"),
        ("CALL", "BID", 150_000, "Hedge/Weak Bearish"),
        ("PUT", "ASK", 150_000, "Bearish"),
        ("PUT", "BID", 600_000, "Strong Bullish/Put Selling"),
    ],
)
def test_classify_matches_the_engine(
    option_type: str, direction: str, premium: float, label: str
) -> None:
    assert classify(option_type, direction, premium) == label
    p = FlowPrint(
        **{
            **TSLA.__dict__,
            "option_type": option_type,
            "direction": direction,
            "premium": premium,
            "slug": None,
        }
    )
    assert impact_label(p) == label


def test_compose_puts_the_largest_first_and_stays_under_280() -> None:
    many = [
        FlowPrint(**{**TSLA.__dict__, "id": f"p{i}", "premium": 100_000 + i * 1000})
        for i in range(10)
    ]
    text, used, _ = compose_flow(many, today=TODAY, max_lines=8)
    assert weighted_len(text) <= 280
    assert text.endswith("Not investment advice.")
    assert used[0].premium == max(p.premium for p in many)
    assert 1 <= len(used) < 8


def test_compose_honours_the_line_cap_and_header() -> None:
    text, used, _ = compose_flow([QQQ, TSLA, SPXW], today=TODAY, max_lines=2, header="Options Flow")
    assert [p.id for p in used] == ["a1", "a2"]
    assert text.splitlines()[0] == "Options Flow"


# cycle


async def test_dry_run_drafts_and_writes_no_state(settings: Settings) -> None:
    source = ListSource([TSLA, SPXW, QQQ])
    first = await run_flow_once(settings=settings, source=source, now=NOW)
    second = await run_flow_once(settings=settings, source=source, now=NOW)
    assert first.reason == second.reason == "drafted"
    assert first.text == second.text
    assert first.text.splitlines()[0] == "Options Flow · Fri 10/9 🧵"
    assert first.text.splitlines()[1].startswith("🔴 $255K PUT ASK | $TSLA")
    assert first.tweet_id is None


async def test_posting_needs_the_kill_switch(settings: Settings) -> None:
    with pytest.raises(ConfigError) as caught:
        await run_flow_once(settings=settings, source=ListSource([TSLA]), post=True, now=NOW)
    assert caught.value.code == "social_disabled"


async def test_live_cycle_posts_once_then_waits_for_new_flow(tmp_path: Path) -> None:
    settings = live(tmp_path)
    posts: list[str] = []
    source = ListSource([TSLA, SPXW])
    client = x_poster(posts)
    try:
        first = await run_flow_once(
            settings=settings, source=source, post=True, client=client, now=NOW
        )
        second = await run_flow_once(
            settings=settings, source=source, post=True, client=client, now=NOW
        )
        source.prints.append(QQQ)
        third = await run_flow_once(
            settings=settings,
            source=source,
            post=True,
            client=client,
            now=NOW + timedelta(minutes=15),
        )
    finally:
        await client.aclose()

    assert first.reason == "posted" and first.tweet_id == "t1"
    assert first.api_cost_usd == pytest.approx(0.015)
    assert second.reason == "no_new_flow"
    assert third.reason == "posted"
    assert len(posts) == 2
    assert "$QQQ" in posts[1] and "TSLA" not in posts[1]


async def test_old_flow_is_never_posted(tmp_path: Path) -> None:
    stale = FlowPrint(**{**TSLA.__dict__, "created_at": NOW - timedelta(hours=3)})
    report = await run_flow_once(
        settings=live(tmp_path), source=ListSource([stale]), now=NOW, max_age_minutes=30
    )
    assert report.reason == "no_new_flow"


async def test_a_failed_post_leaves_the_flow_for_the_next_cycle(tmp_path: Path) -> None:
    settings = live(tmp_path)

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "busy"})

    tokens = TokenProvider(XCredentials(access_token="static"))
    broken = XClient(tokens, base_url="https://api.test/2", transport=httpx.MockTransport(refuse))
    try:
        with pytest.raises(Exception, match="503"):
            await run_flow_once(
                settings=settings, source=ListSource([TSLA]), post=True, client=broken, now=NOW
            )
    finally:
        await broken.aclose()

    posts: list[str] = []
    client = x_poster(posts)
    try:
        retry = await run_flow_once(
            settings=settings, source=ListSource([TSLA]), post=True, client=client, now=NOW
        )
    finally:
        await client.aclose()
    assert retry.reason == "posted"


# sources


async def test_json_file_source_reads_query_shaped_rows(tmp_path: Path) -> None:
    path = tmp_path / "flow.json"
    path.write_text(
        json.dumps(
            [
                {
                    "id": "r1",
                    "symbol": "TSLA",
                    "expiration": "2027-01-15",
                    "strike": 380,
                    "option_type": "PUT",
                    "direction": "ASK",
                    "premium": 255000,
                    "price": 27.45,
                    "slug": "Bearish Pressure",
                    "created_at": "2026-10-09T14:55:00",
                }
            ]
        )
    )
    prints = await JsonFileSource(path).fetch(
        since=NOW - timedelta(minutes=30), min_premium=100_000, limit=10
    )
    assert [p.id for p in prints] == ["r1"]
    assert prints[0].created_at.tzinfo is not None


def test_postgres_source_needs_a_connection_string() -> None:
    with pytest.raises(ConfigError) as caught:
        PostgresFlowSource()
    assert "FLOW_DATABASE_URL" in str(caught.value)


# cli


def test_x_flow_rehearses_from_a_file(tmp_path: Path) -> None:
    path = tmp_path / "flow.json"
    created = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    row: dict[str, Any] = {
        "id": "r1",
        "symbol": "QQQ",
        "expiration": "2026-10-16",
        "strike": 750,
        "option_type": "PUT",
        "direction": "BID",
        "premium": 216000,
        "price": 5.39,
        "slug": "Hedge / Weak Bullish",
        "created_at": created,
    }
    path.write_text(json.dumps([row]))
    result = runner.invoke(app, ["x", "flow", "--from-file", str(path), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["reason"] == "drafted"
    assert "🟢 $216K PUT BID | $QQQ 750P" in payload["text"]


def test_x_flow_without_a_database_explains_what_to_set() -> None:
    result = runner.invoke(app, ["x", "flow"])
    assert result.exit_code == 2, result.output
    assert "FLOW_DATABASE_URL" in result.output


# thread


async def test_one_main_post_a_day_and_replies_under_it(tmp_path: Path) -> None:
    settings = live(tmp_path)
    posts: list[str] = []
    replies: list[str | None] = []
    source = ListSource([TSLA])
    client = x_poster(posts, replies)
    try:
        main = await run_flow_once(
            settings=settings, source=source, post=True, client=client, now=NOW
        )
        source.prints.append(
            FlowPrint(**{**SPXW.__dict__, "created_at": NOW + timedelta(minutes=10)})
        )
        reply = await run_flow_once(
            settings=settings,
            source=source,
            post=True,
            client=client,
            now=NOW + timedelta(minutes=15),
        )
        source.prints.append(FlowPrint(**{**QQQ.__dict__, "created_at": NOW + timedelta(days=3)}))
        monday = await run_flow_once(
            settings=settings,
            source=source,
            post=True,
            client=client,
            now=NOW + timedelta(days=3, minutes=5),
        )
    finally:
        await client.aclose()

    assert main.in_reply_to is None and replies[0] is None
    assert posts[0].startswith("Options Flow · Fri 10/9 🧵\n")
    assert posts[0].endswith("Not investment advice.")

    assert reply.in_reply_to == main.tweet_id == "t1"
    assert replies[1] == "t1"
    assert posts[1].startswith("🔴 $219K CALL BID") and "Not investment advice." not in posts[1]

    assert monday.in_reply_to is None and replies[2] is None
    assert posts[2].startswith("Options Flow · Mon 10/12 🧵\n")


async def test_dry_run_shows_the_reply_it_would_make(tmp_path: Path) -> None:
    settings = live(tmp_path)
    posts: list[str] = []
    client = x_poster(posts)
    try:
        await run_flow_once(
            settings=settings, source=ListSource([TSLA]), post=True, client=client, now=NOW
        )
    finally:
        await client.aclose()
    later = FlowPrint(**{**SPXW.__dict__, "created_at": NOW + timedelta(minutes=10)})
    draft = await run_flow_once(
        settings=settings, source=ListSource([later]), now=NOW + timedelta(minutes=15)
    )
    assert draft.reason == "drafted"
    assert draft.in_reply_to == "t1"


async def test_no_thread_posts_standalone(tmp_path: Path) -> None:
    settings = live(tmp_path)
    posts: list[str] = []
    replies: list[str | None] = []
    source = ListSource([TSLA])
    client = x_poster(posts, replies)
    try:
        await run_flow_once(
            settings=settings, source=source, post=True, client=client, now=NOW, thread=False
        )
        source.prints.append(
            FlowPrint(**{**SPXW.__dict__, "created_at": NOW + timedelta(minutes=10)})
        )
        await run_flow_once(
            settings=settings,
            source=source,
            post=True,
            client=client,
            now=NOW + timedelta(minutes=15),
            thread=False,
        )
    finally:
        await client.aclose()
    assert replies == [None, None]
    assert posts[0].startswith("🔴 $255K")


def test_only_the_first_line_carries_a_cashtag() -> None:
    # X refuses a post with more than one cashtag (403).
    text, used, lines = compose_flow([TSLA, SPXW, QQQ], today=TODAY, header="Options Flow")
    assert len(used) == 3
    assert text.count("$TSLA") == 1
    assert "| SPXW 7790C" in text and "| QQQ 750P" in text
    assert sum(1 for w in text.split() if w.startswith("$") and w[1:2].isalpha()) == 1
    assert lines[0].startswith("🔴 $255K PUT ASK | $TSLA")


async def test_cashtag_refusal_does_not_blame_the_app_permissions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"detail": "Posts are limited to a maximum of one cashtag ($SYMBOL)."},
        )

    tokens = TokenProvider(XCredentials(access_token="static"))
    client = XClient(tokens, base_url="https://api.test/2", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(Exception) as caught:
            await client.post("x")
    finally:
        await client.aclose()
    assert "one cashtag" in str(caught.value)
    assert "Read and Write" not in str(caught.value)
