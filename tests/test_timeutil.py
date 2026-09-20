from __future__ import annotations

from datetime import datetime

from socagents.core.timeutil import ET, next_session_open


def _et(*args: int) -> datetime:
    return datetime(*args, tzinfo=ET)


def test_before_todays_open_returns_today() -> None:
    # Tuesday 2026-09-15, 08:00 ET -> today's 09:30 open.
    assert next_session_open(_et(2026, 9, 15, 8, 0)) == _et(2026, 9, 15, 9, 30)


def test_after_todays_open_rolls_to_tomorrow() -> None:
    # Tuesday 2026-09-15, 13:00 ET -> Wednesday's open.
    assert next_session_open(_et(2026, 9, 15, 13, 0)) == _et(2026, 9, 16, 9, 30)


def test_friday_afternoon_skips_the_weekend() -> None:
    # Friday 2026-09-11, 16:00 ET -> Monday 2026-09-14's open, not Saturday.
    assert next_session_open(_et(2026, 9, 11, 16, 0)) == _et(2026, 9, 14, 9, 30)


def test_saturday_and_sunday_both_land_on_monday() -> None:
    assert next_session_open(_et(2026, 9, 12, 10, 0)) == _et(2026, 9, 14, 9, 30)
    assert next_session_open(_et(2026, 9, 13, 23, 0)) == _et(2026, 9, 14, 9, 30)
