"""The underlying spot the assignment guard acts on, from two independent feeds.

An unreadable spot (None, NaN, inf) must route to the policy's fail-safe path, never poison a
Decimal comparison. With two readable feeds that disagree, the LOWER price wins: for a short
put a lower spot is the more dangerous reading, so a feed fault can only make the guard act
sooner. One dead feed no longer blinds the guard; both dead is the unchanged fail-safe (None).
"""

from __future__ import annotations

import math
from decimal import Decimal

SPOT_MAX_AGE_SEC = 30 * 60
SPOT_DIVERGENCE = 0.02


def readable_spot(price) -> Decimal | None:
    if price is None:
        return None
    try:
        f = float(price)
    except (TypeError, ValueError):
        return None
    return Decimal(str(price)) if math.isfinite(f) else None


def guard_spot(primary, secondary, secondary_age_sec: float | None = None, max_age_sec: float = SPOT_MAX_AGE_SEC):
    """(spot, source, divergence). `source` is 'primary' or 'secondary'; divergence is the
    fraction the two feeds disagree by when both were readable, else None. An undated or stale
    secondary print is not trusted."""
    p, sec = readable_spot(primary), readable_spot(secondary)
    if sec is not None and (secondary_age_sec is None or secondary_age_sec > max_age_sec):
        sec = None
    if p is not None and sec is not None:
        div = abs(p - sec) / max(p, sec)
        if div > SPOT_DIVERGENCE:
            return min(p, sec), ("primary" if p <= sec else "secondary"), float(div)
        return p, "primary", float(div)
    if p is not None:
        return p, "primary", None
    if sec is not None:
        return sec, "secondary", None
    return None, None, None
