"""Broker-truth audit of booked fills.

The position reconcile proves the ledger holds the same CONTRACTS as the broker; nothing
proves it booked the same DOLLARS until this does. It re-reads each recent filled order and
compares the broker's filled quantity and average price to the ledger's; a mismatch, or an
order the broker cannot show, records a failing `fill_audit` health row. It never edits the
ledger and never halts opens: wrong bookkeeping is not wrong exposure, and an automated
rewrite of money rows is the change that once corrupted a ledger.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

logger = logging.getLogger(__name__)

KIND_FILL_AUDIT = "fill_audit"
LOOKBACK = timedelta(days=14)
PRICE_TOLERANCE = Decimal("0.005")  # at or under half a cent is rounding, not drift


@dataclass(frozen=True)
class FillDrift:
    client_order_id: str
    contract_symbol: str
    reason: str


def audit_candidates(records, now: datetime, lookback: timedelta = LOOKBACK) -> list:
    cutoff = now - lookback
    return [r for r in records if not r.shadow and r.broker_order_id and r.filled_qty > 0 and r.updated_at >= cutoff]


def compare_fill(rec, broker_order) -> str | None:
    if broker_order.filled_qty != rec.filled_qty:
        return f"filled {rec.filled_qty} in the ledger vs {broker_order.filled_qty} at the broker"
    if broker_order.avg_fill_price is None:
        return "broker reports a fill with no executed price"
    if rec.avg_fill_price is None:
        return f"ledger has no fill price (broker executed at {broker_order.avg_fill_price})"
    gap = abs(Decimal(rec.avg_fill_price) - Decimal(broker_order.avg_fill_price))
    if gap > PRICE_TOLERANCE:
        return f"booked at {rec.avg_fill_price} vs executed {broker_order.avg_fill_price} (${gap * Decimal(rec.filled_qty) * Decimal(100):.2f} of P&L)"
    return None


def summarize(n_checked: int, drifts: list[FillDrift], unreadable: list[str]) -> tuple[bool, str]:
    if not drifts and not unreadable:
        return True, f"{n_checked} fills match the broker"
    parts = []
    if drifts:
        parts.append(f"{len(drifts)} of {n_checked} fills disagree: " + "; ".join(f"{d.contract_symbol} {d.reason}" for d in drifts[:5]))
    if unreadable:
        parts.append(f"{len(unreadable)} could not be read back at the broker")
    return False, " | ".join(parts)


async def run_fill_audit(store, broker, ledger, now: datetime, lookback: timedelta = LOOKBACK) -> tuple[bool, str]:
    try:
        records = await ledger.all_orders()
    except Exception as exc:  # noqa: BLE001 - a crash is a failed audit, recorded so it pages
        detail = f"audit could not read the ledger: {type(exc).__name__}"
        await store.record(KIND_FILL_AUDIT, ok=False, at=now, result="fail", detail=detail)
        return False, detail
    cands = audit_candidates(records, now, lookback)
    drifts: list[FillDrift] = []
    unreadable: list[str] = []
    for rec in cands:
        try:
            read = await broker.status(rec.broker_order_id)
        except Exception as exc:  # noqa: BLE001 - one bad read must not hide the rest
            logger.warning("fill-audit: status read failed for %s: %s", rec.client_order_id, exc)
            unreadable.append(rec.client_order_id)
            continue
        reason = compare_fill(rec, read)
        if reason:
            drifts.append(FillDrift(rec.client_order_id, rec.contract_symbol, reason))
    ok, detail = summarize(len(cands), drifts, unreadable)
    await store.record(KIND_FILL_AUDIT, ok=ok, at=now, result="pass" if ok else "fail", detail=detail)
    return ok, detail
