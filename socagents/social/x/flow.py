"""Options Flow digest: every cycle, post the largest new flow prints as one tweet.

One line per print, in the shape the desk already reads::

    🔴 $255K PUT ASK | $TSLA 380P 1/15/27 @27.45 → Bearish

One thread per trading day: the first post of the day (New York time) is the main post, with
a dated header and the disclaimer. Every later cycle that day replies to that main post, so
the timeline shows one post a day and the rest of the flow sits under it.

Like the reply bot, a cycle is one-shot and drafts by default. Posting needs ``--post`` and
``AGENT_SOCIAL=1``. A dry run writes nothing, so it can be repeated against the same data.

Where the prints come from is a :class:`FlowSource`. :mod:`socagents.social.x.flow_source`
has a read-only Postgres source and a JSON file source for rehearsals.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from socagents.core.config import Settings
from socagents.core.errors import ConfigError
from socagents.core.timeutil import ET, iso, utcnow
from socagents.db.store import Store
from socagents.social.x.client import POST_USD, XClient
from socagents.social.x.compose import DISCLAIMER, X_MAX_WEIGHT, weighted_len
from socagents.social.x.storage import XStore

FLOW_CURSOR = "flow_cursor"
FLOW_THREAD_DATE = "flow_thread_date"
FLOW_THREAD_ROOT = "flow_thread_root"

FLOW_SCHEMA = """
CREATE TABLE IF NOT EXISTS x_flow_posts (
    print_id TEXT PRIMARY KEY,
    tweet_id TEXT NOT NULL,
    line TEXT NOT NULL,
    posted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_x_flow_posts_posted ON x_flow_posts(posted_at DESC);
"""

# Same thresholds as the engine's classifier, used only when a print arrives without a slug.
_LARGE_PREMIUM = 500_000
_MEDIUM_PREMIUM = 100_000


@dataclass(frozen=True)
class FlowPrint:
    id: str
    symbol: str
    expiration: str
    strike: float
    option_type: str  # CALL | PUT
    direction: str  # ASK (bought) | BID (sold)
    premium: float
    price: float
    created_at: datetime
    slug: str | None = None


class FlowSource(Protocol):
    async def fetch(self, *, since: datetime, min_premium: float, limit: int) -> list[FlowPrint]:
        """Prints created after ``since``, largest premium first."""
        ...


# formatting


def classify(option_type: str, direction: str, premium: float) -> str:
    """Port of the engine's ImpactClassifier, for prints stored without a slug."""
    large, medium = premium >= _LARGE_PREMIUM, premium >= _MEDIUM_PREMIUM
    bought = direction.upper() == "ASK"
    if option_type.upper() == "CALL":
        if bought:
            return "Aggressive Bullish" if large else "Bullish" if medium else "Mild Bullish"
        return "Strong Bearish/Call Selling" if large else "Hedge/Weak Bearish"
    if bought:
        return "Aggressive Bearish" if large else "Bearish" if medium else "Mild Bearish"
    return "Strong Bullish/Put Selling" if large else "Hedge/Weak Bullish"


def impact_label(p: FlowPrint) -> str:
    """The engine's slug, shortened: "Bearish Pressure" → "Bearish", "A / B" → "A/B"."""
    if not p.slug:
        return classify(p.option_type, p.direction, p.premium)
    label = re.sub(r"\s*/\s*", "/", p.slug.replace(" Pressure", ""))
    return re.sub(r"\s+", " ", label).strip()


def fmt_premium(premium: float) -> str:
    if premium >= 1_000_000:
        return f"${premium / 1_000_000:.1f}".removesuffix(".0") + "M"
    return f"${round(premium / 1000)}K"


def fmt_strike(strike: float) -> str:
    return f"{strike:.2f}".rstrip("0").rstrip(".")


def fmt_expiration(expiration: str, *, today: date) -> str:
    """``1/15/27`` for another year, ``10/23`` for this one. Unknown shapes pass through."""
    raw = expiration.strip()[:10]
    for pattern in ("%Y-%m-%d", "%Y%m%d", "%y%m%d"):
        try:
            day = datetime.strptime(raw, pattern).date()
        except ValueError:
            continue
        short = f"{day.month}/{day.day}"
        return short if day.year == today.year else f"{short}/{day.year % 100:02d}"
    return expiration


def flow_line(p: FlowPrint, *, today: date) -> str:
    label = impact_label(p)
    dot = "🟢" if "Bullish" in label else "🔴" if "Bearish" in label else "⚪"
    side = p.option_type.upper()
    return (
        f"{dot} {fmt_premium(p.premium)} {side} {p.direction.upper()} | "
        f"${p.symbol.upper()} {fmt_strike(p.strike)}{side[:1]} "
        f"{fmt_expiration(p.expiration, today=today)} @{p.price:.2f} → {label}"
    )


def compose_flow(
    prints: list[FlowPrint],
    *,
    today: date,
    max_lines: int = 4,
    header: str | None = None,
    footer: str | None = DISCLAIMER,
) -> tuple[str, list[FlowPrint]]:
    """Pack the largest prints into one tweet. Returns the text and the prints it carries."""
    fixed = [part for part in (header,) if part]
    tail = [footer] if footer else []
    used: list[FlowPrint] = []
    lines: list[str] = []
    for p in sorted(prints, key=lambda x: x.premium, reverse=True):
        if len(used) >= max_lines:
            break
        candidate = "\n".join([*fixed, *lines, flow_line(p, today=today), *tail])
        if weighted_len(candidate) > X_MAX_WEIGHT:
            continue
        lines.append(flow_line(p, today=today))
        used.append(p)
    if not used:
        return "", []
    return "\n".join([*fixed, *lines, *tail]), used


# state


class FlowStore:
    def __init__(self, store: Store) -> None:
        self.x = XStore(store)
        self._db = store.connection
        self._db.executescript(FLOW_SCHEMA)
        self._db.commit()

    def cursor(self) -> datetime | None:
        raw = self.x.get_state(FLOW_CURSOR)
        return datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None

    def set_cursor(self, at: datetime) -> None:
        self.x.set_state(FLOW_CURSOR, iso(at))

    def thread_root(self, day: date) -> str | None:
        """Today's main post, if one went out. A root from another day is not reused."""
        if self.x.get_state(FLOW_THREAD_DATE) != day.isoformat():
            return None
        return self.x.get_state(FLOW_THREAD_ROOT)

    def set_thread_root(self, day: date, tweet_id: str) -> None:
        self.x.set_state(FLOW_THREAD_ROOT, tweet_id)
        self.x.set_state(FLOW_THREAD_DATE, day.isoformat())

    def posted(self, print_id: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM x_flow_posts WHERE print_id = ?", (print_id,)
        ).fetchone()
        return row is not None

    def record(self, tweet_id: str, prints: list[FlowPrint], lines: list[str]) -> None:
        now = iso(utcnow())
        self._db.executemany(
            "INSERT OR REPLACE INTO x_flow_posts (print_id, tweet_id, line, posted_at) "
            "VALUES (?, ?, ?, ?)",
            [(p.id, tweet_id, line, now) for p, line in zip(prints, lines, strict=True)],
        )
        self._db.commit()

    def posts_within(self, hours: int = 24) -> int:
        row = self._db.execute(
            "SELECT COUNT(DISTINCT tweet_id) AS n FROM x_flow_posts WHERE posted_at >= ?",
            (iso(utcnow() - timedelta(hours=hours)),),
        ).fetchone()
        return int(row["n"])


# cycle


@dataclass
class FlowReport:
    dry_run: bool
    since: datetime
    fetched: int
    text: str = ""
    prints: list[FlowPrint] = field(default_factory=list)
    tweet_id: str | None = None
    in_reply_to: str | None = None  # today's main post, when this one is a reply
    reason: str = ""  # posted | drafted | no_new_flow | nothing_fits

    @property
    def api_cost_usd(self) -> float:
        return POST_USD if self.tweet_id else 0.0


def root_header(day: date) -> str:
    return f"Options Flow · {day.strftime('%a')} {day.month}/{day.day} 🧵"


def _utc(dt: datetime) -> datetime:
    """Postgres ``timestamp`` columns come back naive. The engine writes them in UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


async def run_flow_once(
    *,
    settings: Settings,
    source: FlowSource,
    post: bool = False,
    client: XClient | None = None,
    min_premium: float = 100_000,
    max_lines: int = 4,
    max_age_minutes: int = 30,
    header: str | None = None,
    thread: bool = True,
    now: datetime | None = None,
) -> FlowReport:
    if post and not settings.kill_switches.agent_social:
        raise ConfigError(
            "Posting to X is off. Set AGENT_SOCIAL=1 to let this machine post, or drop "
            "--post to draft the digest without sending it.",
            code="social_disabled",
        )
    now = now or utcnow()
    store = Store(settings.db_path)
    try:
        flow_store = FlowStore(store)
        # Never reach further back than max_age: after an outage, old flow is stale news.
        floor = now - timedelta(minutes=max_age_minutes)
        cursor = flow_store.cursor()
        since = max(cursor, floor) if cursor else floor

        fetched = await source.fetch(since=since, min_premium=min_premium, limit=50)
        fresh = [p for p in fetched if not flow_store.posted(p.id)]
        report = FlowReport(dry_run=not post, since=since, fetched=len(fetched))
        if not fresh:
            report.reason = "no_new_flow"
            return report

        today = now.astimezone(ET).date()
        root = flow_store.thread_root(today) if thread else None
        if root:
            # A reply: the main post already carries the date and the disclaimer.
            text, used = compose_flow(
                fresh, today=today, max_lines=max_lines, header=header, footer=None
            )
        else:
            first = header or (root_header(today) if thread else None)
            text, used = compose_flow(fresh, today=today, max_lines=max_lines, header=first)
        report.text, report.prints, report.in_reply_to = text, used, root
        if not used:
            report.reason = "nothing_fits"
            return report
        if not post:
            report.reason = "drafted"
            return report

        owned = client is None
        if client is None:
            from socagents.social.x.auth import load_authorizer

            client = XClient(load_authorizer())
        try:
            report.tweet_id = await client.post(text, in_reply_to=root)
        finally:
            if owned:
                await client.aclose()
        if thread and not root:
            flow_store.set_thread_root(today, report.tweet_id)
        flow_store.record(report.tweet_id, used, [flow_line(p, today=today) for p in used])
        flow_store.set_cursor(max(_utc(p.created_at) for p in fetched))
        report.reason = "posted"
        return report
    finally:
        store.close()
