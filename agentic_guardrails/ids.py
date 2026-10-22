"""Deterministic client_order_id derivation (docs plan 4.4), pure.

The idempotency key. It is derived ONLY from (strategy, contract, intent,
entry_date) so that a crash-and-restart re-derives the SAME id for the same
intended order, and the reconciler re-adopts the in-flight broker order instead
of placing a second one. It deliberately does NOT include quantity, price, or a
timestamp: those can change across a reprice within the same entry, and the id
must remain the stable identity of "this arm's this-day order on this contract".
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import date

from agentic_guardrails.types import OrderIntent

_PREFIX = "sat"  # namespaces our ids in the broker's order log

# Fixed namespace for deriving the broker-facing ref_id from our client_order_id.
# A constant namespace keeps the derivation deterministic across restarts.
_REF_NS = uuid.UUID("5a700b1e-0000-5a70-0000-5a7000000001")


def client_order_id(
    strategy: str, contract_symbol: str, intent: OrderIntent, entry_date: date, long_contract_symbol: str | None = None
) -> str:
    """A stable, collision-resistant id for one arm's order on one contract on
    one day for one intent. Same inputs -> same id, always. A spread folds its
    wing into the key too, so the same short leg wrapped with two different wings
    on one day is two orders, never one re-adopting the other. Single-leg ids
    are unchanged (the wing is omitted from the key when absent)."""
    parts = [strategy.strip().lower(), contract_symbol.strip().upper(), intent.value, entry_date.isoformat()]
    if long_contract_symbol:
        parts.append(long_contract_symbol.strip().upper())
    key = "|".join(parts)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return f"{_PREFIX}_{digest}"


def broker_ref_id(client_order_id: str) -> str:
    """The broker-facing idempotency token derived from our client_order_id.

    The Robinhood MCP place endpoint began rejecting a non-UUID ref_id (observed
    'ref_id must be a UUID (got "sat_..."'), which silently blocked
    every live open AND every assignment-guard protective close. This maps our
    stable sat_* id to a deterministic UUID5, so the broker sees a valid UUID
    while the id stays the same identity across a reprice/restart (same coid ->
    same ref_id, so the reconciler still re-adopts an in-flight order by ref_id,
    and the broker's own ref_id dedup is preserved). Our internal id and DB
    idempotency are unchanged; this is purely the broker wire form."""
    return str(uuid.uuid5(_REF_NS, client_order_id))
