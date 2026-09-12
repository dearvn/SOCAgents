"""US equity market session, judged from the clock and the data's last trade time.

Regular hours only (9:30-16:00 ET). An exchange holiday shows up as a last trade from an
earlier day, so it also reads as closed.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel

from socagents.core.timeutil import ET, fmt_et, utcnow

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
FINAL_MINUTE = time(15, 59)

SessionStatus = Literal["open", "closed"]


class MarketSession(BaseModel):
    status: SessionStatus
    last_session: date  # ET date of the session the data describes
    last_trade: datetime

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    def context(self) -> dict[str, str]:
        """The session as role context."""
        return {
            "status": self.status,
            "last_session": f"{self.last_session:%Y-%m-%d (%A)}",
            "last_trade": fmt_et(self.last_trade),
        }


def market_session(last_trade: datetime, now: datetime | None = None) -> MarketSession:
    now_et = (now or utcnow()).astimezone(ET)
    trade_et = last_trade.astimezone(ET)
    in_hours = now_et.weekday() < 5 and REGULAR_OPEN <= now_et.time() < REGULAR_CLOSE
    status: SessionStatus = "open" if in_hours and trade_et.date() == now_et.date() else "closed"
    return MarketSession(status=status, last_session=trade_et.date(), last_trade=last_trade)


def expiration_is_live(expiration: date, as_of: datetime) -> bool:
    """Whether contracts expiring on ``expiration`` still trade after data stamped ``as_of``.

    Judged from the data, not the clock, so replays and fixtures stay stable. Data stamped in
    the final minute of the expiration day is the closing print, and those contracts are done.
    """
    as_of_et = as_of.astimezone(ET)
    if expiration != as_of_et.date():
        return expiration > as_of_et.date()
    return as_of_et.time() < FINAL_MINUTE


def live_expirations(expirations: list[date], as_of: datetime) -> list[date]:
    return [d for d in expirations if expiration_is_live(d, as_of)]
