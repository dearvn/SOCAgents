"""Options Flow digest to a Discord channel, in the same lines the X bot posts.

A channel webhook is all it needs: no bot user, no gateway connection. The webhook URL is a
secret (anyone holding it can post as the webhook), so it only comes from the environment.

Discord has no cashtag limit and a 2,000-character message, so every symbol keeps its ``$``
and a message carries more lines than a tweet. The first message of a New York day carries a
dated header and the disclaimer; later cycles that day post just the new lines.

State is separate from the X bot's, so a failed tweet never holds back Discord or the other
way round. Like the X flow, a cycle drafts by default and posting needs ``--post`` and
``AGENT_SOCIAL=1``.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import httpx

from socagents import __version__
from socagents.core.config import Settings
from socagents.core.errors import ConfigError, SocAgentsError
from socagents.core.timeutil import ET, iso, utcnow
from socagents.db.store import Store
from socagents.social.x.compose import DISCLAIMER
from socagents.social.x.flow import FlowPrint, FlowSource, _utc, flow_line

WEBHOOK_ENV = "DISCORD_FLOW_WEBHOOK_URL"
DISCORD_MAX_CHARS = 2000
SUPPRESS_EMBEDS = 1 << 2

_WEBHOOK_RE = re.compile(
    r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/[\w-]+$"
)

DISCORD_SCHEMA = """
CREATE TABLE IF NOT EXISTS discord_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS discord_flow_posts (
    print_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    line TEXT NOT NULL,
    posted_at TEXT NOT NULL
);
"""

FLOW_CURSOR = "flow_cursor"
FLOW_DAY = "flow_day"  # the New York day the last message went out


class DiscordError(SocAgentsError):
    code = "discord_error"


def webhook_url_from_env() -> str:
    url = os.environ.get(WEBHOOK_ENV, "").strip()
    if not url:
        raise ConfigError(
            f"{WEBHOOK_ENV} is not set. In Discord: channel settings → Integrations → "
            "Webhooks → New Webhook → Copy Webhook URL.",
            code="discord_webhook_missing",
        )
    if not _WEBHOOK_RE.match(url):
        raise ConfigError(
            f"{WEBHOOK_ENV} does not look like a Discord webhook URL "
            "(https://discord.com/api/webhooks/<id>/<token>).",
            code="discord_webhook_invalid",
        )
    return url


class DiscordWebhook:
    def __init__(self, url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._url = url
        self._http = httpx.AsyncClient(
            timeout=20.0,
            transport=transport,
            headers={"User-Agent": f"SOCAgents/{__version__}"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def send(self, content: str) -> str:
        """Post one message and return its id. Waits out one rate limit, then gives up."""
        body = {
            "content": content,
            "allowed_mentions": {"parse": []},  # flow text never pings anyone
            "flags": SUPPRESS_EMBEDS,
        }
        for attempt in range(2):
            resp = await self._http.post(self._url, params={"wait": "true"}, json=body)
            if resp.status_code == 429 and attempt == 0:
                retry_after = float((resp.json() or {}).get("retry_after", 1.0))
                await asyncio.sleep(min(retry_after, 30.0))
                continue
            if resp.status_code in (401, 403, 404):
                raise DiscordError(
                    f"Discord refused the webhook ({resp.status_code}). It was probably "
                    f"deleted or regenerated: make a new one and update {WEBHOOK_ENV}.",
                    code="discord_webhook_rejected",
                )
            if resp.status_code >= 400:
                raise DiscordError(f"Discord webhook returned {resp.status_code}: {resp.text}")
            return str(resp.json()["id"])
        raise DiscordError("Discord kept rate limiting the webhook.", code="discord_rate_limited")


def compose_discord(
    prints: list[FlowPrint],
    *,
    today: date,
    max_lines: int = 10,
    header: str | None = None,
    footer: str | None = None,
) -> tuple[str, list[FlowPrint], list[str]]:
    fixed = [header] if header else []
    tail = [footer] if footer else []
    used: list[FlowPrint] = []
    lines: list[str] = []
    for p in sorted(prints, key=lambda x: x.premium, reverse=True):
        if len(used) >= max_lines:
            break
        line = flow_line(p, today=today, tag="$")
        if len("\n".join([*fixed, *lines, line, *tail])) > DISCORD_MAX_CHARS:
            break
        lines.append(line)
        used.append(p)
    if not used:
        return "", [], []
    return "\n".join([*fixed, *lines, *tail]), used, lines


def day_header(day: date) -> str:
    return f"**Options Flow · {day.strftime('%a')} {day.month}/{day.day}**"


class DiscordFlowStore:
    def __init__(self, store: Store) -> None:
        self._db = store.connection
        self._db.executescript(DISCORD_SCHEMA)
        self._db.commit()

    def get_state(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM discord_state WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_state(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO discord_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, iso(utcnow())),
        )
        self._db.commit()

    def cursor(self) -> datetime | None:
        raw = self.get_state(FLOW_CURSOR)
        return datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None

    def posted(self, print_id: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM discord_flow_posts WHERE print_id = ?", (print_id,)
        ).fetchone()
        return row is not None

    def record(
        self,
        message_id: str,
        prints: list[FlowPrint],
        lines: list[str],
        *,
        day: date,
        cursor: datetime,
    ) -> None:
        now = iso(utcnow())
        self._db.executemany(
            "INSERT OR REPLACE INTO discord_flow_posts (print_id, message_id, line, posted_at) "
            "VALUES (?, ?, ?, ?)",
            [(p.id, message_id, line, now) for p, line in zip(prints, lines, strict=True)],
        )
        self._db.commit()
        self.set_state(FLOW_DAY, day.isoformat())
        self.set_state(FLOW_CURSOR, iso(cursor))


@dataclass
class DiscordFlowReport:
    dry_run: bool
    since: datetime
    fetched: int
    text: str = ""
    prints: list[FlowPrint] = field(default_factory=list)
    message_id: str | None = None
    first_of_day: bool = False
    reason: str = ""  # posted | drafted | no_new_flow | nothing_fits


async def run_discord_flow_once(
    *,
    settings: Settings,
    source: FlowSource,
    post: bool = False,
    webhook: DiscordWebhook | None = None,
    min_premium: float = 100_000,
    max_lines: int = 10,
    max_age_minutes: int = 30,
    now: datetime | None = None,
) -> DiscordFlowReport:
    if post and not settings.kill_switches.agent_social:
        raise ConfigError(
            "Posting to Discord is off. Set AGENT_SOCIAL=1 to let this machine post, or drop "
            "--post to draft the message without sending it.",
            code="social_disabled",
        )
    if post and webhook is None:
        webhook_url = webhook_url_from_env()  # fail before touching the source
    now = now or utcnow()
    store = Store(settings.db_path)
    try:
        flow_store = DiscordFlowStore(store)
        # Never reach further back than max_age: after an outage, old flow is stale news.
        floor = now - timedelta(minutes=max_age_minutes)
        cursor = flow_store.cursor()
        since = max(cursor, floor) if cursor else floor

        fetched = await source.fetch(since=since, min_premium=min_premium, limit=50)
        fresh = [p for p in fetched if not flow_store.posted(p.id)]
        report = DiscordFlowReport(dry_run=not post, since=since, fetched=len(fetched))
        if not fresh:
            report.reason = "no_new_flow"
            return report

        today = now.astimezone(ET).date()
        report.first_of_day = flow_store.get_state(FLOW_DAY) != today.isoformat()
        if report.first_of_day:
            text, used, lines = compose_discord(
                fresh,
                today=today,
                max_lines=max_lines,
                header=day_header(today),
                footer=f"-# {DISCLAIMER}",
            )
        else:
            text, used, lines = compose_discord(fresh, today=today, max_lines=max_lines)
        report.text, report.prints = text, used
        if not used:
            report.reason = "nothing_fits"
            return report
        if not post:
            report.reason = "drafted"
            return report

        owned = webhook is None
        if webhook is None:
            webhook = DiscordWebhook(webhook_url)
        try:
            report.message_id = await webhook.send(text)
        finally:
            if owned:
                await webhook.aclose()
        flow_store.record(
            report.message_id,
            used,
            lines,
            day=today,
            cursor=max(_utc(p.created_at) for p in fetched),
        )
        report.reason = "posted"
        return report
    finally:
        store.close()
