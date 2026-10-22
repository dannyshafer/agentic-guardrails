"""Expiry-day and assignment-window rules, pure.

What the research says happens on expiry day, in time order: venues refuse opening orders on
same-day contracts from mid-afternoon (one at 15:30 ET, one 30 minutes before the close); a
venue's risk desk closes in-the-money legs on its own from about 14:00 ET when post-expiry
margin reads red, with no stated cutoff; after the 16:00 close the underlying keeps moving and
holders may exercise out-of-the-money contracts until a venue-specific cutoff around 17:30 ET,
so a contract that "expired worthless" at the close can still be assigned, and a bot that already
booked the expiry sells shares it will not have. The rules here are the bot's own cushion ahead
of each of those venue actions.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from agentic_guardrails.runtime.calendar import ET, to_et

NO_OPEN_AFTER = time(15, 0)  # on the contract's expiry day: earlier than every venue's own cutoff
PIN_CLOSE_AFTER = time(13, 0)  # close near-the-money shorts before the venue's risk desk does
PIN_BAND = Decimal("0.02")  # within 2% of strike counts as pinned
ASSIGNMENT_WINDOW_ENDS = time(17, 45)  # after this the expiry is final; before it, do not touch the underlying


def no_open_reason(expiration: date, now: datetime) -> str | None:
    et = to_et(now)
    if expiration < et.date():
        return "contract has expired"
    if expiration == et.date() and et.time() >= NO_OPEN_AFTER:
        return f"no opening orders on an expiring contract after {NO_OPEN_AFTER.strftime('%H:%M')} ET"
    return None


def pin_risk_reason(expiration: date, strike: Decimal, kind: str, spot: Decimal, now: datetime, band: Decimal = PIN_BAND) -> str | None:
    """A short expiring TODAY with spot inside the band of the strike after PIN_CLOSE_AFTER: close it
    now rather than let the venue decide at a price of its choosing. Out of band deep ITM is the
    assignment guard's case; out of band OTM is left to expire."""
    et = to_et(now)
    if expiration != et.date() or et.time() < PIN_CLOSE_AFTER:
        return None
    if strike <= 0:
        return None
    dist = abs(spot - strike) / strike
    if dist <= band:
        return f"{kind} expiring today pinned: spot {spot} within {band * 100:.0f}% of strike {strike} after {PIN_CLOSE_AFTER.strftime('%H:%M')} ET"
    return None


def assignment_window_open(expiration: date, now: datetime) -> bool:
    """True from the close on expiry day until the exercise cutoff: the position is not yet known.
    A bot must not book "expired worthless", trade the underlying, or free the collateral here."""
    et = to_et(now)
    if expiration != et.date():
        return False
    return time(16, 0) <= et.time() < ASSIGNMENT_WINDOW_ENDS


def expiry_settled_at(expiration: date) -> datetime:
    """The earliest instant the expiry outcome can be trusted from the broker's positions read."""
    return datetime.combine(expiration, ASSIGNMENT_WINDOW_ENDS, tzinfo=ET)


def days_to_expiry(expiration: date, now: datetime) -> int:
    return (expiration - to_et(now).date()).days


def ex_dividend_assignment_reason(
    expiration: date, ex_date: date | None, now: datetime, lookahead: timedelta = timedelta(days=2)
) -> str | None:
    """Short CALLS get exercised early the session before an ex-dividend date when the dividend
    exceeds the remaining extrinsic. The guard does not know the extrinsic; it flags the window so
    the caller can read the quote and decide. Puts are unaffected (callers pass kind)."""
    if ex_date is None:
        return None
    today = to_et(now).date()
    if today <= ex_date <= min(expiration, today + lookahead):
        return f"ex-dividend {ex_date} falls before expiry {expiration}: early assignment risk on short calls"
    return None
