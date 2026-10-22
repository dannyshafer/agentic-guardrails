"""Executor health record: what the dead-man gate reads.

`selftest.run_probe` + `selftest.evaluate` decide PASS/FAIL, and
`killswitch.deadman_gate` refuses to arm unless the last PASS is RECENT. Between
those two sat nothing: no scheduler ran the probe and no store remembered when
it last passed, so `deadman_gate` would have read `Freshness(None)` forever and
halted the executor on its first cycle. That is the safe direction, but it means
the gate was decorative rather than operational.

This module closes that loop:

  record_selftest / record_reconcile -> options_executor_health (one row per
  check kind, upserted) -> freshness(...) -> killswitch.KillState

Design notes worth keeping:

- ONE ROW PER KIND, upserted, not an append log. The gate only ever asks "when
  did this last pass"; an ever-growing table would need pruning to answer a
  question a single row answers exactly. Failures update `last_result` and
  `last_run_at` but deliberately do NOT advance `last_ok_at`, which is the field
  the gate reads: a failing check must age out the arm, not refresh it.
- FAIL CLOSED on a read error. An unreadable health row returns Freshness(None),
  which the gate treats as stale, which halts. A store that cannot be read is
  exactly when you least want to place orders.
- Pure/IO split kept: `freshness` and `should_run` are pure and unit-tested; the
  SQL store is the thin shell.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from agentic_guardrails import killswitch

logger = logging.getLogger(__name__)

KIND_SELFTEST = "selftest"
KIND_RECONCILE = "reconcile"


@dataclass(frozen=True)
class HealthRecord:
    kind: str
    last_run_at: datetime | None
    last_ok_at: datetime | None
    last_result: str | None
    detail: str | None = None


def freshness(rec: HealthRecord | None) -> killswitch.Freshness:
    """Adapt a stored record to the kill switch's input. None -> never OK -> stale."""
    return killswitch.Freshness(last_ok_at=rec.last_ok_at if rec else None)


def should_run(rec: HealthRecord | None, now: datetime, interval: timedelta) -> bool:
    """Pure: is this check due? Never-run is always due."""
    if rec is None or rec.last_run_at is None:
        return True
    return (now - rec.last_run_at) >= interval


class HealthStore(Protocol):
    async def get(self, kind: str) -> HealthRecord | None: ...
    async def record(self, kind: str, *, ok: bool, at: datetime, result: str, detail: str | None = None) -> None: ...


class InMemoryHealthStore:
    """Test double / shadow-mode store."""

    def __init__(self) -> None:
        self._rows: dict[str, HealthRecord] = {}

    async def get(self, kind: str) -> HealthRecord | None:
        return self._rows.get(kind)

    async def record(self, kind: str, *, ok: bool, at: datetime, result: str, detail: str | None = None) -> None:
        prev = self._rows.get(kind)
        self._rows[kind] = HealthRecord(
            kind=kind,
            last_run_at=at,
            # A failure never advances last_ok_at: the arm must age out, not refresh.
            last_ok_at=at if ok else (prev.last_ok_at if prev else None),
            last_result=result,
            detail=detail,
        )


async def run_selftest_cycle(
    store: HealthStore,
    broker,
    ledger,
    canary_symbol: str,
    *,
    now: datetime | None = None,
    interval: timedelta = timedelta(hours=12),
    force: bool = False,
    acknowledged_adjusted: frozenset[str] | set[str] = frozenset(),
) -> HealthRecord | None:
    """Run the morning self-test if due and record the outcome.

    Returns the stored record, or None when the check was not due. Never raises:
    a probe that blows up is recorded as a FAILURE, which ages the arm out
    through the dead-man gate. That is the whole point of the gate, so an
    exception here must not escape and skip the recording.
    """
    from agentic_guardrails.selftest import evaluate, run_probe

    now = now or datetime.now(timezone.utc)
    existing = await store.get(KIND_SELFTEST)
    if not force and not should_run(existing, now, interval):
        return existing
    try:
        probe = await run_probe(broker, ledger, canary_symbol, now, acknowledged_adjusted=acknowledged_adjusted)
        result = evaluate(probe)
        ok, detail = result.passed, "; ".join(result.reasons) or None
    except Exception as exc:  # noqa: BLE001 — a crashed probe is a FAILED probe
        ok, detail = False, f"probe raised: {exc}"
        logger.exception("executor self-test raised; recording as FAIL")
    await store.record(KIND_SELFTEST, ok=ok, at=now, result="pass" if ok else "fail", detail=detail)
    if not ok:
        logger.error("executor self-test FAILED: %s", detail)
    return await store.get(KIND_SELFTEST)


