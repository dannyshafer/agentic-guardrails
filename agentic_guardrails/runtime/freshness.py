"""Quote and feed freshness gates, pure.

Two reported failures: a venue's options quote feed froze for minutes while equities kept
ticking, so limit orders sat against a stale book while last sale ran past them; and a websocket
that reported "connected" for half an hour with no ticks, so a signal filled 33 minutes stale.
"Connected" and "returned data" prove nothing; the two things that do are the age of the quote's
own timestamp and whether the content still changes.

`quote_lag_reason` is the per-order gate (the quote's exchange timestamp against the wall clock).
`FrozenFeedDetector` is the per-symbol content monitor: it remembers the last distinct quote and
when it changed, and flags a symbol whose bid/ask has not moved for longer than the window while
the market is open. A frozen feed cannot be told from a quiet contract by one symbol alone, so
callers should also watch `any_moved`: when nothing in the whole set has moved, it is the feed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from agentic_guardrails.runtime.calendar import market_open

DEFAULT_MAX_QUOTE_AGE = timedelta(seconds=20)
DEFAULT_FROZEN_WINDOW = timedelta(minutes=5)


def quote_lag_reason(as_of: datetime | None, now: datetime, max_age: timedelta = DEFAULT_MAX_QUOTE_AGE) -> str | None:
    """None when the quote is fresh enough to price an order; otherwise why not. A missing or
    future-dated timestamp is stale, never fresh (a feed that stamps nothing is not measured)."""
    if as_of is None:
        return "quote carries no timestamp"
    if as_of.tzinfo is None or now.tzinfo is None:
        return "quote or clock is timezone-naive"
    age = now - as_of
    if age < timedelta(0):
        return f"quote is {(-age).total_seconds():.0f}s in the future (clock skew)"
    if age > max_age:
        return f"quote is {age.total_seconds():.0f}s old (max {max_age.total_seconds():.0f}s)"
    return None


@dataclass
class _Last:
    bid: Decimal
    ask: Decimal
    changed_at: datetime
    seen_at: datetime


@dataclass
class FrozenFeedDetector:
    window: timedelta = DEFAULT_FROZEN_WINDOW
    _last: dict[str, _Last] = field(default_factory=dict)

    def observe(self, symbol: str, bid: Decimal, ask: Decimal, now: datetime) -> None:
        prev = self._last.get(symbol)
        if prev is None or prev.bid != bid or prev.ask != ask:
            self._last[symbol] = _Last(bid, ask, now, now)
        else:
            prev.seen_at = now

    def frozen(self, symbol: str, now: datetime) -> bool:
        """Only while the market is open (nothing moves overnight), and only for a symbol the caller
        is still observing (one not seen within the window is unknown, not frozen; an expired
        contract left in the table cannot keep the verdict red)."""
        prev = self._last.get(symbol)
        if prev is None or not market_open(now):
            return False
        if (now - prev.seen_at) > self.window:
            return False
        return (now - prev.changed_at) > self.window

    def forget(self, symbol: str) -> None:
        self._last.pop(symbol, None)

    def frozen_symbols(self, now: datetime) -> list[str]:
        return sorted(s for s in self._last if self.frozen(s, now))

    def _live(self, now: datetime) -> list[str]:
        return [s for s, p in self._last.items() if (now - p.seen_at) <= self.window]

    def any_moved(self, now: datetime) -> bool:
        """False when EVERY recently-observed symbol is frozen: that is the feed, not the market."""
        live = self._live(now)
        return any(not self.frozen(s, now) for s in live) if live else True

    def feed_frozen_reason(self, now: datetime, min_symbols: int = 3) -> str | None:
        """Whole-feed verdict: needs a few symbols so one illiquid contract cannot halt the book."""
        live = self._live(now)
        if len(live) < min_symbols or not market_open(now):
            return None
        if self.any_moved(now):
            return None
        return f"no quote moved across {len(live)} symbols for over {self.window.total_seconds() / 60:.0f} min"
