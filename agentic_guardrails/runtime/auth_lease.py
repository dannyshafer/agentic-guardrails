"""Credential lifetime watchdog, pure.

Every venue's auth dies on its own clock and the failure never looks like "your token aged
out": one venue's refresh token hard-expires after seven days and the token endpoint then
answers `invalid_client`, which reads as a bad app secret; another's access token lives fifteen
minutes; a gateway session needs a keep-alive every minute and a login from a phone kills it
mid-day. The expensive version of the failure is not the failed open, it is the protective
CLOSE that cannot be placed because the session died with a short on the book.

A `Lease` records when a credential was issued and how long the venue lets it live. The gate
asks one question before an open: will this credential outlive the position's guard window?
If a human has to log in again before the next session's open, do not add a short that needs
guarding past that point. The lease also yields a warning schedule so the human hears about
the expiry a day out and again hours out, not from a reject at the open.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# Documented hard lifetimes. None = the venue does not expire it on a clock (it still dies on
# a password change, a login elsewhere, or an IP block); the lease then only tracks freshness.
VENUE_HARD_LIFETIMES: dict[str, timedelta | None] = {
    "schwab_refresh": timedelta(days=7),
    "schwab_access": timedelta(minutes=30),
    "tastytrade_access": timedelta(minutes=15),
    "tastytrade_refresh": None,
    "tradier_access": None,
    "alpaca_key": None,
    "ibkr_cp_session": timedelta(minutes=6),  # lapses without a tickle; the tickle resets issued_at
    "robinhood_access": timedelta(hours=24),
}

DEFAULT_WARN_AT: tuple[timedelta, ...] = (timedelta(hours=24), timedelta(hours=6), timedelta(hours=1))


@dataclass(frozen=True)
class Lease:
    name: str
    issued_at: datetime
    hard_lifetime: timedelta | None
    renewable_unattended: bool  # False = a human must act (browser login, 2FA) to renew

    @property
    def expires_at(self) -> datetime | None:
        return None if self.hard_lifetime is None else self.issued_at + self.hard_lifetime

    def remaining(self, now: datetime) -> timedelta | None:
        e = self.expires_at
        return None if e is None else e - now

    def expired(self, now: datetime) -> bool:
        r = self.remaining(now)
        return r is not None and r <= timedelta(0)

    def warning(self, now: datetime, warn_at: tuple[timedelta, ...] = DEFAULT_WARN_AT) -> str | None:
        """The most urgent warning tier crossed, or None. Only credentials a human must renew warn;
        an unattended refresh is the code's job, and a failed one surfaces as an auth error."""
        if self.renewable_unattended:
            return None
        r = self.remaining(now)
        if r is None:
            return None
        if r <= timedelta(0):
            return f"{self.name} EXPIRED {(-r).total_seconds() / 3600:.1f}h ago; a human must renew it"
        for tier in sorted(warn_at):
            if r <= tier:
                return f"{self.name} expires in {r.total_seconds() / 3600:.1f}h; renew it before then"
        return None


def open_blocked_reason(lease: Lease, now: datetime, guard_horizon: timedelta) -> str | None:
    """Refuse an open that the credential cannot guard. `guard_horizon` is how long the bot needs
    to keep acting on the new position (at least until the next session's open, so the assignment
    guard gets one more look). An unattended-renewable lease never blocks."""
    if lease.renewable_unattended:
        return None
    r = lease.remaining(now)
    if r is None:
        return None
    if r <= timedelta(0):
        return f"{lease.name} has expired; nothing can be placed or closed until it is renewed"
    if r < guard_horizon:
        return f"{lease.name} expires in {r.total_seconds() / 3600:.1f}h, before the {guard_horizon.total_seconds() / 3600:.0f}h guard horizon; no new shorts"
    return None
