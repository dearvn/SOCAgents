from __future__ import annotations

from datetime import date, datetime

from socagents.core.market import expiration_is_live, live_expirations, market_session
from socagents.core.timeutil import ET


def et(*args: int) -> datetime:
    return datetime(*args, tzinfo=ET)


def test_open_in_regular_hours_with_a_trade_today() -> None:
    assert market_session(et(2026, 9, 11, 10, 0), now=et(2026, 9, 11, 10, 15)).is_open


def test_closed_on_the_weekend() -> None:
    session = market_session(et(2026, 9, 11, 15, 59, 59), now=et(2026, 9, 12, 11, 0))
    assert not session.is_open
    assert session.last_session == date(2026, 9, 11)
    assert session.context() == {
        "status": "closed",
        "last_session": "2026-09-11 (Friday)",
        "last_trade": "2026-09-11 15:59 ET",
    }


def test_closed_after_the_bell_and_before_the_open() -> None:
    assert not market_session(et(2026, 9, 11, 15, 59), now=et(2026, 9, 11, 16, 30)).is_open
    assert not market_session(et(2026, 9, 11, 15, 59), now=et(2026, 9, 14, 8, 0)).is_open


def test_a_holiday_reads_as_closed() -> None:
    # A weekday in regular hours, but the last trade is from the previous session.
    assert not market_session(et(2026, 9, 4, 15, 59), now=et(2026, 9, 7, 11, 0)).is_open


FRI, MON = date(2026, 9, 11), date(2026, 9, 14)


def test_same_day_expiration_is_live_during_the_session() -> None:
    assert expiration_is_live(FRI, et(2026, 9, 11, 15, 45))
    assert live_expirations([FRI, MON], et(2026, 9, 11, 10, 0)) == [FRI, MON]


def test_same_day_expiration_is_done_at_the_closing_print() -> None:
    assert not expiration_is_live(FRI, et(2026, 9, 11, 15, 59, 59))
    assert live_expirations([FRI, MON], et(2026, 9, 11, 15, 59, 59)) == [MON]


def test_past_expirations_are_never_live() -> None:
    assert not expiration_is_live(date(2026, 9, 10), et(2026, 9, 11, 10, 0))
