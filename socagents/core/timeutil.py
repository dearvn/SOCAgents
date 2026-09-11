"""UTC timestamps for storage and US Eastern time for display."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def utcnow() -> datetime:
    return datetime.now(UTC)


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
    return f"{seconds // 60}m"
