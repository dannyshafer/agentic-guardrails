"""Parsing helpers shared by the REST adapters: every one FAILS CLOSED (a missing or mistyped
field raises `BrokerMalformed`) so a shape drift at a venue is loud and blocking, never a
silently wrong trade. An unknown order state maps to UNKNOWN, never to a guessed terminal."""

from __future__ import annotations

import re

from decimal import Decimal, InvalidOperation

from agentic_guardrails.broker import BrokerMalformed
from agentic_guardrails.types import OrderState

_MISSING = object()


def get(d, *keys, default=_MISSING):
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] is not None:
            return d[k]
    if default is _MISSING:
        raise BrokerMalformed(f"missing any of {keys} in {list(d)[:8] if isinstance(d, dict) else type(d).__name__}")
    return default


def dec(v, *, field: str) -> Decimal:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise BrokerMalformed(f"non-numeric {field}: {v!r}") from exc
    if not d.is_finite():
        raise BrokerMalformed(f"non-finite {field}: {v!r}")
    return d


def opt_dec(v, *, field: str) -> Decimal | None:
    return None if v is None or v == "" else dec(v, field=field)


def whole(v, *, field: str) -> int:
    d = dec(v, field=field)
    if d != d.to_integral_value():
        raise BrokerMalformed(f"fractional {field}: {v!r}")
    return int(d)


def map_state(raw, table: dict[str, OrderState], *, filled: int, total: int | None, venue: str) -> OrderState:
    """A venue state string -> OrderState. A working order with a partial fill reads PARTIAL."""
    key = str(raw or "").strip().lower()
    st = table.get(key)
    if st is None:
        if key:
            import logging

            logging.getLogger("options_executor.broker").warning("%s: unmapped order state %r -> UNKNOWN (fail closed)", venue, raw)
        return OrderState.UNKNOWN
    if st == OrderState.PLACED and filled > 0 and (total is None or filled < total):
        return OrderState.PARTIAL
    return st


def one(items: list, what: str) -> dict:
    if len(items) == 1:
        return items[0]
    if not items:
        raise BrokerMalformed(f"{what}: no record")
    raise BrokerMalformed(f"{what}: {len(items)} records, ambiguous")


def nickel_grid(price: Decimal) -> Decimal:
    """The coarse listed-option grid: nickels under $3, dimes at or above. Valid on every listed
    option, so an adapter without a per-contract tick read rounds to this."""
    return Decimal("0.10") if price >= Decimal(3) else Decimal("0.05")


# Cash-settled index products trade on a nickel grid at EVERY price (never dimes), and one venue
# holds a penny-priced order on them until the close and then rejects it. Everything else falls
# back to the coarse listed grid above. The venue's own `min_tick` read is authoritative when it
# exists; this is the offline fallback.
INDEX_ROOTS: frozenset[str] = frozenset({"SPX", "SPXW", "XSP", "RUT", "RUTW", "NDX", "NDXP", "VIX", "VIXW", "DJX", "OEX", "XEO"})


def occ_root(contract_symbol: str) -> str:
    """The root of an OCC symbol with or without the `O:` prefix; a bare root passes through."""
    s = contract_symbol[2:] if contract_symbol.startswith("O:") else contract_symbol
    m = re.match(r"^([A-Z]+)\d*\d{6}[CP]\d{8}$", s.upper())
    return m.group(1) if m else s.upper()


def tick_for(contract_symbol_or_root: str, price: Decimal) -> Decimal:
    """Wired into every REST adapter's `min_tick`; takes a full OCC symbol or a bare root."""
    if occ_root(contract_symbol_or_root) in INDEX_ROOTS:
        return Decimal("0.05")
    return nickel_grid(price)


def sub_display_min_reason(limit: Decimal, intent_is_buy: bool) -> str | None:
    """A resting bid under the displayed minimum ($0.05 on most books) is accepted by some venues
    and shown to nobody; it fills only when someone market-sells into it. Not a rest, a hope."""
    if intent_is_buy and Decimal("0") < limit < Decimal("0.05"):
        return f"bid {limit} is below the displayed minimum; the order will not be visible to the market"
    return None
