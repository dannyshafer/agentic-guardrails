"""Per-venue request budget and backoff, stdlib only.

Known limits: one venue allows 120 requests a minute overall and a separately configured order
budget whose overflow is REJECTED, not queued, with a retry that prolongs the throttle; another
allows 200 a minute and reports the reset instant in a header; one reports its quota violation
as HTTP 400 plain text rather than 429; one publishes no numbers and sends a bare 429. The
budget here is a token bucket set below each published limit, so the bot never learns the
limit from a reject, plus a backoff that honours `Retry-After` when present and otherwise waits
a full window, since immediate retries were reported to extend the penalty.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

VENUE_BUDGETS_PER_MIN: dict[str, int] = {
    "schwab": 100,  # published 120
    "tradier": 100,  # published 120 (60 sandbox)
    "alpaca": 160,  # published 200
    "tastytrade": 60,  # unpublished; conservative
    "ibkr": 40,  # gateway pacing is per message; stay low
    "robinhood": 60,
}


@dataclass
class TokenBucket:
    per_minute: int
    _tokens: float = field(init=False)
    _last: float | None = field(init=False)

    def __post_init__(self) -> None:
        self._tokens = float(self.per_minute)
        self._last = None

    def take(self, now: float) -> float:
        """Reserve one request and return the seconds to wait before sending it (0.0 = now).
        Monotonic `now`. The token is spent on every call, so ten callers that find the bucket
        empty are each handed a LATER slot rather than the same one; the bucket runs negative
        and refills at the budget rate. Not thread-safe: wrap in a lock if shared across tasks."""
        if self._last is not None:
            self._tokens = min(float(self.per_minute), self._tokens + (now - self._last) * self.per_minute / 60.0)
        self._last = now
        self._tokens -= 1.0
        if self._tokens >= 0.0:
            return 0.0
        return -self._tokens * 60.0 / self.per_minute


def retry_after_seconds(headers: dict, *, default: float = 60.0, cap: float = 300.0) -> float:
    """Honour Retry-After (seconds) or an X-Ratelimit-Reset-After hint AS GIVEN: a venue that asks
    for ten minutes gets ten minutes, since retrying early prolongs the throttle. The cap applies
    only to our own fallback when no header is present."""
    h = {k.lower(): v for k, v in (headers or {}).items()}
    for key in ("retry-after", "x-ratelimit-reset-after"):
        v = h.get(key)
        if v is not None:
            try:
                val = float(v)
            except ValueError:
                continue
            if not math.isfinite(val) or val < 0:  # "nan", "inf" and negatives parse; none is a wait
                continue
            return val
    return min(cap, default)


def backoff_seconds(attempt: int, base: float = 2.0, cap: float = 60.0, jitter: float = 0.25) -> float:
    """Bounded exponential backoff with jitter for TRANSIENT reads. Never for a place: a place
    that timed out is UNKNOWN and is looked up, not retried."""
    raw = min(cap, base * (2 ** max(0, attempt - 1)))
    return raw * (1.0 + random.uniform(-jitter, jitter))


def is_quota_violation_body(status: int, body) -> bool:
    """One venue signals its rate limit as HTTP 400 with a plain-text body."""
    return status == 400 and isinstance(body, str) and "quota violation" in body.lower()
