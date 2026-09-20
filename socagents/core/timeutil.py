"""UTC timestamps for storage and US Eastern time for display."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def utcnow() -> datetime:
    return datetime.now(UTC)


def next_session_open(now: datetime) -> datetime:
    """Next weekday 09:30 ET strictly after `now`.

    Weekend-only guard, no holiday calendar: this repo has no trading-calendar
    data, and the bug this exists for (C10) is specifically "a weekend run's
    fixed-hours window never reaches Monday's open" — not general session
    awareness. A holiday would still slip through; that is a known gap, not
    silently claimed as handled.
    """
    et_now = now.astimezone(ET)
    candidate = et_now.replace(hour=9, minute=30, second=0, microsecond=0)
    if candidate <= et_now:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:  # Saturday=5, Sunday=6
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def fmt_et(dt: datetime) -> str:
    return dt.astimezone(ET).strftime("%Y-%m-%d %H:%M ET")


def fmt_delay(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds <= 0:
        return "real-time"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m"
    return f"{seconds // 86400}d {seconds % 86400 // 3600}h"
