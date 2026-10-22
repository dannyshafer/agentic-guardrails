"""US equity/options session calendar, stdlib only, pure.

Three reported failures this closes: a bot that ran on an exchange holiday against stale
candles and had its orders queued for the next open; an end-of-day routine keyed on server
UTC that fired hours early; a fixed broker offset that ran the session window an hour off every
summer. The fix for all three is one calendar applied everywhere the bot decides what day or
hour it is, with timezone-aware times and no bare `datetime.now()`.

Holidays follow the NYSE schedule (observed dates for weekend holidays) and must be extended by
hand each year; `holidays_known_through` says how far the table goes, and `assert_calendar_covers`
refuses to run a date past it, so a stale table halts rather than trades a holiday.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Full-day closes (observed). Keep sorted; extend each December.
HOLIDAYS: frozenset[date] = frozenset(
    {
        # 2025
        date(2025, 1, 1),
        date(2025, 1, 9),
        date(2025, 1, 20),
        date(2025, 2, 17),
        date(2025, 4, 18),
        date(2025, 5, 26),
        date(2025, 6, 19),
        date(2025, 7, 4),
        date(2025, 9, 1),
        date(2025, 11, 27),
        date(2025, 12, 25),
        # 2026
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 4, 3),
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 7, 3),
        date(2026, 9, 7),
        date(2026, 11, 26),
        date(2026, 12, 25),
        # 2027
        date(2027, 1, 1),
        date(2027, 1, 18),
        date(2027, 2, 15),
        date(2027, 3, 26),
        date(2027, 5, 31),
        date(2027, 6, 18),
        date(2027, 7, 5),
        date(2027, 9, 6),
        date(2027, 11, 25),
        date(2027, 12, 24),
    }
)
# 13:00 ET closes.
EARLY_CLOSES: frozenset[date] = frozenset(
    {
        date(2025, 7, 3),
        date(2025, 11, 28),
        date(2025, 12, 24),
        date(2026, 11, 27),
        date(2026, 12, 24),
        date(2027, 11, 26),
    }
)
HOLIDAYS_KNOWN_THROUGH = date(2027, 12, 31)

OPEN = time(9, 30)
CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
OPTIONS_CLOSE_INDEX = time(16, 15)  # cash-settled index options trade to 16:15 ET


def assert_aware(ts: datetime, what: str = "timestamp") -> datetime:
    """Refuse a naive datetime. Every clock bug in the research started with one."""
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"{what} must be timezone-aware")
    return ts


def to_et(ts: datetime) -> datetime:
    return assert_aware(ts).astimezone(ET)


def assert_calendar_covers(d: date) -> None:
    if d > HOLIDAYS_KNOWN_THROUGH:
        raise ValueError(f"holiday table ends {HOLIDAYS_KNOWN_THROUGH}; extend runtime/calendar.py before trading {d}")


def is_trading_day(d: date) -> bool:
    assert_calendar_covers(d)
    return d.weekday() < 5 and d not in HOLIDAYS


def session_close(d: date) -> time:
    return EARLY_CLOSE if d in EARLY_CLOSES else CLOSE


def market_open(ts: datetime) -> bool:
    """Regular session only. Extended hours are closed on purpose: options do not trade there and
    every pre-market fill story in the research was a limit the bot did not set."""
    et = to_et(ts)
    d = et.date()
    return is_trading_day(d) and OPEN <= et.time() < session_close(d)


def next_session_open(ts: datetime) -> datetime:
    et = to_et(ts)
    d = et.date()
    if is_trading_day(d) and et.time() < OPEN:
        return datetime.combine(d, OPEN, tzinfo=ET)
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return datetime.combine(d, OPEN, tzinfo=ET)


def previous_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def minutes_to_close(ts: datetime) -> float | None:
    """Minutes until this session's close, None when the market is closed."""
    if not market_open(ts):
        return None
    et = to_et(ts)
    close = datetime.combine(et.date(), session_close(et.date()), tzinfo=ET)
    return (close - et).total_seconds() / 60.0


def bar_is_current(bar_ts: datetime, now: datetime, period: timedelta = timedelta(minutes=5), max_age: timedelta | None = None) -> bool:
    """A decision bar must belong to the expected session (today's once it has opened, else the last
    one) AND, while the market is open, be no older than `max_age`: a feed frozen at the open
    still serves today's date. Bars are usually stamped at their period OPEN, so a healthy
    completed bar is up to two periods old by stamp; the default bound is therefore `2 * period`,
    and `period` must be the bar size the caller actually consumes. Outside the session only the
    date is checked (the last bar of the previous session is the right bar to hold). A bar
    stamped after `now` is never current."""
    if max_age is None:
        max_age = 2 * period
    b = to_et(bar_ts)
    n = to_et(now)
    if b > n:
        return False
    expect = n.date() if (is_trading_day(n.date()) and n.time() >= OPEN) else previous_trading_day(n.date())
    if b.date() != expect:
        return False
    if market_open(now) and (n - b) > max_age:
        return False
    return True
