"""The runtime layer: every control connected, asked the right question, on the right cadence."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from agentic_guardrails import assignment_guard as ag
from agentic_guardrails import killswitch
from agentic_guardrails.broker import SimBroker
from agentic_guardrails.config import GuardrailConfig, OpenSanity
from agentic_guardrails.engine import Engine, EngineConfig
from agentic_guardrails.guards import Caps
from agentic_guardrails.health import HealthRecord, InMemoryHealthStore
from agentic_guardrails.ledger import InMemoryLedger, OrderRecord
from agentic_guardrails.runtime import (
    AssignmentGuardLoop,
    DrillResult,
    GuardLoopHooks,
    check_health_row,
    check_reconcile_cadence,
    check_resting_closes,
    close_limit,
    guard_kill_state,
    guard_spot,
    limit_below_live_bid_reason,
    next_close_coid,
    open_kill_state,
    open_limit,
    open_sanity_reason,
    readable_spot,
    realized_pnl_today,
    run_fill_audit,
    scan_quote_disagreement,
    summarize_drill,
)
from agentic_guardrails.runtime.vetting import GUARD_STRATEGY
from agentic_guardrails.types import Account, BrokerOrder, OrderIntent, OrderState, Position, Quote, QuoteSource

NOW = datetime(2026, 10, 22, 15, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
PUT = "O:TQQQ261023P00080000"  # expires tomorrow


def _rec(coid, strategy, sym, intent, state, filled, px, *, created=NOW, updated=NOW, shadow=False, strike=Decimal(80)):
    return OrderRecord(
        client_order_id=coid,
        strategy=strategy,
        contract_symbol=sym,
        intent=intent,
        quantity=max(filled, 1),
        limit_price=px,
        strike=strike,
        expiration=date(2026, 10, 23),
        state=state,
        filled_qty=filled,
        avg_fill_price=px if filled else None,
        broker_order_id="b" if filled else None,
        basis_rule="bid",
        basis_source="live",
        shadow=shadow,
        created_at=created,
        updated_at=updated,
    )


def run(c):
    return asyncio.run(c)


# ── vetting ──────────────────────────────────────────────────────────────────
def test_realized_pnl_pairs_a_guard_close_to_the_arm_that_opened_it():
    recs = [
        _rec("o", "arm", PUT, OrderIntent.STO, OrderState.FILLED, 1, Decimal("1.00"), created=NOW - timedelta(days=3)),
        _rec("c", GUARD_STRATEGY, PUT, OrderIntent.BTC, OrderState.FILLED, 1, Decimal("1.50")),
    ]
    by, total, ok = realized_pnl_today(recs, TODAY)
    assert ok and by == {"arm": Decimal("-50")} and total == Decimal("-50")


def test_realized_pnl_fails_closed_on_an_unattributable_close():
    recs = [_rec("c", "arm", PUT, OrderIntent.BTC, OrderState.FILLED, 1, Decimal("1.50"))]
    _, _, ok = realized_pnl_today(recs, TODAY)
    assert ok is False


def test_open_limit_lifts_to_the_bid_and_caps_at_the_ask():
    assert open_limit("0.16", "0.58", "0.62") == Decimal("0.58")
    assert open_limit("0.70", "0.58", "0.62") == Decimal("0.62")
    assert open_limit(None, "0.58", None) == Decimal("0.58")
    assert open_limit("nan", None, None) is None


def test_scan_quote_disagreement_fails_closed_and_passes_agreement():
    assert scan_quote_disagreement("1.00", "1.05", "0.50", "0.55") is not None
    assert scan_quote_disagreement("1.00", "1.05", "1.00", "1.10") is None
    assert "not an NBBO" in scan_quote_disagreement("1.00", "1.05", "1.00", "1.10", recorded_source="theo")
    assert "unreadable" in scan_quote_disagreement("1.00", "1.05", None, "1.10")
    assert limit_below_live_bid_reason("0.16", "0.58") is not None and limit_below_live_bid_reason("0.55", "0.58") is None


def test_open_sanity_floors_and_stack_rule():
    cfg = GuardrailConfig()
    base = dict(cfg=cfg, ticker="TQQQ", strike=Decimal(80), today=TODAY, records=[], spot=Decimal(95))
    assert "credit" in open_sanity_reason(**base, expiration=TODAY + timedelta(days=30), limit=Decimal("0.05"))
    assert "DTE" in open_sanity_reason(**base, expiration=TODAY + timedelta(days=2), limit=Decimal("1.00"))
    assert "/yr" in open_sanity_reason(**base, expiration=TODAY + timedelta(days=30), limit=Decimal("0.11"))
    assert open_sanity_reason(**base, expiration=TODAY + timedelta(days=30), limit=Decimal("1.50")) is None
    guard_close = _rec("c", GUARD_STRATEGY, PUT, OrderIntent.BTC, OrderState.FILLED, 1, Decimal("1.50"))
    assert "same-day" in open_sanity_reason(
        **{**base, "records": [guard_close]}, expiration=TODAY + timedelta(days=30), limit=Decimal("1.50")
    )
    threatened = _rec("o", "arm", PUT, OrderIntent.STO, OrderState.FILLED, 1, Decimal("1.00"))
    assert "threatened" in open_sanity_reason(
        **{**base, "records": [threatened], "spot": Decimal(81)}, expiration=TODAY + timedelta(days=30), limit=Decimal("1.50")
    )
    # Stacking: a second NDX short past 40% of equity is refused; with no equity read the rule is off.
    held = _rec("o", "arm", PUT, OrderIntent.STO, OrderState.FILLED, 1, Decimal("1.00"), strike=Decimal(80))
    r = open_sanity_reason(
        **{**base, "records": [held], "ticker": "QLD"}, expiration=TODAY + timedelta(days=30), limit=Decimal("1.50"), equity=Decimal(20000)
    )
    assert r is not None and "stack" in r
    assert (
        open_sanity_reason(**{**base, "records": [held], "ticker": "QLD"}, expiration=TODAY + timedelta(days=30), limit=Decimal("1.50"))
        is None
    )


def test_sanity_uses_the_config_floors_not_module_constants():
    cfg = GuardrailConfig(open_sanity=OpenSanity(min_credit=Decimal("2.00")))
    assert "credit" in open_sanity_reason(
        cfg=cfg,
        ticker="TQQQ",
        strike=Decimal(80),
        expiration=TODAY + timedelta(days=30),
        limit=Decimal("1.50"),
        spot=None,
        today=TODAY,
        records=[],
    )


# ── kill states ──────────────────────────────────────────────────────────────
def test_guard_kill_state_never_halts_and_open_kill_state_reads_real_inputs():
    g = guard_kill_state(NOW)
    assert not killswitch.deadman_gate(g, NOW, max_reconcile_age=timedelta(minutes=30), max_selftest_age=timedelta(hours=20)).halted
    stale = killswitch.Freshness(last_ok_at=None)
    o = open_kill_state(GuardrailConfig(enabled=True), stale, stale, Decimal(0), Decimal(0))
    assert killswitch.deadman_gate(o, NOW, max_reconcile_age=timedelta(minutes=30), max_selftest_age=timedelta(hours=20)).halted
    assert o.strategy_daily_stop == Decimal(1500) and o.executor_enabled is True


# ── spot ─────────────────────────────────────────────────────────────────────
def test_guard_spot_lower_wins_on_divergence_and_unreadable_feeds_fail_safe():
    assert readable_spot(float("nan")) is None and readable_spot("95.5") == Decimal("95.5")
    assert guard_spot(100, 90, 60) == (Decimal(90), "secondary", 0.1)
    assert guard_spot(100, 99.5, 60)[1] == "primary"
    assert guard_spot(None, 90, None) == (None, None, None)  # undated secondary is not trusted
    assert guard_spot(None, 90, 60)[0] == Decimal(90)
    assert guard_spot(float("nan"), None) == (None, None, None)


# ── drill + fill audit ───────────────────────────────────────────────────────
def test_drill_checks():
    assert check_reconcile_cadence(600).status == "ok" and check_reconcile_cadence(86400).status == "noop"
    assert check_health_row("x", None, NOW, timedelta(hours=1), market_open=True).status == "noop"
    rec = HealthRecord("x", NOW - timedelta(hours=2), NOW - timedelta(hours=2), "pass", None)
    assert check_health_row("x", rec, NOW, timedelta(hours=1), market_open=False).status == "warn"
    assert check_health_row("x", replace(rec, last_ok_at=None), NOW, timedelta(hours=1), market_open=True).status == "noop"
    assert check_resting_closes([("a", PUT)], set(), market_open=True).status == "noop"
    ok, text = summarize_drill([DrillResult("a", "ok", "fine"), DrillResult("b", "noop", "dead")])
    assert not ok and "NOOP  b" in text


def test_fill_audit_pages_on_a_price_drift_and_never_edits_the_ledger():
    led, store = InMemoryLedger(), InMemoryHealthStore()
    rec = _rec("o", "arm", PUT, OrderIntent.STO, OrderState.FILLED, 1, Decimal("1.00"))
    run(led.upsert(rec))

    class B:
        async def status(self, boid):
            return BrokerOrder(boid, OrderState.FILLED, 1, Decimal("1.10"))

    ok, detail = run(run_fill_audit(store, B(), led, NOW))
    assert not ok and "$10.00" in detail
    assert run(led.get("o")).avg_fill_price == Decimal("1.00")
    assert run(store.get("fill_audit")).last_ok_at is None


# ── guard loop ───────────────────────────────────────────────────────────────
def test_close_limit_pads_each_retry_and_next_coid_walks_terminal_non_fills():
    assert close_limit(Decimal("1.00"), "x") == Decimal("1.00")
    assert close_limit(Decimal("1.00"), "x_r2") == Decimal("1.10")
    assert close_limit(Decimal("1.00"), "x_r20") == Decimal("1.50")  # capped at +50%
    led = InMemoryLedger()
    coid, _ = run(next_close_coid(led, PUT, OrderIntent.BTC, TODAY, 1))
    run(led.upsert(_rec(coid, GUARD_STRATEGY, PUT, OrderIntent.BTC, OrderState.CANCELED, 0, Decimal(1))))
    coid2, _ = run(next_close_coid(led, PUT, OrderIntent.BTC, TODAY, 1))
    assert coid2 == coid + "_r1"
    run(led.upsert(_rec(coid2, GUARD_STRATEGY, PUT, OrderIntent.BTC, OrderState.PLACED, 0, Decimal(1))))
    assert run(next_close_coid(led, PUT, OrderIntent.BTC, TODAY, 1))[0] is None


def _loop(armed: bool, delta="-0.60"):
    broker = SimBroker(account_state=Account(free_cash=Decimal(20000), buying_power=Decimal(20000)))
    broker.positions_state = [Position(PUT, -1, Decimal("1.00"))]
    broker.quotes[PUT] = Quote(
        PUT, Decimal("2.00"), Decimal("2.10"), QuoteSource.ROBINHOOD, NOW, delta=Decimal(delta), mark=Decimal("2.05")
    )
    ledger = InMemoryLedger()
    engine = Engine(broker, ledger, EngineConfig(caps=Caps(per_order=Decimal(20000)), shadow=False), clock=lambda: NOW)
    alerts = []

    async def alert(t):
        alerts.append(t)

    async def spot(ticker):
        return Decimal(79), None, None

    hooks = GuardLoopHooks(spot=spot, alert=alert)
    return AssignmentGuardLoop(broker, ledger, engine, hooks=hooks, armed=armed, clock=lambda: NOW), broker, ledger, alerts


def test_guard_loop_shadow_alerts_and_places_nothing():
    loop, broker, ledger, alerts = _loop(armed=False)
    decisions = run(loop.tick())
    assert decisions and decisions[0].action == ag.GuardAction.CLOSE
    assert any("SHADOW" in a and "CLOSE" in a for a in alerts)
    assert run(ledger.all_orders()) == []


def test_guard_loop_armed_closes_the_short_through_the_engine():
    loop, broker, ledger, alerts = _loop(armed=True)
    run(loop.tick())
    recs = run(ledger.all_orders())
    assert (
        len(recs) == 1 and recs[0].intent == OrderIntent.BTC and recs[0].state == OrderState.FILLED and recs[0].strategy == GUARD_STRATEGY
    )
    assert recs[0].limit_price == Decimal("2.10")  # marketable at the ask on the first attempt
    assert any("CLOSE" in a and "filled 1" in a for a in alerts)


def test_guard_loop_defers_after_hours_once_per_day():
    loop, broker, ledger, alerts = _loop(armed=True)
    loop.hooks.market_open = lambda: False
    run(loop.tick())
    run(loop.tick())
    assert run(ledger.all_orders()) == []
    assert sum("deferring" in a for a in alerts) == 1


def test_guard_loop_holds_a_safe_put_and_skips_longs():
    loop, broker, ledger, alerts = _loop(armed=True, delta="-0.05")

    async def spot(ticker):
        return Decimal(95), None, None

    loop.hooks.spot = spot
    broker.positions_state.append(Position("O:TQQQ261023C00090000", 1, Decimal("1.00")))
    d = run(loop.tick())
    assert len(d) == 1 and d[0].action in (ag.GuardAction.HOLD, ag.GuardAction.LET_EXPIRE)
    assert run(ledger.all_orders()) == []
