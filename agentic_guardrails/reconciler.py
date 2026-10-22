"""Ledger-vs-broker reconciliation classifier (docs plan 4.5), pure.

The broker is truth for what we HOLD; the ledger is what we THINK we hold. This
module classifies each per-contract discrepancy so the I/O shell knows the safe
action. Direction is everything (the copy-book drift lesson, generalized):

- UNDER  (broker holds MORE of a position than the ledger, same direction): a
  fill settled that we under-recorded. On-chain/broker balance lags matching and
  never leads, so the broker is a FLOOR on what we hold -> the SAFE direction:
  heal the ledger UP toward the broker. Auto-fixable.
- OVER   (ledger claims a position the broker denies, or the sign disagrees):
  dangerous. Either a phantom we never really had, or a position that closed
  under us. For a spread's LONG wing this is the naked-short case (the wing is
  gone while the short leg may still be on): HALT new entries and alert; never
  auto-close on this (auto-closing ledger-over is what corrupted rows before).
  Long legs (Phase 2 spreads) are POSITIVE quantities and are classified by the
  same magnitude-and-sign rule, so a wing the broker holds and the ledger
  missed heals up like an under-recorded short.
- ASSIGNMENT: a short option vanished AND shares of its underlying appeared. Not
  an anomaly: the wheel event. Route to the covered-call decision.
- DUST: a discrepancy below the min-shares / min-fraction floor. Settle noise,
  ignored.
- MATCH: no meaningful difference.
- ADJUSTED: an OCC-adjusted contract (reverse split / corporate action: a new
  root carrying a digit such as NVDL1, or a non-100 multiplier). The broker
  re-keys the held short under the adjusted symbol, so the ledger's original
  symbol reads as "denied" and the adjusted one as "unknown". Without this class
  that pair surfaced as OVER + UNDER: the executor halted with a misleading
  "phantom position" reason and nothing told a human the real cause. ADJUSTED is
  never auto-healed and never auto-traded; it fails the reconcile (the ledger no
  longer describes the account) until a human acknowledges it.

Two-read confirmation (a drift must persist across two consecutive reconciles
before action) is the I/O shell's job; this module classifies a single snapshot.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from agentic_guardrails.types import adjusted_base_key, is_adjusted_symbol


class DriftClass(str, enum.Enum):
    MATCH = "match"
    UNDER = "under"  # broker > ledger: safe, heal up
    OVER = "over"  # ledger > broker: dangerous, halt + alert
    ASSIGNMENT = "assignment"  # short gone + shares appeared: wheel event
    DUST = "dust"  # below the floor: ignore
    ADJUSTED = "adjusted"  # OCC-adjusted contract: human only, never auto-traded


@dataclass(frozen=True)
class Finding:
    contract_symbol: str
    drift: DriftClass
    ledger_qty: int  # signed (short = negative), as the ledger has it
    broker_qty: int  # signed, as the broker reports
    detail: str


def classify_contract(
    contract_symbol: str,
    ledger_qty: int,
    broker_qty: int,
    *,
    shares_appeared: bool = False,
    min_shares: int = 1,
) -> Finding:
    """Classify one contract's ledger-vs-broker discrepancy. Quantities are
    signed (short puts negative). `shares_appeared` is True when the underlying's
    long shares showed up in the same reconcile (the assignment tell)."""
    diff = abs(broker_qty - ledger_qty)
    if diff == 0:
        return Finding(contract_symbol, DriftClass.MATCH, ledger_qty, broker_qty, "in sync")

    # A short that the ledger held and the broker no longer shows, with shares
    # appearing, is an assignment regardless of magnitude.
    if ledger_qty < 0 and broker_qty == 0 and shares_appeared:
        return Finding(contract_symbol, DriftClass.ASSIGNMENT, ledger_qty, broker_qty, "short gone, shares appeared: assigned")

    if diff < min_shares:
        return Finding(contract_symbol, DriftClass.DUST, ledger_qty, broker_qty, f"diff {diff} < floor {min_shares}")

    # The broker holds MORE of the position than the ledger booked, in the same
    # direction (ledger flat, or both short, or both long): an under-recorded
    # fill, safe to heal up. Sign is what makes a LONG wing (+) and a SHORT put
    # (-) both read correctly here.
    same_direction = ledger_qty == 0 or (ledger_qty < 0) == (broker_qty < 0)
    if same_direction and abs(broker_qty) > abs(ledger_qty):
        return Finding(
            contract_symbol, DriftClass.UNDER, ledger_qty, broker_qty, "broker holds more of this position than the ledger: heal up"
        )

    # Otherwise the ledger claims a position the broker does not confirm (or the
    # sign flipped): dangerous.
    return Finding(contract_symbol, DriftClass.OVER, ledger_qty, broker_qty, "ledger claims a position the broker denies: halt + alert")


def reconcile(
    ledger: dict[str, int],
    broker: dict[str, int],
    *,
    shares_by_underlying: dict[str, bool] | None = None,
    underlying_of=lambda sym: sym,
    min_shares: int = 1,
    adjusted: set[str] | frozenset[str] = frozenset(),
) -> list[Finding]:
    """Classify every contract present in either book. `shares_by_underlying`
    maps an underlying symbol -> whether long shares of it appeared this
    reconcile; `underlying_of` extracts the underlying from an OCC symbol.
    Returns one Finding per contract (MATCH included, so the caller can count a
    fully-clean reconcile)."""
    shares_by_underlying = shares_by_underlying or {}
    # Adjusted broker contracts: a digit root, or anything the caller flags (a
    # non-100 multiplier read from the instrument). Their base keys pair them
    # with the ledger's pre-adjustment symbol.
    adj_broker = {s for s in broker if s in adjusted or is_adjusted_symbol(s)}
    adj_keys = {adjusted_base_key(s) for s in adj_broker} - {None}
    out: list[Finding] = []
    for sym in sorted(set(ledger) | set(broker)):
        lq, bq = ledger.get(sym, 0), broker.get(sym, 0)
        if sym in adj_broker or (sym in ledger and lq != 0 and bq == 0 and adjusted_base_key(sym) in adj_keys):
            out.append(
                Finding(
                    sym,
                    DriftClass.ADJUSTED,
                    lq,
                    bq,
                    "OCC-adjusted contract (reverse split / corporate action): human must manage; never auto-traded",
                )
            )
            continue
        appeared = shares_by_underlying.get(underlying_of(sym), False)
        out.append(classify_contract(sym, ledger.get(sym, 0), broker.get(sym, 0), shares_appeared=appeared, min_shares=min_shares))
    return out


def unacknowledged_drift(findings: list[Finding], acknowledged: frozenset[str] | set[str] = frozenset()) -> list[Finding]:
    """Every finding that still means "the ledger does not describe the
    account", after the human acknowledgement policy for OCC-adjusted contracts.

    The policy is EXACT-symbol, never a family match:
    - an adjusted BROKER contract is waived only when its own symbol is in
      `acknowledged` (acking NVDL1 $34P does not waive NVDL1 $30P);
    - a legacy LEDGER symbol paired with adjusted contracts is waived only when
      the acknowledged adjusted broker quantity on its (base root, expiry, type)
      key exactly covers the legacy ledger quantity on that key. Any unacked
      adjusted contract on the key, or any excess / shortfall, keeps every
      finding on that key blocking.
    MATCH and DUST are benign; everything else (OVER, UNDER, ASSIGNMENT,
    unwaived ADJUSTED) is returned."""
    acked = {a.strip().upper() for a in acknowledged}
    adj_broker, legacy = _split_adjusted(findings)

    waived: set[str] = {f.contract_symbol for f in adj_broker if f.contract_symbol in acked}
    waived |= _waived_legacy(adj_broker, legacy, acked)

    benign = {DriftClass.MATCH, DriftClass.DUST}
    return [f for f in findings if f.drift not in benign and f.contract_symbol not in waived]


def _split_adjusted(findings: list[Finding]) -> tuple[list[Finding], list[Finding]]:
    """(adjusted broker-side findings, legacy ledger-only findings)."""
    adj_broker = [f for f in findings if f.drift == DriftClass.ADJUSTED and is_adjusted_symbol(f.contract_symbol)]
    legacy = [f for f in findings if f.drift == DriftClass.ADJUSTED and not is_adjusted_symbol(f.contract_symbol)]
    # A multiplier-only adjustment keeps a standard-looking symbol; it is a
    # broker-side finding when the broker holds it, and must itself be acked.
    adj_broker += [f for f in legacy if f.broker_qty != 0]
    legacy = [f for f in legacy if f.broker_qty == 0]
    return adj_broker, legacy


def _waived_legacy(adj_broker: list[Finding], legacy: list[Finding], acked: set[str]) -> set[str]:
    """Legacy ledger symbols whose (base root, expiry, type) key is exactly covered
    by acknowledged adjusted broker quantity, with nothing unacknowledged on it."""
    by_key: dict = {}
    for f in adj_broker:
        k = adjusted_base_key(f.contract_symbol)
        tot = by_key.setdefault(k, {"acked": 0, "unacked": 0, "legacy": 0})
        tot["acked" if f.contract_symbol in acked else "unacked"] += f.broker_qty
    for f in legacy:
        by_key.setdefault(adjusted_base_key(f.contract_symbol), {"acked": 0, "unacked": 0, "legacy": 0})["legacy"] += f.ledger_qty
    waived: set[str] = set()
    for f in legacy:
        tot = by_key[adjusted_base_key(f.contract_symbol)]
        if tot["unacked"] == 0 and tot["acked"] != 0 and tot["acked"] == tot["legacy"]:
            waived.add(f.contract_symbol)
    return waived


def has_blocking_drift(findings: list[Finding]) -> bool:
    """True if any finding is OVER or ADJUSTED, the classes that must HALT new
    entries (an adjusted contract means the ledger's collateral and identity no
    longer describe the account)."""
    return any(f.drift in (DriftClass.OVER, DriftClass.ADJUSTED) for f in findings)