async def record_reconcile(store: HealthStore, *, findings_blocking: bool, detail: str | None = None, now: datetime | None = None) -> None:
    """Record the outcome of a broker-versus-ledger reconciliation.

    The dead-man gate needs BOTH a fresh self-test and a fresh clean reconcile,
    so without this writer the gate stayed halted even after self-tests passed
    (caught in review of PR #450). Only a CLEAN reconcile counts as OK: a
    blocking finding (the OVER class, where the ledger claims a short the broker
    denies) records a failure and therefore ages the arm out, which is the whole
    point of reconciling before trading.
    """
    now = now or datetime.now(timezone.utc)
    await store.record(
        KIND_RECONCILE,
        ok=not findings_blocking,
        at=now,
        result="fail" if findings_blocking else "pass",
        detail=detail,
    )


async def run_reconcile_cycle(
    store: HealthStore,
    broker,
    ledger,
    *,
    now: datetime | None = None,
    acknowledged_adjusted: frozenset[str] | set[str] = frozenset(),
) -> HealthRecord | None:
    """Reconcile the ledger against the broker and record the result.

    Same failure discipline as the self-test: a crashed reconcile is a FAILED
    reconcile, never an escaped exception, because an exception here would leave
    the last good timestamp in place and keep the executor armed while blind.
    ANY drift, in either direction, is a failure while nothing heals the ledger.
    """
    from agentic_guardrails.ledger import net_positions
    from agentic_guardrails.reconciler import reconcile, unacknowledged_drift

    now = now or datetime.now(timezone.utc)
    try:
        broker_pos: dict[str, int] = {}
        adjusted: set[str] = set()
        for p in await broker.positions():
            broker_pos[p.contract_symbol] = broker_pos.get(p.contract_symbol, 0) + p.quantity
            if getattr(p, "adjusted", False):
                adjusted.add(p.contract_symbol)
        findings = reconcile(net_positions(await ledger.all_orders()), broker_pos, adjusted=adjusted)
        # ANY drift fails, not just the OVER class. `has_blocking_drift` treats
        # UNDER (broker holds MORE short than the ledger knows) as the safe
        # direction because it is safe TO HEAL, and nothing in this codebase
        # heals it yet. An unhealed UNDER means every collateral guard, which
        # computes from net_positions, works off UNDERSTATED exposure and can
        # admit orders past the per-ticker, per-factor and per-portfolio caps,
        # while KIND_RECONCILE stays fresh and passing and the gate stays armed
        # (caught in review of PR #452). Until ledger healing exists, unreconciled
        # is unreconciled. Relax this to blocking-only in the same diff that
        # implements healing, never before.
        # MATCH is agreement; DUST is the deliberate below-the-floor ignore class
        # (failing on it would halt the gate forever on trivial residue, the
        # sub-5-share lesson from the copy book). Everything else, UNDER, OVER and
        # ASSIGNMENT, means the ledger does not describe the account.
        #
        # ADJUSTED (an OCC-adjusted contract after a reverse split) fails like any
        # other drift until a human ACKNOWLEDGES the specific symbols
        # (`acknowledged_adjusted`, the OPT_EXECUTOR_ACK_ADJUSTED setting). The
        # ack is per-symbol, so a NEW adjustment still halts. An acknowledged pair
        # keeps the ledger's original symbol in net_positions, i.e. its collateral
        # stays counted (conservative), and nothing ever trades it automatically.
        # Exact-symbol ack policy, shared with the self-test (reconciler.unacknowledged_drift).
        drift = unacknowledged_drift(findings, acknowledged_adjusted)
        blocking = bool(drift)
        detail = "; ".join(f"{f.drift.value} {f.contract_symbol}" for f in drift) or None
    except Exception as exc:  # noqa: BLE001 — a crashed reconcile is a FAILED reconcile
        blocking, detail = True, f"reconcile raised: {exc}"
        logger.exception("executor reconcile raised; recording as FAIL")
    await record_reconcile(store, findings_blocking=blocking, detail=detail, now=now)
    if blocking:
        logger.error("executor reconcile BLOCKING: %s", detail)
    return await store.get(KIND_RECONCILE)


async def kill_state_freshness(store: HealthStore) -> tuple[killswitch.Freshness, killswitch.Freshness]:
    """(reconcile, selftest) freshness for building a KillState."""
    return freshness(await store.get(KIND_RECONCILE)), freshness(await store.get(KIND_SELFTEST))
