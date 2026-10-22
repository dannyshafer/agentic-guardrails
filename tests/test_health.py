"""Executor health record: the dead-man gate's missing input (Sep 6 2026).

The gate refused to arm without a recent PASS, but nothing ran the self-test and
nothing stored the result, so the gate could only ever halt. These pin the
behavior that makes it operational without making it permissive.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentic_guardrails import killswitch
from agentic_guardrails.health import (
    KIND_RECONCILE,
    KIND_SELFTEST,
    InMemoryHealthStore,
    freshness,
    record_reconcile,
    run_selftest_cycle,
    should_run,
)

NOW = datetime(2026, 9, 6, 13, 0, tzinfo=timezone.utc)


def test_missing_record_is_stale_so_the_gate_halts():
    """No record must read as stale, never as fine. This is the fail-closed spine."""
    f = freshness(None)
    assert f.last_ok_at is None
    state = _kill_state(f)
    assert killswitch.deadman_gate(state, NOW, max_reconcile_age=timedelta(hours=1), max_selftest_age=timedelta(hours=20)).halted


def _kill_state(selftest_fresh, reconcile_fresh=None):
    from decimal import Decimal

    return killswitch.KillState(
        executor_enabled=True,
        strategy_pnl_today=Decimal(0),
        strategy_daily_stop=None,
        global_pnl_today=Decimal(0),
        global_daily_stop=None,
        last_reconcile_ok=reconcile_fresh or killswitch.Freshness(NOW),
        last_selftest_ok=selftest_fresh,
    )


@pytest.mark.asyncio
async def test_a_pass_records_last_ok_and_arms_the_gate():
    store = InMemoryHealthStore()
    await store.record(KIND_SELFTEST, ok=True, at=NOW, result="pass")
    rec = await store.get(KIND_SELFTEST)
    assert rec.last_ok_at == NOW
    assert not killswitch.deadman_gate(
        _kill_state(freshness(rec)), NOW, max_reconcile_age=timedelta(hours=1), max_selftest_age=timedelta(hours=20)
    ).halted


@pytest.mark.asyncio
async def test_a_failure_never_advances_last_ok_so_the_arm_ages_out():
    """The load-bearing rule: a failing check must not refresh the gate."""
    store = InMemoryHealthStore()
    await store.record(KIND_SELFTEST, ok=True, at=NOW, result="pass")
    later = NOW + timedelta(hours=30)
    await store.record(KIND_SELFTEST, ok=False, at=later, result="fail", detail="account read failed")
    rec = await store.get(KIND_SELFTEST)
    assert rec.last_run_at == later
    assert rec.last_ok_at == NOW  # NOT advanced
    assert killswitch.deadman_gate(
        _kill_state(freshness(rec)), later, max_reconcile_age=timedelta(hours=1), max_selftest_age=timedelta(hours=20)
    ).halted


def test_should_run_treats_never_run_as_due():
    assert should_run(None, NOW, timedelta(hours=12))


@pytest.mark.asyncio
async def test_should_run_respects_the_interval():
    store = InMemoryHealthStore()
    await store.record(KIND_SELFTEST, ok=True, at=NOW, result="pass")
    rec = await store.get(KIND_SELFTEST)
    assert not should_run(rec, NOW + timedelta(hours=1), timedelta(hours=12))
    assert should_run(rec, NOW + timedelta(hours=13), timedelta(hours=12))


class _ExplodingBroker:
    async def account(self):
        raise RuntimeError("transport is on fire")


@pytest.mark.asyncio
async def test_a_crashed_probe_is_recorded_as_a_failure_not_swallowed():
    """An exception must not escape and skip the recording: that would leave the
    LAST GOOD timestamp in place and silently keep the executor armed."""
    store = InMemoryHealthStore()
    rec = await run_selftest_cycle(store, _ExplodingBroker(), None, "O:TQQQ261016P00080000", now=NOW, force=True)
    assert rec.last_result == "fail"
    assert rec.last_ok_at is None
    assert "probe raised" in rec.detail


@pytest.mark.asyncio
async def test_cycle_skips_when_not_due():
    store = InMemoryHealthStore()
    await store.record(KIND_SELFTEST, ok=True, at=NOW, result="pass")
    out = await run_selftest_cycle(store, _ExplodingBroker(), None, "X", now=NOW + timedelta(minutes=5))
    assert out.last_result == "pass"  # untouched, the exploding broker was never called


@pytest.mark.asyncio
async def test_a_clean_reconcile_records_ok_and_a_blocking_one_does_not():
    """The gate needs BOTH a fresh self-test and a fresh CLEAN reconcile. Nothing
    wrote the reconcile row at all in the first cut, so the gate stayed halted
    even once self-tests passed (found in review of PR #450)."""
    store = InMemoryHealthStore()
    await record_reconcile(store, findings_blocking=False, now=NOW)
    rec = await store.get(KIND_RECONCILE)
    assert rec.last_ok_at == NOW and rec.last_result == "pass"
    later = NOW + timedelta(hours=1)
    await record_reconcile(store, findings_blocking=True, detail="OVER O:NVDL...", now=later)
    rec = await store.get(KIND_RECONCILE)
    assert rec.last_run_at == later
    assert rec.last_ok_at == NOW  # a blocking drift never refreshes the gate


@pytest.mark.asyncio
async def test_both_rows_together_clear_the_dead_man_gate():
    """The end-to-end property the gate is for: only when self-test AND reconcile
    are both recent and passing does the executor arm."""
    store = InMemoryHealthStore()
    await store.record(KIND_SELFTEST, ok=True, at=NOW, result="pass")
    # Self-test alone is not enough: no reconcile row means stale means halt.
    from agentic_guardrails.health import kill_state_freshness

    rec_f, self_f = await kill_state_freshness(store)
    assert rec_f.last_ok_at is None
    state = killswitch.KillState(
        executor_enabled=True,
        strategy_pnl_today=__import__("decimal").Decimal(0),
        strategy_daily_stop=None,
        global_pnl_today=__import__("decimal").Decimal(0),
        global_daily_stop=None,
        last_reconcile_ok=rec_f,
        last_selftest_ok=self_f,
    )
    assert killswitch.deadman_gate(state, NOW, max_reconcile_age=timedelta(minutes=30), max_selftest_age=timedelta(hours=20)).halted
    # Add the clean reconcile and it clears.
    await record_reconcile(store, findings_blocking=False, now=NOW)
    rec_f, self_f = await kill_state_freshness(store)
    state = killswitch.KillState(
        executor_enabled=True,
        strategy_pnl_today=__import__("decimal").Decimal(0),
        strategy_daily_stop=None,
        global_pnl_today=__import__("decimal").Decimal(0),
        global_daily_stop=None,
        last_reconcile_ok=rec_f,
        last_selftest_ok=self_f,
    )
    assert not killswitch.deadman_gate(state, NOW, max_reconcile_age=timedelta(minutes=30), max_selftest_age=timedelta(hours=20)).halted


@pytest.mark.asyncio
async def test_an_unhealed_under_finding_is_not_a_passing_reconcile():
    """The broker holding MORE short than the ledger knows is only the "safe"
    direction because it is safe TO HEAL, and nothing heals it yet. Left passing,
    every collateral guard works off understated exposure and can admit orders
    past its caps while the gate stays armed (found in review of PR #452)."""
    from agentic_guardrails.health import run_reconcile_cycle
    from decimal import Decimal

    from agentic_guardrails.types import Position

    class _Broker:
        async def positions(self):
            # Broker is short 5; the ledger below knows about none of it.
            return [Position(contract_symbol="O:NVDL260918P00030000", quantity=-5, avg_price=Decimal("0.65"))]

    class _Ledger:
        async def all_orders(self):
            return []

    store = InMemoryHealthStore()
    rec = await run_reconcile_cycle(store, _Broker(), _Ledger(), now=NOW)
    assert rec.last_result == "fail"
    assert rec.last_ok_at is None
    assert "under" in (rec.detail or "")


@pytest.mark.asyncio
async def test_a_clean_reconcile_still_passes():
    from agentic_guardrails.health import run_reconcile_cycle

    class _Broker:
        async def positions(self):
            return []

    class _Ledger:
        async def all_orders(self):
            return []

    store = InMemoryHealthStore()
    rec = await run_reconcile_cycle(store, _Broker(), _Ledger(), now=NOW)
    assert rec.last_result == "pass" and rec.last_ok_at == NOW
