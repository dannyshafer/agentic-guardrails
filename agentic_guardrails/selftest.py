"""Morning self-test (docs/ROBINHOOD_MCP_EXECUTION_PLAN.md 5.4): the dead-man gate's input.

`run_probe` gathers the facts (account/positions/quote reads, ledger reconcile) from a broker and a
ledger; `evaluate` is pure and decides PASS/FAIL against fixed sanity rules. The kill switch arms
only if the last PASS is fresh (`killswitch.deadman_gate`), so a broker that silently changes shape,
loses auth, or drifts from the ledger halts the executor before it can place anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from agentic_guardrails.broker import BrokerAdapter, BrokerError
from agentic_guardrails.ledger import LedgerStore, net_positions
from agentic_guardrails.reconciler import DriftClass, reconcile, unacknowledged_drift


@dataclass(frozen=True)
class Probe:
    account_ok: bool
    free_cash: Decimal | None
    positions_ok: bool
    quote_ok: bool
    quote_bid: Decimal | None
    quote_ask: Decimal | None
    quote_age_s: float | None
    reconcile_over: int  # count of OVER findings (ledger claims short the broker denies)
    reconcile_under: int
    errors: tuple[str, ...] = field(default_factory=tuple)
    # Unacknowledged OCC-adjusted contracts (reverse split). Same policy as the
    # reconcile cycle: blocking until a human acks the exact broker symbol.
    reconcile_adjusted: int = 0


@dataclass(frozen=True)
class SelfTestResult:
    passed: bool
    reasons: tuple[str, ...]


def evaluate(p: Probe, *, max_quote_age_s: float = 900.0) -> SelfTestResult:
    reasons = []
    if not p.account_ok or p.free_cash is None or p.free_cash < 0:
        reasons.append("account read failed or nonsensical")
    if not p.positions_ok:
        reasons.append("positions read failed")
    if not p.quote_ok or p.quote_bid is None or p.quote_ask is None:
        reasons.append("canary quote read failed")
    else:
        if p.quote_bid < 0 or p.quote_ask <= 0 or p.quote_bid > p.quote_ask:
            reasons.append(f"canary quote malformed: bid {p.quote_bid} ask {p.quote_ask}")
        if p.quote_age_s is not None and p.quote_age_s > max_quote_age_s:
            reasons.append(f"canary quote stale: {p.quote_age_s:.0f}s")
    if p.reconcile_adjusted > 0:
        reasons.append(f"{p.reconcile_adjusted} unacknowledged OCC-adjusted contract finding(s): human must manage + ack")
    if p.reconcile_over > 0:
        reasons.append(f"{p.reconcile_over} OVER reconcile finding(s): ledger claims short the broker denies")
    reasons.extend(p.errors)
    return SelfTestResult(passed=not reasons, reasons=tuple(reasons))


async def run_probe(
    broker: BrokerAdapter,
    ledger: LedgerStore,
    canary_symbol: str,
    now: datetime,
    *,
    acknowledged_adjusted: frozenset[str] | set[str] = frozenset(),
) -> Probe:
    errors: list[str] = []
    account_ok = positions_ok = quote_ok = False
    free_cash = bid = ask = None
    age = None
    broker_pos: dict[str, int] = {}
    adjusted: set[str] = set()
    try:
        acct = await broker.account()
        free_cash, account_ok = acct.free_cash, True
    except BrokerError as exc:
        errors.append(f"account: {exc}")
    try:
        for p in await broker.positions():
            broker_pos[p.contract_symbol] = broker_pos.get(p.contract_symbol, 0) + p.quantity
            if getattr(p, "adjusted", False):
                adjusted.add(p.contract_symbol)
        positions_ok = True
    except BrokerError as exc:
        errors.append(f"positions: {exc}")
    try:
        q = await broker.quote(canary_symbol)
        bid, ask, quote_ok = q.bid, q.ask, True
        age = (now - q.as_of).total_seconds()
    except BrokerError as exc:
        errors.append(f"quote: {exc}")
    over = under = adj = 0
    if positions_ok:
        findings = reconcile(net_positions(await ledger.all_orders()), broker_pos, adjusted=adjusted)
        live = unacknowledged_drift(findings, acknowledged_adjusted)
        over = sum(f.drift == DriftClass.OVER for f in live)
        under = sum(f.drift == DriftClass.UNDER for f in live)
        adj = sum(f.drift == DriftClass.ADJUSTED for f in live)
    return Probe(account_ok, free_cash, positions_ok, quote_ok, bid, ask, age, over, under, tuple(errors), reconcile_adjusted=adj)
