"""Finding an order the venue may have accepted when we did not get the id, pure.

A place can time out after the venue accepted it; one venue returns the id only in a header a
proxy may strip; another returns 200 for a request it later rejects. The retry that follows is
how positions double. The engine already parks such a placement as UNKNOWN and looks it up by
client order id where the venue stores one. Two venues store none, so the lookup falls back to
matching the order's SHAPE (contract, side, quantity, limit) inside a short window around the
attempt; a candidate missing its limit or timestamp is never a shape match. A candidate carrying a DIFFERENT client tag is
never a shape match: the tag is positive evidence of who placed it. Exactly one match adopts
it; zero or several leave it UNKNOWN. Several is the honest
answer when two identical orders went out: a human has to look, and the engine stays halted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

DEFAULT_WINDOW = timedelta(minutes=10)


@dataclass(frozen=True)
class Candidate:
    broker_order_id: str
    contract_symbol: str
    side: str  # "buy" | "sell" as the venue reports it
    quantity: int
    limit_price: Decimal | None
    created_at: datetime | None
    client_tag: str | None = None  # any venue-echoed tag we set, when present


@dataclass(frozen=True)
class Adoption:
    broker_order_id: str | None
    verdict: str  # "adopted" | "none" | "ambiguous"
    matches: tuple[str, ...]


def _within(created_at: datetime | None, attempted_at: datetime, window: timedelta) -> bool:
    """A candidate with no timestamp, or a naive one, can never be matched by shape: a stale resting
    order from a prior session carrying no time would otherwise be adopted as today's placement."""
    if created_at is None or created_at.tzinfo is None or attempted_at.tzinfo is None:
        return False
    return abs(created_at - attempted_at) <= window


def match_unidentified(
    cands: list[Candidate],
    *,
    client_order_id: str,
    contract_symbol: str,
    side: str,
    quantity: int,
    limit_price: Decimal,
    attempted_at: datetime,
    window: timedelta = DEFAULT_WINDOW,
) -> Adoption:
    tagged = [c for c in cands if c.client_tag and c.client_tag == client_order_id]
    if len(tagged) == 1:
        return Adoption(tagged[0].broker_order_id, "adopted", (tagged[0].broker_order_id,))
    if len(tagged) > 1:
        return Adoption(None, "ambiguous", tuple(c.broker_order_id for c in tagged))
    shape = [
        c
        for c in cands
        if c.contract_symbol == contract_symbol
        and (not c.client_tag or c.client_tag == client_order_id)
        and c.side == side
        and c.quantity == quantity
        and c.limit_price is not None
        and c.limit_price == limit_price
        and _within(c.created_at, attempted_at, window)
    ]
    if len(shape) == 1:
        return Adoption(shape[0].broker_order_id, "adopted", (shape[0].broker_order_id,))
    if not shape:
        return Adoption(None, "none", ())
    return Adoption(None, "ambiguous", tuple(c.broker_order_id for c in shape))
