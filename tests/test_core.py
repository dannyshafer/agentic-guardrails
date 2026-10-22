"""Tests for the executor decision core + SimBroker (docs plan 5.1, 5.2).

Every state transition, guard, and named failure mode gets at least one test:
this is the Phase-1 bar (CI green before the first real order), applied to the
broker-agnostic core that ships first. Pure-core tests are plain; SimBroker
lifecycle/fault tests are async.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agentic_guardrails import guards as G
from agentic_guardrails import killswitch as K
from agentic_guardrails import pricing as P
from agentic_guardrails import reconciler as R
from agentic_guardrails import state_machine as SM
from agentic_guardrails.broker import (
    BrokerAuthExpired,
    BrokerMalformed,
    BrokerRejected,
    BrokerTimeout,
    BrokerTransient,
    Fault,
    SimBroker,
)
from agentic_guardrails.ids import broker_ref_id, client_order_id
from agentic_guardrails.types import (
    BrokerOrder,
    OrderIntent,
    OrderPlan,
    OrderState,
    PriceBasis,
    Quote,
    QuoteSource,
)

NOW = datetime(2026, 9, 18, 15, 0, tzinfo=timezone.utc)
EXP = date(2026, 10, 16)


def _quote(bid, ask, source=QuoteSource.ROBINHOOD, sym="O:TQQQ261016P00080000"):
    return Quote(contract_symbol=sym, bid=Decimal(str(bid)), ask=Decimal(str(ask)), source=source, as_of=NOW)


def _plan(intent=OrderIntent.STO, qty=1, limit="1.50", strike="80", cid=None):
    basis = PriceBasis("sto_bid", QuoteSource.ROBINHOOD, Decimal("1.50"), Decimal("1.60"), NOW)
    return OrderPlan(
        client_order_id=cid or client_order_id("pcs_skewivz_10w", "O:TQQQ261016P00080000", intent, date(2026, 9, 18)),
        strategy="pcs_skewivz_10w",
        contract_symbol="O:TQQQ261016P00080000",
        intent=intent,
        quantity=qty,
        limit_price=Decimal(limit),
        strike=Decimal(strike),
        expiration=EXP,
        basis=basis,
        created_at=NOW,
    )


# ── types ─────────────────────────────────────────────────────────────────────
def test_orderplan_invariants_and_collateral():
    with pytest.raises(ValueError):
        _plan(qty=0)
    with pytest.raises(ValueError):
        _plan(limit="0")
    with pytest.raises(ValueError):
        _plan(strike="0")
    # STO ties up strike*100*qty; BTC frees collateral (0 here).
    assert _plan(intent=OrderIntent.STO, qty=3, strike="80").collateral() == Decimal("24000")
    assert _plan(intent=OrderIntent.BTC).collateral() == Decimal(0)


# ── state machine ─────────────────────────────────────────────────────────────
def test_transitions_legal_and_illegal():
    assert SM.is_legal(OrderState.PLANNED, OrderState.PLACED)
    assert SM.is_legal(OrderState.PLACED, OrderState.PARTIAL)
    assert SM.is_legal(OrderState.PLACED, OrderState.UNKNOWN)
    assert SM.is_legal(OrderState.UNKNOWN, OrderState.FILLED)
    # A terminal order moves nowhere.
    assert not SM.is_legal(OrderState.FILLED, OrderState.PLACED)
    assert not SM.is_legal(OrderState.CANCELED, OrderState.PARTIAL)
    # PLANNED cannot jump straight to FILLED.
    assert not SM.is_legal(OrderState.PLANNED, OrderState.FILLED)
    with pytest.raises(SM.IllegalTransition):
        SM.assert_legal(OrderState.FILLED, OrderState.PLACED)


def test_apply_broker_status_keeps_high_water_mark():
    v = SM.OrderView(current=OrderState.PLACED, filled_qty=0, avg_fill_price=None)
    # Partial fill 3 @ 1.50.
    v = SM.apply_broker_status(v, BrokerOrder("b1", OrderState.PARTIAL, 3, Decimal("1.50")))
    assert v.current == OrderState.PARTIAL and v.filled_qty == 3 and v.avg_fill_price == Decimal("1.50")
    # A STALE read reporting fewer filled must NOT shrink our booked size.
    v2 = SM.apply_broker_status(v, BrokerOrder("b1", OrderState.PARTIAL, 1, Decimal("9.99")))
    assert v2.filled_qty == 3 and v2.avg_fill_price == Decimal("1.50")
    # CANCELED with a completed slow fill books the fill, not a discard.
    v3 = SM.apply_broker_status(v2, BrokerOrder("b1", OrderState.CANCELED, 5, Decimal("1.52")))
    assert v3.current == OrderState.CANCELED and v3.filled_qty == 5 and v3.avg_fill_price == Decimal("1.52")


def test_to_unknown_and_terminal_stays_terminal():
    working = SM.OrderView(OrderState.PLACED, 0, None)
    assert SM.to_unknown(working).current == OrderState.UNKNOWN
    done = SM.OrderView(OrderState.FILLED, 5, Decimal("1.50"))
    assert SM.to_unknown(done).current == OrderState.FILLED  # a timeout does not un-finalize


# ── pricing ───────────────────────────────────────────────────────────────────
def test_sto_limit_defaults_to_bid_and_shades_with_k():
    q = _quote("1.50", "1.70")  # mid 1.60, half 0.10
    assert P.sto_limit(q) == Decimal("1.50")  # default k=1 -> the bid, paper-consistent
    assert P.sto_limit(q, k=Decimal("0.5")) == Decimal("1.55")  # mid - 0.5*0.10, shaded toward mid
    assert P.sto_limit(q, k=Decimal("0")) == Decimal("1.60")  # k=0 -> the mid
    assert P.sto_limit(q, k=Decimal("5")) == Decimal("1.50")  # k>=1 clamps to the bid, never below
    with pytest.raises(ValueError):
        P.sto_limit(q, k=Decimal("-1"))


def test_btc_limit_at_target_and_bounded_by_ask():
    q = _quote("0.60", "0.80")
    # 50% of a 1.50 credit = 0.75, penny-ceiled, and below the 0.80 ask.
    assert P.btc_limit(q, Decimal("1.50")) == Decimal("0.75")
    # Target above the ask is capped at the ask (never overpay to close).
    assert P.btc_limit(q, Decimal("2.00")) == Decimal("0.80")


def test_nbbo_agreement_tolerance():
    a = _quote("1.50", "1.60")  # mid 1.55
    b = _quote("1.52", "1.62", source=QuoteSource.MASSIVE)  # mid 1.57
    assert P.nbbo_agreement(a, b).agree  # 0.02 within the 0.05 floor
    far = _quote("2.00", "2.20", source=QuoteSource.MASSIVE)  # mid 2.10, delta 0.55
    chk = P.nbbo_agreement(a, far)
    assert not chk.agree and chk.delta == Decimal("0.55")


# ── ids ───────────────────────────────────────────────────────────────────────
def test_client_order_id_is_deterministic_and_intent_sensitive():
    a = client_order_id("arm", "O:X", OrderIntent.STO, date(2026, 9, 18))
    b = client_order_id("arm", "O:X", OrderIntent.STO, date(2026, 9, 18))
    assert a == b and a.startswith("sat_")
    assert client_order_id("arm", "O:X", OrderIntent.BTC, date(2026, 9, 18)) != a
    assert client_order_id("arm", "O:X", OrderIntent.STO, date(2026, 9, 19)) != a


def test_broker_ref_id_is_a_deterministic_uuid():
    # The broker rejects a non-UUID ref_id; the broker-facing token must be a
    # valid UUID, stable for a given coid, and distinct per coid.
    import uuid

    coid = client_order_id("arm", "O:X", OrderIntent.STO, date(2026, 9, 18))
    ref = broker_ref_id(coid)
    assert uuid.UUID(ref)  # parses as a UUID (would raise otherwise)
    assert broker_ref_id(coid) == ref  # deterministic
    assert broker_ref_id("sat_other") != ref  # distinct per coid
    assert not ref.startswith("sat_")  # no longer the raw internal id


# ── guards ────────────────────────────────────────────────────────────────────
def _ctx(**kw):
    base = dict(
        free_cash=Decimal("50000"),
        open_collateral_total=Decimal(0),
        open_collateral_ticker=Decimal(0),
        open_collateral_strategy=Decimal(0),
        committed_today=Decimal(0),
        ticker="TQQQ",
        strategy_has_unknown_order=False,
        inflight_states_for_contract=(),
    )
    base.update(kw)
    return G.ExecContext(**base)


def test_guard_cash_secured_and_caps():
    plan = _plan(qty=1, strike="80")  # collateral 8000
    assert G.evaluate(plan, _ctx(free_cash=Decimal("8000")), G.Caps()).allow
    assert not G.evaluate(plan, _ctx(free_cash=Decimal("7999")), G.Caps()).allow
    # Free cash unreadable (<=0) fails closed.
    assert not G.guard_cash_secured(plan, _ctx(free_cash=Decimal(0)), G.Caps()).allow
    # Per-order cap.
    assert not G.guard_caps(plan, _ctx(), G.Caps(per_order=Decimal("5000"))).allow
    # Per-day cap counts what is already committed today.
    caps = G.Caps(per_day=Decimal("10000"))
    assert not G.guard_caps(plan, _ctx(committed_today=Decimal("5000")), caps).allow  # 5000+8000 > 10000
    assert G.guard_caps(plan, _ctx(committed_today=Decimal("1000")), caps).allow


def test_guard_unknown_blocks_opens_but_not_closes():
    ctx = _ctx(strategy_has_unknown_order=True)
    assert not G.guard_unknown_blocks_opens(_plan(intent=OrderIntent.STO), ctx, G.Caps()).allow
    # A BTC (reducing risk) is allowed through even while blind.
    assert G.guard_unknown_blocks_opens(_plan(intent=OrderIntent.BTC), ctx, G.Caps()).allow


def test_guard_one_in_flight():
    ctx = _ctx(inflight_states_for_contract=(OrderState.PARTIAL,))
    assert not G.guard_one_in_flight(_plan(), ctx, G.Caps()).allow
    # A terminal prior order does not block a new one.
    ctx2 = _ctx(inflight_states_for_contract=(OrderState.FILLED, OrderState.CANCELED))
    assert G.guard_one_in_flight(_plan(), ctx2, G.Caps()).allow


def test_guard_has_limit_is_defense_in_depth():
    # OrderPlan already refuses a non-positive limit, so build a valid plan and
    # assert the guard passes it; the guard's job is the money-path double-check.
    assert G.guard_has_limit(_plan(limit="1.50"), _ctx(), G.Caps()).allow


# ── reconciler ────────────────────────────────────────────────────────────────
def test_reconcile_classifies_each_direction():
    # UNDER: broker holds more short than the ledger booked -> heal up.
    f = R.classify_contract("O:X", ledger_qty=-3, broker_qty=-5)
    assert f.drift == R.DriftClass.UNDER
    # OVER: ledger claims a short the broker denies -> halt.
    f = R.classify_contract("O:X", ledger_qty=-5, broker_qty=-3)
    assert f.drift == R.DriftClass.OVER
    # ASSIGNMENT: short gone + shares appeared.
    f = R.classify_contract("O:X", ledger_qty=-5, broker_qty=0, shares_appeared=True)
    assert f.drift == R.DriftClass.ASSIGNMENT
    # MATCH.
    assert R.classify_contract("O:X", -5, -5).drift == R.DriftClass.MATCH


def test_reconcile_over_the_whole_book_and_blocking():
    findings = R.reconcile(ledger={"O:A": -5, "O:B": -2}, broker={"O:A": -5, "O:B": -1})
    by = {f.contract_symbol: f.drift for f in findings}
    assert by["O:A"] == R.DriftClass.MATCH
    assert by["O:B"] == R.DriftClass.OVER  # ledger 2 short, broker 1 short
    assert R.has_blocking_drift(findings)


# ── killswitch ────────────────────────────────────────────────────────────────
def _kill(**kw):
    base = dict(
        executor_enabled=True,
        strategy_pnl_today=Decimal(0),
        strategy_daily_stop=Decimal("500"),
        global_pnl_today=Decimal(0),
        global_daily_stop=Decimal("2000"),
        last_reconcile_ok=K.Freshness(NOW),
        last_selftest_ok=K.Freshness(NOW),
    )
    base.update(kw)
    return K.KillState(**base)


def test_killswitch_layers():
    # All clear.
    assert not K.evaluate_halt(_kill(), NOW).halted
    # Disabled flag halts first.
    assert K.evaluate_halt(_kill(executor_enabled=False), NOW).halted
    # Strategy daily loss stop.
    assert K.evaluate_halt(_kill(strategy_pnl_today=Decimal("-500")), NOW).halted
    # Global daily loss stop.
    assert K.evaluate_halt(_kill(global_pnl_today=Decimal("-2001")), NOW).halted
    # Dead-man gate: a stale reconcile halts.
    stale = _kill(last_reconcile_ok=K.Freshness(NOW - timedelta(hours=2)))
    assert K.evaluate_halt(stale, NOW).halted
    # Never-succeeded self-test halts (fail closed).
    assert K.evaluate_halt(_kill(last_selftest_ok=K.Freshness(None)), NOW).halted


# ── SimBroker lifecycles + faults ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_simbroker_happy_lifecycle():
    plan = _plan(qty=5)
    sim = SimBroker(quotes={plan.contract_symbol: _quote("1.50", "1.60")})
    placed = await sim.place(plan)
    assert placed.state == OrderState.PLACED and placed.filled_qty == 0
    view = SM.OrderView(OrderState.PLACED, 0, None)
    read = await sim.status(placed.broker_order_id)  # script advances to FILLED
    view = SM.apply_broker_status(view, read)
    assert view.current == OrderState.FILLED and view.filled_qty == 5


@pytest.mark.asyncio
async def test_simbroker_partial_then_slow_fill_after_cancel():
    plan = _plan(qty=5, cid="cid-slow")
    sim = SimBroker(
        quotes={plan.contract_symbol: _quote("1.50", "1.60")},
        scripts={"cid-slow": [(OrderState.PLACED, 0), (OrderState.PARTIAL, 3), (OrderState.CANCELED, 5)]},
    )
    placed = await sim.place(plan)
    view = SM.OrderView(OrderState.PLACED, 0, None)
    view = SM.apply_broker_status(view, await sim.status(placed.broker_order_id))  # PARTIAL 3
    assert view.current == OrderState.PARTIAL and view.filled_qty == 3
    # We cancel, then RE-READ: the slow fill completed to 5 after the cancel.
    await sim.cancel(placed.broker_order_id)
    view = SM.apply_broker_status(view, await sim.status(placed.broker_order_id))
    assert view.current == OrderState.CANCELED and view.filled_qty == 5  # kept the high-water mark


@pytest.mark.asyncio
async def test_simbroker_timeout_after_accept_is_findable_by_reconcile():
    plan = _plan(cid="cid-timeout")
    sim = SimBroker(quotes={plan.contract_symbol: _quote("1.50", "1.60")}, faults={"place": [Fault.TIMEOUT]})
    with pytest.raises(BrokerTimeout):
        await sim.place(plan)
    # The order EXISTS despite the timeout: a positions/status read finds it, so
    # the engine goes UNKNOWN (fail closed) rather than blind-replacing.
    view = SM.to_unknown(SM.OrderView(OrderState.PLACED, 0, None))
    assert view.current == OrderState.UNKNOWN
    boid = sim._by_client["cid-timeout"]
    resolved = SM.apply_broker_status(view, await sim.status(boid))
    assert resolved.current in (OrderState.PLACED, OrderState.FILLED)


@pytest.mark.asyncio
async def test_simbroker_idempotent_replace():
    plan = _plan(cid="cid-dup")
    sim = SimBroker(quotes={plan.contract_symbol: _quote("1.50", "1.60")})
    a = await sim.place(plan)
    b = await sim.place(plan)  # same client_order_id -> same broker order, not a second
    assert a.broker_order_id == b.broker_order_id
    assert len(sim._orders) == 1


@pytest.mark.asyncio
async def test_simbroker_place_reject():
    plan = _plan()
    sim = SimBroker(quotes={plan.contract_symbol: _quote("1.50", "1.60")}, faults={"place": [Fault.REJECT]})
    with pytest.raises(BrokerRejected):
        await sim.place(plan)
    assert not sim._orders  # a rejected place created no order


@pytest.mark.asyncio
async def test_simbroker_read_fault_taxonomy():
    plan = _plan()
    sim = SimBroker(
        quotes={plan.contract_symbol: _quote("1.50", "1.60")},
        faults={"status": [Fault.TRANSIENT, Fault.AUTH, Fault.MALFORMED]},
    )
    p = await sim.place(plan)
    for exc in (BrokerTransient, BrokerAuthExpired, BrokerMalformed):
        with pytest.raises(exc):
            await sim.status(p.broker_order_id)


@pytest.mark.asyncio
async def test_simbroker_assignment_shows_in_positions():
    from agentic_guardrails.types import Position

    plan = _plan()
    sim = SimBroker(
        quotes={plan.contract_symbol: _quote("1.50", "1.60")},
        positions_state=[Position("TQQQ", quantity=100, avg_price=Decimal("80"))],  # assigned shares
    )
    pos = await sim.positions()
    # The short put is gone from positions and long shares appeared: the
    # reconciler classifies this as ASSIGNMENT, not an OVER-drift anomaly.
    ledger = {plan.contract_symbol: -1}
    broker = {p.contract_symbol: p.quantity for p in pos}  # {"TQQQ": 100}, no short
    findings = R.reconcile(
        ledger, {plan.contract_symbol: 0}, shares_by_underlying={"TQQQ": broker.get("TQQQ", 0) > 0}, underlying_of=lambda s: "TQQQ"
    )
    assert findings[0].drift == R.DriftClass.ASSIGNMENT
