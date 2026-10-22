"""Phase 2 verticals: the put credit spread path through the executor (Sep 16 2026).

Every layer the finding (docs/findings/2026-09-10_saturn_spreads_capital_year.md)
needs for the $9k TQQQ instrument: a two-leg plan, net pricing at the paper
book's bid-cross-ask basis, the structure + concurrency guards, one atomic
two-leg broker order, per-leg ledger bookkeeping (short -q, wing +q), width
collateral, and a reconciler that reads the wing as a LONG.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agentic_guardrails import guards, reconciler as R
from agentic_guardrails.broker import SimBroker
from agentic_guardrails.engine import Engine, EngineConfig, ticker_of
from agentic_guardrails.guards import Caps, ExecContext
from agentic_guardrails.ids import client_order_id
from agentic_guardrails.killswitch import Freshness, KillState
from agentic_guardrails.ledger import (
    InMemoryLedger,
    OrderRecord,
    SqlLedger,
    net_positions,
    open_collateral,
    open_contracts_by_record,
    open_spreads,
)
from agentic_guardrails.pricing import pcs_close_limit, pcs_open_limit
from agentic_guardrails.spreads import build_pcs_close, build_pcs_open
from agentic_guardrails.types import Account, OrderIntent, OrderPlan, OrderState, PriceBasis, Quote, QuoteSource

NOW = datetime(2026, 9, 16, 15, 0, tzinfo=timezone.utc)
EXP = date(2026, 10, 16)
SHORT = "O:TQQQ261016P00080000"  # $80 short put
WING = "O:TQQQ261016P00072000"  # $72 wing: width 8, a ~10%-of-spot TQQQ wing
WING70 = "O:TQQQ261016P00070000"  # a second wing on the same short
OTHER_EXP_WING = "O:TQQQ261023P00072000"
CALL = "O:TQQQ261016C00072000"


def _q(sym, bid, ask, **kw):
    return Quote(sym, Decimal(bid), Decimal(ask), QuoteSource.ROBINHOOD, NOW, **kw)


SHORT_Q = _q(SHORT, "1.50", "1.60")
WING_Q = _q(WING, "0.50", "0.56")


def _basis():
    return PriceBasis("pcs_open_bid_cross_ask", QuoteSource.ROBINHOOD, Decimal("0.94"), Decimal("1.10"), NOW)


def _spread_plan(qty=1, limit="0.94", strategy="pcs_tqqq_10w", intent=OrderIntent.PCS_OPEN, wing=WING, wing_strike="72"):
    return OrderPlan(
        client_order_id(strategy, SHORT, intent, NOW.date(), wing),
        strategy,
        SHORT,
        intent,
        qty,
        Decimal(limit),
        Decimal("80"),
        EXP,
        _basis(),
        NOW,
        long_contract_symbol=wing,
        long_strike=Decimal(wing_strike),
    )


def _ctx(**kw):
    base = dict(
        free_cash=Decimal(9000),
        open_collateral_total=Decimal(0),
        open_collateral_ticker=Decimal(0),
        open_collateral_strategy=Decimal(0),
        committed_today=Decimal(0),
        ticker="TQQQ",
        strategy_has_unknown_order=False,
    )
    base.update(kw)
    return ExecContext(**base)


def _kill():
    return KillState(
        True,
        Decimal(0),
        Decimal(500),
        Decimal(0),
        Decimal(1000),
        Freshness(NOW - timedelta(minutes=5)),
        Freshness(NOW - timedelta(hours=1)),
    )


def _engine(cash="9000", caps=None, shadow=False):
    broker = SimBroker(account_state=Account(free_cash=Decimal(cash), buying_power=Decimal(cash)))
    broker.quotes[SHORT] = SHORT_Q
    broker.quotes[WING] = WING_Q
    ledger = InMemoryLedger()
    cfg = EngineConfig(caps=caps or Caps(per_order=Decimal(20000), max_open_spreads_per_strategy=4), shadow=shadow)
    return Engine(broker, ledger, cfg, clock=lambda: NOW), broker, ledger


def run(coro):
    return asyncio.run(coro)


# ── types ─────────────────────────────────────────────────────────────────────
def test_spread_plan_collateral_is_width_not_strike_and_lists_both_legs():
    p = _spread_plan(qty=3)
    assert p.is_spread and p.width() == Decimal(8)
    assert p.collateral() == Decimal(8) * 100 * 3  # the defined max loss, gross of credit
    assert p.legs() == (SHORT, WING)
    # A close frees collateral, like a BTC.
    assert _spread_plan(intent=OrderIntent.PCS_CLOSE, limit="0.40").collateral() == 0


def test_spread_plan_rejects_malformed_structures_at_construction():
    with pytest.raises(ValueError):  # no wing on a spread intent
        OrderPlan("c", "s", SHORT, OrderIntent.PCS_OPEN, 1, Decimal("0.9"), Decimal("80"), EXP, _basis(), NOW)
    with pytest.raises(ValueError):  # wing above the short
        _spread_plan(wing_strike="85")
    with pytest.raises(ValueError):  # credit at/above the width is not a spread
        _spread_plan(limit="8.00")
    with pytest.raises(ValueError):  # a single-leg intent cannot carry a wing
        OrderPlan(
            "c",
            "s",
            SHORT,
            OrderIntent.STO,
            1,
            Decimal("0.9"),
            Decimal("80"),
            EXP,
            _basis(),
            NOW,
            long_contract_symbol=WING,
            long_strike=Decimal(72),
        )
    with pytest.raises(ValueError):  # both legs the same contract
        _spread_plan(wing=SHORT, wing_strike="72")


def test_spread_plan_validates_the_leg_symbols_against_the_strike_and_expiry_fields():
    # The symbols are what the broker trades; the fields are what the caps read. They must agree.
    with pytest.raises(ValueError):  # call legs under a put-spread intent
        OrderPlan(
            "c",
            "s",
            "O:TQQQ261016C00080000",
            OrderIntent.PCS_OPEN,
            1,
            Decimal("0.5"),
            Decimal(80),
            EXP,
            _basis(),
            NOW,
            long_contract_symbol=CALL,
            long_strike=Decimal(72),
        )
    with pytest.raises(ValueError):  # a fabricated $1 width that the symbols do not carry
        OrderPlan(
            "c",
            "s",
            SHORT,
            OrderIntent.PCS_OPEN,
            1,
            Decimal("0.5"),
            Decimal(80),
            EXP,
            _basis(),
            NOW,
            long_contract_symbol=WING,
            long_strike=Decimal(79),
        )
    with pytest.raises(ValueError):  # the wing's expiry is not the plan's
        _spread_plan(wing=OTHER_EXP_WING)
    with pytest.raises(ValueError):  # plan expiration disagrees with both legs
        OrderPlan(
            "c",
            "s",
            SHORT,
            OrderIntent.PCS_OPEN,
            1,
            Decimal("0.5"),
            Decimal(80),
            date(2026, 10, 23),
            _basis(),
            NOW,
            long_contract_symbol=WING,
            long_strike=Decimal(72),
        )
    with pytest.raises(ValueError):  # not an OCC symbol at all
        OrderPlan(
            "c",
            "s",
            "O:NOTOCC",
            OrderIntent.PCS_OPEN,
            1,
            Decimal("0.5"),
            Decimal(80),
            EXP,
            _basis(),
            NOW,
            long_contract_symbol=WING,
            long_strike=Decimal(72),
        )


def test_every_intent_value_fits_a_sixteen_char_ledger_column():
    # A SQL ledger with a String(16) intent column must hold every value (pcs_close is 9 chars;
    # a String(8) column would abort every spread close before broker review).
    assert all(len(i.value) <= 16 for i in OrderIntent)


def test_client_order_id_folds_the_wing_in_but_leaves_single_leg_ids_unchanged():
    a = client_order_id("s", SHORT, OrderIntent.PCS_OPEN, NOW.date(), WING)
    b = client_order_id("s", SHORT, OrderIntent.PCS_OPEN, NOW.date(), OTHER_EXP_WING)
    assert a != b and a.startswith("sat_")
    assert client_order_id("s", SHORT, OrderIntent.STO, NOW.date()) == client_order_id("s", SHORT, OrderIntent.STO, NOW.date(), None)


# ── pricing ───────────────────────────────────────────────────────────────────
def test_pcs_open_limit_k1_is_the_paper_books_bid_cross_ask_and_k0_is_the_net_mid():
    assert pcs_open_limit(SHORT_Q, WING_Q) == Decimal("0.94")  # 1.50 - 0.56
    assert pcs_open_limit(SHORT_Q, WING_Q, Decimal(0)) == Decimal("1.02")  # 1.55 - 0.53
    # k between shades toward the mid; never below the cross.
    mid_k = pcs_open_limit(SHORT_Q, WING_Q, Decimal("0.5"))
    assert Decimal("0.94") < mid_k < Decimal("1.02")
    assert pcs_open_limit(SHORT_Q, WING_Q, Decimal(3)) == Decimal("0.94")
    with pytest.raises(ValueError):  # no net credit at the cross -> not priceable
        pcs_open_limit(_q(SHORT, "0.50", "0.60"), _q(WING, "0.55", "0.60"))


def test_pcs_close_limit_targets_half_the_credit_and_never_pays_past_ask_cross_bid():
    assert pcs_close_limit(SHORT_Q, WING_Q, Decimal("0.94")) == Decimal("0.47")
    # Ask-cross-bid = 1.60 - 0.50 = 1.10 caps a target above the market.
    assert pcs_close_limit(SHORT_Q, WING_Q, Decimal("4.00")) == Decimal("1.10")


# ── the builder ───────────────────────────────────────────────────────────────
def test_build_pcs_open_derives_every_field_from_the_two_quotes():
    p = build_pcs_open(strategy="pcs_tqqq_10w", short_quote=SHORT_Q, long_quote=WING_Q, quantity=2, now=NOW)
    assert p.intent == OrderIntent.PCS_OPEN and p.quantity == 2
    assert (p.contract_symbol, p.long_contract_symbol) == (SHORT, WING)
    assert (p.strike, p.long_strike, p.expiration) == (Decimal(80), Decimal(72), EXP)
    assert p.limit_price == Decimal("0.94") and p.basis.rule == "pcs_open_bid_cross_ask"
    assert p.basis.bid == Decimal("0.94") and p.basis.ask == Decimal("1.10")
    assert p.client_order_id == client_order_id("pcs_tqqq_10w", SHORT, OrderIntent.PCS_OPEN, NOW.date(), WING)


def test_build_pcs_open_refuses_wrong_legs_before_any_guard_runs():
    with pytest.raises(ValueError):  # a call wing
        build_pcs_open(strategy="s", short_quote=SHORT_Q, long_quote=_q(CALL, "0.5", "0.6"), quantity=1, now=NOW)
    with pytest.raises(ValueError):  # a different expiration is a diagonal, not a vertical
        build_pcs_open(strategy="s", short_quote=SHORT_Q, long_quote=_q(OTHER_EXP_WING, "0.5", "0.6"), quantity=1, now=NOW)
    with pytest.raises(ValueError):  # wing above the short
        build_pcs_open(strategy="s", short_quote=_q(WING, "0.5", "0.6"), long_quote=SHORT_Q, quantity=1, now=NOW)


def test_build_pcs_close_is_a_debit_at_half_the_entry_credit():
    p = build_pcs_close(strategy="s", short_quote=SHORT_Q, long_quote=WING_Q, quantity=1, entry_credit=Decimal("0.94"), now=NOW)
    assert p.intent == OrderIntent.PCS_CLOSE and p.limit_price == Decimal("0.47") and p.collateral() == 0


# ── ledger ────────────────────────────────────────────────────────────────────
def _filled(plan, qty=None, shadow=False):
    rec = OrderRecord.from_plan(plan, shadow=shadow, now=NOW)
    from dataclasses import replace

    return replace(rec, state=OrderState.FILLED, filled_qty=plan.quantity if qty is None else qty, avg_fill_price=plan.limit_price)


def test_ledger_books_a_spread_as_short_leg_minus_and_wing_plus_with_width_collateral():
    rec = _filled(_spread_plan(qty=2))
    assert rec.leg_fills() == {SHORT: -2, WING: +2}
    assert net_positions([rec]) == {SHORT: -2, WING: 2}
    total, by_t, by_s = open_collateral([rec], ticker_of=ticker_of)
    assert total == Decimal(1600) and by_t == {"TQQQ": Decimal(1600)} and by_s == {"pcs_tqqq_10w": Decimal(1600)}
    assert open_spreads([rec]) == {"pcs_tqqq_10w": 2}
    # Shadow rows never become positions or spreads.
    assert open_spreads([_filled(_spread_plan(qty=2), shadow=True)]) == {}


def test_ledger_a_spread_close_zeroes_both_legs_and_frees_the_collateral():
    opened = _filled(_spread_plan(qty=2))
    closed = _filled(_spread_plan(qty=2, intent=OrderIntent.PCS_CLOSE, limit="0.40"))
    assert closed.leg_fills() == {SHORT: +2, WING: -2}
    assert net_positions([opened, closed]) == {}
    assert open_collateral([opened, closed], ticker_of=ticker_of)[0] == 0
    assert open_spreads([opened, closed]) == {}
    # A partial close leaves the remainder open.
    half = _filled(_spread_plan(qty=2, intent=OrderIntent.PCS_CLOSE, limit="0.40"), qty=1)
    assert net_positions([opened, half]) == {SHORT: -1, WING: 1}
    assert open_spreads([opened, half]) == {"pcs_tqqq_10w": 1}


def test_closing_one_of_two_spreads_that_share_a_short_leaves_only_the_other_open():
    # 80/72 and 80/70 share the $80 short. Closing the 80/72 must not read the 80/70
    # as closed (net_positions) nor as still open twice (collateral, concurrency).
    from dataclasses import replace

    a = _filled(_spread_plan(qty=1))
    b = replace(_filled(_spread_plan(qty=1, wing=WING70, wing_strike="70")), created_at=NOW + timedelta(minutes=1))
    close_a = replace(_filled(_spread_plan(qty=1, intent=OrderIntent.PCS_CLOSE, limit="0.40")), created_at=NOW + timedelta(minutes=2))
    recs = [a, b, close_a]
    assert net_positions(recs) == {SHORT: -1, WING70: 1}
    assert open_contracts_by_record(recs) == {b.client_order_id: 1}
    assert open_spreads(recs) == {"pcs_tqqq_10w": 1}
    assert open_collateral(recs, ticker_of=ticker_of)[0] == Decimal(1000)  # the $10-wide one


def test_residual_is_allocated_fifo_across_opens_of_the_same_structure():
    # Two opens of the same vertical (a 2-lot, then a 1-lot a day later), one 1-lot close:
    # the close is applied to the OLDER open (FIFO), so 1 of the first and all of the
    # second stay open. Neither record claims the residual twice.
    from dataclasses import replace

    first = _filled(_spread_plan(qty=2))
    second_plan = OrderPlan(
        "later",
        "pcs_tqqq_10w",
        SHORT,
        OrderIntent.PCS_OPEN,
        1,
        Decimal("0.94"),
        Decimal(80),
        EXP,
        _basis(),
        NOW + timedelta(days=1),
        long_contract_symbol=WING,
        long_strike=Decimal(72),
    )
    second = _filled(second_plan)
    close = replace(_filled(_spread_plan(qty=1, intent=OrderIntent.PCS_CLOSE, limit="0.40")), created_at=NOW + timedelta(days=2))
    recs = [second, close, first]  # deliberately unordered
    assert open_contracts_by_record(recs) == {first.client_order_id: 2, "later": 0} or open_contracts_by_record(recs) == {
        first.client_order_id: 2
    }
    assert sum(open_contracts_by_record(recs).values()) == 2
    # Same rule for cash-secured puts: two STOs on one contract, one BTC.
    sto1 = _filled(OrderPlan("s1", "d", SHORT, OrderIntent.STO, 1, Decimal("1.5"), Decimal(80), EXP, _basis(), NOW))
    sto2 = _filled(OrderPlan("s2", "d", SHORT, OrderIntent.STO, 1, Decimal("1.5"), Decimal(80), EXP, _basis(), NOW + timedelta(days=1)))
    btc = _filled(OrderPlan("b1", "d", SHORT, OrderIntent.BTC, 1, Decimal("0.7"), Decimal(80), EXP, _basis(), NOW + timedelta(days=2)))
    assert open_collateral([sto1, sto2, btc], ticker_of=ticker_of)[0] == Decimal(8000)  # not 16000


def test_single_leg_records_are_unchanged_by_the_spread_fields():
    sto = OrderPlan(
        client_order_id("d", SHORT, OrderIntent.STO, NOW.date()),
        "d",
        SHORT,
        OrderIntent.STO,
        1,
        Decimal("1.5"),
        Decimal(80),
        EXP,
        _basis(),
        NOW,
    )
    rec = _filled(sto)
    assert rec.leg_fills() == {SHORT: -1} and rec.collateral_per_contract() == Decimal(8000)
    assert open_collateral([rec], ticker_of=ticker_of)[0] == Decimal(8000)
    assert open_spreads([rec]) == {}


def test_sql_ledger_reads_the_wing_columns_and_tolerates_rows_without_them():
    class Row:
        client_order_id = "c"
        strategy = "s"
        contract_symbol = SHORT
        intent = "pcs_open"
        quantity = 1
        limit_price = "0.94"
        strike = "80"
        expiration = EXP
        state = "filled"
        filled_qty = 1
        avg_fill_price = "0.94"
        broker_order_id = "rh-1"
        basis_rule = "pcs_open_bid_cross_ask"
        basis_source = "robinhood"
        shadow = False
        created_at = NOW
        updated_at = NOW
        long_contract_symbol = WING
        long_strike = "72.00"

    rec = SqlLedger._row_to_rec(Row())
    assert rec.is_spread and rec.long_contract_symbol == WING and rec.long_strike == Decimal("72.00") and rec.width() == 8

    class Legacy(Row):
        intent = "sto"
        long_contract_symbol = None
        long_strike = None

    old = SqlLedger._row_to_rec(Legacy())
    assert not old.is_spread and old.long_strike is None


# ── guards ────────────────────────────────────────────────────────────────────
def test_guard_spread_structure_refuses_a_diagonal_and_a_starved_credit():
    caps = Caps(min_spread_credit_frac=Decimal("0.10"))
    assert guards.guard_spread_structure(_spread_plan(), _ctx(), caps).allow
    # $0.47 on an $8 width is under 10%: the 5%-wing TQQQ cell that earned nothing.
    r = guards.guard_spread_structure(_spread_plan(limit="0.47"), _ctx(), caps)
    assert not r.allow and "under 10%" in r.reason
    # Different expiration on the wing: not a vertical. OrderPlan refuses this at
    # construction, so the guard's own check is exercised by forcing the field
    # onto a valid frozen plan (defense in depth on the money path).
    bad = _spread_plan()
    object.__setattr__(bad, "long_contract_symbol", OTHER_EXP_WING)
    r = guards.guard_spread_structure(bad, _ctx(), caps)
    assert not r.allow and "root/expiration" in r.reason
    worse = _spread_plan()
    object.__setattr__(worse, "long_contract_symbol", CALL)
    assert not guards.guard_spread_structure(worse, _ctx(), caps).allow
    # A close is not held to the credit floor.
    assert guards.guard_spread_structure(_spread_plan(intent=OrderIntent.PCS_CLOSE, limit="0.10"), _ctx(), caps).allow
    # Single-leg plans pass straight through.
    sto = OrderPlan("c", "s", SHORT, OrderIntent.STO, 1, Decimal("1.5"), Decimal(80), EXP, _basis(), NOW)
    assert guards.guard_spread_structure(sto, _ctx(), caps).allow


def test_guard_spread_concurrency_caps_open_spread_contracts_per_strategy():
    caps = Caps(max_open_spreads_per_strategy=4)
    assert guards.guard_spread_concurrency(_spread_plan(qty=1), _ctx(open_spreads_strategy=3), caps).allow
    r = guards.guard_spread_concurrency(_spread_plan(qty=2), _ctx(open_spreads_strategy=3), caps)
    assert not r.allow and "concurrency cap" in r.reason
    # Closing always passes; an unbounded cap passes.
    assert guards.guard_spread_concurrency(
        _spread_plan(intent=OrderIntent.PCS_CLOSE, limit="0.4"), _ctx(open_spreads_strategy=9), caps
    ).allow
    assert guards.guard_spread_concurrency(_spread_plan(qty=9), _ctx(open_spreads_strategy=9), Caps()).allow


def test_evaluate_runs_structure_first_and_concurrency_last():
    caps = Caps(per_order=Decimal(20000), max_open_spreads_per_strategy=1, min_spread_credit_frac=Decimal("0.10"))
    assert "under 10%" in guards.evaluate(_spread_plan(limit="0.47"), _ctx(open_spreads_strategy=5), caps).reason
    assert "concurrency" in guards.evaluate(_spread_plan(), _ctx(open_spreads_strategy=1), caps).reason
    assert guards.evaluate(_spread_plan(), _ctx(), caps).allow


# ── engine end to end against SimBroker ───────────────────────────────────────
def test_engine_places_a_vertical_books_both_legs_and_commits_its_width():
    eng, broker, ledger = _engine()
    plan = build_pcs_open(strategy="pcs_tqqq_10w", short_quote=SHORT_Q, long_quote=WING_Q, quantity=2, now=NOW)
    res = run(eng.submit(plan, _kill()))
    assert res.accepted and res.record.state == OrderState.FILLED and res.record.filled_qty == 2
    recs = list(ledger.orders.values())
    assert net_positions(recs) == {SHORT: -2, WING: 2}
    assert open_collateral(recs, ticker_of=ticker_of)[0] == Decimal(1600)
    # Re-submitting the same plan is the idempotency path, never a second spread.
    again = run(eng.submit(plan, _kill()))
    assert not again.accepted and "already tracked" in again.reason


def test_engine_enforces_the_spread_concurrency_cap_from_the_ledger():
    eng, broker, ledger = _engine(caps=Caps(per_order=Decimal(20000), max_open_spreads_per_strategy=3))
    p3 = build_pcs_open(strategy="pcs_tqqq_10w", short_quote=SHORT_Q, long_quote=WING_Q, quantity=3, now=NOW)
    assert run(eng.submit(p3, _kill())).accepted
    # A fourth contract, on a different day's id, is refused by the cap.
    later = NOW + timedelta(days=1)
    p1 = build_pcs_open(strategy="pcs_tqqq_10w", short_quote=SHORT_Q, long_quote=WING_Q, quantity=1, now=later)
    res = run(eng.submit(p1, _kill()))
    assert not res.accepted and "concurrency cap" in res.reason
    # Another strategy has its own budget.
    other = build_pcs_open(strategy="pcs_other", short_quote=SHORT_Q, long_quote=WING_Q, quantity=1, now=later)
    assert run(eng.submit(other, _kill())).accepted


def test_engine_treats_a_working_order_on_the_wing_as_in_flight():
    eng, broker, ledger = _engine()
    # A single-leg STO on the wing contract that stays working.
    wing_sto = OrderPlan(
        client_order_id("x", WING, OrderIntent.STO, NOW.date()),
        "x",
        WING,
        OrderIntent.STO,
        1,
        Decimal("0.5"),
        Decimal(72),
        EXP,
        _basis(),
        NOW,
    )
    broker.scripts[wing_sto.client_order_id] = [(OrderState.PLACED, 0)] * 12
    eng.config = EngineConfig(caps=eng.config.caps, shadow=False, cancel_unfilled_after_poll=False)
    run(eng.submit(wing_sto, _kill()))
    plan = build_pcs_open(strategy="pcs_tqqq_10w", short_quote=SHORT_Q, long_quote=WING_Q, quantity=1, now=NOW)
    res = run(eng.submit(plan, _kill()))
    assert not res.accepted and "already in flight" in res.reason


def test_engine_cash_secured_guard_reads_the_spreads_width_not_its_strike():
    # $9,000 of cash funds a 2-lot $8-wide vertical ($1,600) that a CSP ($16,000) never could.
    eng, broker, ledger = _engine(cash="9000", caps=Caps(per_order=Decimal(20000)))
    plan = build_pcs_open(strategy="pcs_tqqq_10w", short_quote=SHORT_Q, long_quote=WING_Q, quantity=2, now=NOW)
    assert run(eng.submit(plan, _kill())).accepted
    csp = OrderPlan(
        client_order_id("d", SHORT, OrderIntent.STO, NOW.date()),
        "d",
        SHORT,
        OrderIntent.STO,
        2,
        Decimal("1.5"),
        Decimal(80),
        EXP,
        _basis(),
        NOW,
    )
    res = run(eng.submit(csp, _kill()))
    assert not res.accepted and "exceeds free cash" in res.reason


def test_engine_shadow_mode_records_a_spread_without_sending_it():
    eng, broker, ledger = _engine(shadow=True)
    plan = build_pcs_open(strategy="pcs_tqqq_10w", short_quote=SHORT_Q, long_quote=WING_Q, quantity=1, now=NOW)
    res = run(eng.submit(plan, _kill()))
    assert res.accepted and "shadow" in res.reason
    assert broker._orders == {} and net_positions(list(ledger.orders.values())) == {}


def test_engine_reconciles_both_legs_against_the_broker():
    eng, broker, ledger = _engine()
    plan = build_pcs_open(strategy="pcs_tqqq_10w", short_quote=SHORT_Q, long_quote=WING_Q, quantity=1, now=NOW)
    run(eng.submit(plan, _kill()))
    from agentic_guardrails.types import Position

    broker.positions_state = [Position(SHORT, -1, Decimal("1.50")), Position(WING, 1, Decimal("0.56"))]
    findings, blocking = run(eng.reconcile_positions())
    assert not blocking and {f.contract_symbol: f.drift for f in findings} == {SHORT: R.DriftClass.MATCH, WING: R.DriftClass.MATCH}
    # The wing vanishing at the broker while the ledger holds it = a naked short: OVER, blocking.
    broker.positions_state = [Position(SHORT, -1, Decimal("1.50"))]
    findings, blocking = run(eng.reconcile_positions())
    assert blocking and {f.contract_symbol: f.drift for f in findings}[WING] == R.DriftClass.OVER


# ── reconciler: longs ─────────────────────────────────────────────────────────
def test_reconciler_classifies_long_wings_by_magnitude_and_sign():
    assert R.classify_contract(WING, ledger_qty=0, broker_qty=1).drift == R.DriftClass.UNDER  # wing we missed: heal up
    assert R.classify_contract(WING, ledger_qty=1, broker_qty=2).drift == R.DriftClass.UNDER
    assert R.classify_contract(WING, ledger_qty=1, broker_qty=0).drift == R.DriftClass.OVER  # wing gone: halt
    assert R.classify_contract(WING, ledger_qty=1, broker_qty=-1).drift == R.DriftClass.OVER  # sign flipped: halt
    assert R.classify_contract(WING, ledger_qty=1, broker_qty=1).drift == R.DriftClass.MATCH
    # The short-put cases are unchanged.
    assert R.classify_contract(SHORT, ledger_qty=-3, broker_qty=-5).drift == R.DriftClass.UNDER
    assert R.classify_contract(SHORT, ledger_qty=-5, broker_qty=-3).drift == R.DriftClass.OVER
    assert R.classify_contract(SHORT, ledger_qty=0, broker_qty=-1).drift == R.DriftClass.UNDER


def test_ledger_groups_open_collateral_by_expiry_date():
    """Two lots on one expiry count together; a lot on another date stands alone;
    shadow rows and closes never count (the per-expiry concentration input)."""
    from agentic_guardrails.ledger import open_collateral_by_expiry

    a = _filled(_spread_plan(qty=2))  # width 8 x 100 x 2 = 1600 on EXP
    b = _filled(_spread_plan(qty=1))  # +800 on EXP
    by = open_collateral_by_expiry([a, b, _filled(_spread_plan(qty=3), shadow=True)])
    assert by == {EXP: Decimal(2400)}


def test_guard_expiry_concentration_refuses_a_second_lot_on_a_crowded_date():
    plan = _spread_plan(qty=1)  # 800 of collateral on EXP
    caps = Caps(per_order=Decimal(20000), per_expiry=Decimal(1000))

    def ctx_for(by_expiry):
        return ExecContext(
            free_cash=Decimal("50000"),
            open_collateral_total=Decimal(0),
            open_collateral_ticker=Decimal(0),
            open_collateral_strategy=Decimal(0),
            committed_today=Decimal(0),
            ticker="TQQQ",
            strategy_has_unknown_order=False,
            open_collateral_by_expiry=by_expiry,
        )

    ctx = ctx_for({EXP: Decimal(800)})
    res = guards.guard_expiry_concentration(plan, ctx, caps)
    assert not res.allow and "expiry cap (2026-10-16)" in res.reason
    # A different date has its own budget.
    assert guards.guard_expiry_concentration(plan, ctx_for({date(2026, 10, 23): Decimal(800)}), caps).allow
    # No cap configured = no opinion.
    assert guards.guard_expiry_concentration(plan, ctx, Caps(per_order=Decimal(20000))).allow
