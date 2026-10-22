"""Engine + ledger + self-test lifecycles against SimBroker and the in-memory ledger (Phase 1)."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from agentic_guardrails import selftest
from agentic_guardrails.broker import Fault, SimBroker
from agentic_guardrails.engine import Engine, EngineConfig, ticker_of
from agentic_guardrails.guards import Caps
from agentic_guardrails.ids import client_order_id
from agentic_guardrails.killswitch import Freshness, KillState
from agentic_guardrails.ledger import InMemoryLedger, net_positions, open_collateral
from agentic_guardrails.types import Account, OrderIntent, OrderPlan, OrderState, Position, PriceBasis, Quote, QuoteSource

NOW = datetime(2026, 9, 2, 14, 30, tzinfo=timezone.utc)
EXP = date(2026, 10, 16)
SYM = "O:TQQQ261016P00080000"


def _plan(intent=OrderIntent.STO, qty=1, limit="1.50", strike="80", strategy="pcs_skewivz_10w", sym=SYM):
    basis = PriceBasis("sto_bid", QuoteSource.ROBINHOOD, Decimal("1.50"), Decimal("1.60"), NOW)
    return OrderPlan(
        client_order_id(strategy, sym, intent, NOW.date()), strategy, sym, intent, qty, Decimal(limit), Decimal(strike), EXP, basis, NOW
    )


def _kill(enabled=True):
    return KillState(
        enabled,
        Decimal(0),
        Decimal(500),
        Decimal(0),
        Decimal(1000),
        Freshness(NOW - timedelta(minutes=5)),
        Freshness(NOW - timedelta(hours=1)),
    )


def _engine(shadow=False, cash="100000", caps=None):
    broker = SimBroker(account_state=Account(free_cash=Decimal(cash), buying_power=Decimal(cash)))
    broker.quotes[SYM] = Quote(SYM, Decimal("1.50"), Decimal("1.60"), QuoteSource.ROBINHOOD, NOW)
    ledger = InMemoryLedger()
    eng = Engine(broker, ledger, EngineConfig(caps=caps or Caps(per_order=Decimal(20000)), shadow=shadow), clock=lambda: NOW)
    return eng, broker, ledger


def run(coro):
    return asyncio.run(coro)


def test_ticker_of_parses_occ():
    assert ticker_of("O:TQQQ261016P00080000") == "TQQQ"
    assert ticker_of("SPY261016P00500000") == "SPY"


def test_shadow_mode_records_but_never_places():
    eng, broker, ledger = _engine(shadow=True)
    res = run(eng.submit(_plan(), _kill()))
    assert res.accepted and "shadow" in res.reason
    assert broker._orders == {}
    rec = ledger.orders[_plan().client_order_id]
    assert rec.shadow and rec.state == OrderState.REVIEWED and rec.filled_qty == 0
    assert net_positions([rec]) == {}  # shadow rows never become positions


def test_live_happy_path_fills_and_books_position():
    eng, broker, ledger = _engine()
    res = run(eng.submit(_plan(qty=2), _kill()))
    assert res.accepted and res.record.state == OrderState.FILLED and res.record.filled_qty == 2
    assert net_positions(list(ledger.orders.values())) == {SYM: -2}
    total, by_t, by_s = open_collateral(list(ledger.orders.values()), ticker_of=ticker_of)
    assert total == Decimal(16000) and by_t["TQQQ"] == Decimal(16000) and by_s["pcs_skewivz_10w"] == Decimal(16000)
    states = [(e.from_state, e.to_state) for e in ledger.events]
    assert states[0] == (OrderState.PLANNED, OrderState.REVIEWED)
    assert states[-1][1] == OrderState.FILLED


def test_kill_switch_and_guards_block_before_anything_is_sent():
    eng, broker, ledger = _engine()
    res = run(eng.submit(_plan(), _kill(enabled=False)))
    assert not res.accepted and "disabled" in res.reason and ledger.orders == {}
    eng2, broker2, ledger2 = _engine(cash="100")  # cannot secure an $8,000 short
    res2 = run(eng2.submit(_plan(), _kill()))
    assert not res2.accepted and ledger2.orders == {} and broker2._orders == {}


def test_slow_fill_after_cancel_keeps_high_water_mark():
    eng, broker, ledger = _engine()
    p = _plan(qty=4)
    # Never reaches terminal inside the poll budget; the post-cancel re-read shows the rest filled.
    # Reads consumed: 3 status polls, the cancel ack, one post-cancel re-read. The order keeps
    # filling through the cancel; the final read is the one that books the full 4.
    broker.scripts[p.client_order_id] = [
        (OrderState.PLACED, 0),
        (OrderState.PARTIAL, 1),
        (OrderState.PARTIAL, 1),
        (OrderState.PARTIAL, 1),
        (OrderState.PARTIAL, 2),
        (OrderState.CANCELED, 4),
    ]
    eng.config = EngineConfig(caps=Caps(per_order=Decimal(40000)), shadow=False, poll_attempts=3)
    res = run(eng.submit(p, _kill()))
    assert res.record.state == OrderState.CANCELED and res.record.filled_qty == 4
    assert net_positions(list(ledger.orders.values())) == {SYM: -4}


def test_place_timeout_goes_unknown_and_blocks_next_open_until_polled():
    eng, broker, ledger = _engine()
    broker.faults["place"].append(Fault.TIMEOUT)
    p = _plan()
    res = run(eng.submit(p, _kill()))
    assert res.accepted and ledger.orders[p.client_order_id].state == OrderState.UNKNOWN
    # A second open for the same strategy is blocked while UNKNOWN stands.
    other = _plan(sym="O:TQQQ261016P00075000", strike="75")
    res2 = run(eng.submit(other, _kill()))
    assert not res2.accepted and "unknown" in res2.reason.lower()
    # poll_working ADOPTS it by our client_order_id (the order landed under it)
    # and drives it to terminal, so an ambiguous placement never sits UNKNOWN
    # forever (docs plan 4.4; the Devin-review fix on PR #476).
    recs = run(eng.poll_working())
    assert recs[0].broker_order_id is not None and recs[0].state == OrderState.FILLED


def test_reconcile_flags_over_and_heals_nothing_by_itself():
    eng, broker, ledger = _engine()
    run(eng.submit(_plan(qty=2), _kill()))
    broker.positions_state = [Position(SYM, -1, Decimal("1.50"))]  # broker shows fewer short than the ledger
    findings, blocking = run(eng.reconcile_positions())
    assert blocking and any(f.drift.value == "over" for f in findings)
    broker.positions_state = [Position(SYM, -3, Decimal("1.50"))]
    findings, blocking = run(eng.reconcile_positions())
    assert not blocking and any(f.drift.value == "under" for f in findings)


def test_selftest_pass_and_fail_paths():
    eng, broker, ledger = _engine()
    probe = run(selftest.run_probe(broker, ledger, SYM, NOW))
    assert selftest.evaluate(probe).passed
    broker.faults["quote"].append(Fault.AUTH)
    probe = run(selftest.run_probe(broker, ledger, SYM, NOW))
    res = selftest.evaluate(probe)
    assert not res.passed and any("quote" in r for r in res.reasons)
    bad = selftest.Probe(True, Decimal(1), True, True, Decimal("2.0"), Decimal("1.0"), 10.0, 0, 0)
    assert "malformed" in selftest.evaluate(bad).reasons[0]
    stale = selftest.Probe(True, Decimal(1), True, True, Decimal("1.0"), Decimal("1.1"), 5000.0, 0, 0)
    assert "stale" in selftest.evaluate(stale).reasons[0]
    over = selftest.Probe(True, Decimal(1), True, True, Decimal("1.0"), Decimal("1.1"), 1.0, 1, 0)
    assert not selftest.evaluate(over).passed


# ── guard inputs actually reach the guards (review of PR #450) ────────────────
# The first cut added a factor-concentration guard and an earnings guard, but
# Engine._context never populated their inputs, so both defaulted to ALLOW on
# every opening order: guards that read as protection while being no-ops. These
# assert at the ENGINE level, not the guard level, because guard unit tests
# passed the whole time the wiring was missing.
NVDL_SYM = "O:NVDL260918P00030000"


def _nvdl_plan(qty=1, strike="30"):
    basis = PriceBasis("sto_bid", QuoteSource.ROBINHOOD, Decimal("0.65"), Decimal("0.70"), NOW)
    return OrderPlan(
        client_order_id("dsl_skewrich_25d", NVDL_SYM, OrderIntent.STO, NOW.date()),
        "dsl_skewrich_25d",
        NVDL_SYM,
        OrderIntent.STO,
        qty,
        Decimal("0.65"),
        Decimal(strike),
        EXP,
        basis,
        NOW,
    )


def _engine_for(sym, *, caps=None, earnings=None, cash="100000"):
    broker = SimBroker(account_state=Account(free_cash=Decimal(cash), buying_power=Decimal(cash)))
    broker.quotes[sym] = Quote(sym, Decimal("0.65"), Decimal("0.70"), QuoteSource.ROBINHOOD, NOW)
    ledger = InMemoryLedger()
    cfg = EngineConfig(
        caps=caps or Caps(per_order=Decimal(20000)),
        shadow=False,
        earnings_provider=earnings,
        poll_delay_s=0.0,
    )
    return Engine(broker, ledger, cfg, clock=lambda: NOW), broker, ledger


def test_engine_blocks_a_single_name_open_when_no_earnings_provider_is_configured():
    """Fail closed: a single-name wrapper with no earnings feed must not open."""
    eng, broker, _ = _engine_for(NVDL_SYM)
    res = run(eng.submit(_nvdl_plan(), _kill()))
    assert not res.accepted
    assert "earnings" in res.reason.lower()
    assert broker._orders == {}  # nothing was sent


def test_engine_allows_an_index_wrapper_without_an_earnings_provider():
    """An index has no scheduled event, so a missing lookup is not a failure.
    Distinguishing this from the single-name case is what keeps the guard from
    being either a no-op or a blanket stand-down."""
    eng, _, _ = _engine_for(SYM)
    assert run(eng.submit(_plan(), _kill())).accepted


def test_engine_blocks_an_open_whose_expiry_spans_earnings():
    async def provider(_ticker):
        return date(2026, 9, 10)  # before the Sep 18 expiry

    eng, broker, _ = _engine_for(NVDL_SYM, earnings=provider)
    res = run(eng.submit(_nvdl_plan(), _kill()))
    assert not res.accepted and "earnings 2026-09-10" in res.reason
    assert broker._orders == {}


def test_engine_allows_an_open_when_earnings_fall_after_expiry():
    async def provider(_ticker):
        return date(2026, 11, 20)

    eng, _, _ = _engine_for(NVDL_SYM, earnings=provider)
    assert run(eng.submit(_nvdl_plan(), _kill())).accepted


def test_a_raising_earnings_provider_fails_closed_rather_than_crashing():
    async def provider(_ticker):
        raise RuntimeError("earnings feed down")

    eng, _, _ = _engine_for(NVDL_SYM, earnings=provider)
    res = run(eng.submit(_nvdl_plan(), _kill()))
    assert not res.accepted and "fail closed" in res.reason


def test_engine_derives_factor_collateral_from_existing_ledger_exposure():
    """Existing TQQQ exposure must count against a QLD open: same factor (NDX).

    This is the wiring the review found missing: without open_collateral_by_factor
    the guard saw an empty map and allowed everything.
    """
    caps = Caps(per_order=Decimal(20000), per_factor=Decimal("9000"))
    eng, broker, _ = _engine_for(SYM, caps=caps)
    # Fill one TQQQ put: 80 strike x 100 = $8,000 of NDX collateral.
    assert run(eng.submit(_plan(qty=1), _kill())).accepted
    qld_sym = "O:QLD261016P00080000"
    broker.quotes[qld_sym] = Quote(qld_sym, Decimal("1.50"), Decimal("1.60"), QuoteSource.ROBINHOOD, NOW)
    basis = PriceBasis("sto_bid", QuoteSource.ROBINHOOD, Decimal("1.50"), Decimal("1.60"), NOW)
    qld = OrderPlan(
        client_order_id("pcs_skewivz_10w", qld_sym, OrderIntent.STO, NOW.date()),
        "pcs_skewivz_10w",
        qld_sym,
        OrderIntent.STO,
        1,
        Decimal("1.50"),
        Decimal("80"),
        EXP,
        basis,
        NOW,
    )
    res = run(eng.submit(qld, _kill()))
    assert not res.accepted
    assert "factor cap (NDX)" in res.reason


def test_engine_enforces_the_portfolio_cap_across_unrelated_factors():
    """TQQQ (NDX) exposure must count against an NVDL (NVDA) open under
    per_portfolio, which is the bound per_factor deliberately cannot provide."""
    caps = Caps(per_order=Decimal(20000), per_factor=Decimal("50000"), per_portfolio=Decimal("10000"))
    eng, broker, _ = _engine_for(SYM, caps=caps)
    assert run(eng.submit(_plan(qty=1), _kill())).accepted  # $8,000 booked
    broker.quotes[NVDL_SYM] = Quote(NVDL_SYM, Decimal("0.65"), Decimal("0.70"), QuoteSource.ROBINHOOD, NOW)

    async def provider(_ticker):
        return date(2026, 11, 20)

    eng.config = EngineConfig(caps=caps, shadow=False, earnings_provider=provider, poll_delay_s=0.0)
    res = run(eng.submit(_nvdl_plan(qty=1, strike="30"), _kill()))  # needs $3,000, total $11,000
    assert not res.accepted
    assert "portfolio cap" in res.reason


def test_the_earnings_provider_is_asked_about_the_UNDERLIER_not_the_wrapper():
    """NVDL is an ETF with no earnings of its own. Querying the wrapper returns
    None, the lookup reads as a successful "no event", and the guard permits a
    contract spanning NVDA's report: the guard silently defeated for exactly the
    names it protects (found in review of PR #452)."""
    asked = []

    async def provider(symbol):
        asked.append(symbol)
        # Mimics the real feed: only the underlier has a date.
        return date(2026, 9, 10) if symbol == "NVDA" else None

    eng, broker, _ = _engine_for(NVDL_SYM, earnings=provider)
    res = run(eng.submit(_nvdl_plan(), _kill()))
    assert asked[-1] == "NVDA", f"provider was asked about {asked}, not the underlier"
    assert not res.accepted and "earnings 2026-09-10" in res.reason
    assert broker._orders == {}


def test_an_index_wrapper_is_asked_about_its_index_factor():
    asked = []

    async def provider(symbol):
        asked.append(symbol)
        return None

    eng, _, _ = _engine_for(SYM, earnings=provider)
    assert run(eng.submit(_plan(), _kill())).accepted
    assert asked == ["TQQQ", "NDX"]  # ticker first (basket check), then its index factor


def test_a_single_name_with_no_date_from_the_provider_fails_closed():
    """A company always has a next report, so None means the feed could not tell
    us. Without this, the NVDL bug generalizes: any ticker missing from FACTOR_OF
    is queried by its own ETF symbol, returns None, and silently loses the guard."""

    async def provider(_symbol):
        return None

    eng, broker, _ = _engine_for(NVDL_SYM, earnings=provider)
    res = run(eng.submit(_nvdl_plan(), _kill()))
    assert not res.accepted and "fail closed" in res.reason
    assert broker._orders == {}


def test_an_index_with_no_date_is_still_allowed():
    """Only an index or basket legitimately has no earnings date."""

    async def provider(_symbol):
        return None

    eng, _, _ = _engine_for(SYM, earnings=provider)
    assert run(eng.submit(_plan(), _kill())).accepted


def test_a_basket_wrapper_is_asked_by_its_own_ticker_before_its_index_factor():
    """TECL maps to NDX in FACTOR_OF, which has no event, but TECL carries its
    holdings' reports. The engine must ask about the traded ticker first so the
    provider can answer with the basket's earliest holding report (PR #571)."""
    sym = "O:TECL261016P00080000"
    asked = []

    async def provider(symbol):
        asked.append(symbol)
        return date(2026, 9, 10) if symbol == "TECL" else None

    eng, broker, _ = _engine_for(sym, earnings=provider)
    basis = PriceBasis("sto_bid", QuoteSource.ROBINHOOD, Decimal("0.65"), Decimal("0.70"), NOW)
    plan = OrderPlan(
        client_order_id("arm", sym, OrderIntent.STO, NOW.date()),
        "arm",
        sym,
        OrderIntent.STO,
        1,
        Decimal("0.65"),
        Decimal("80"),
        EXP,
        basis,
        NOW,
    )
    res = run(eng.submit(plan, _kill()))
    assert asked[0] == "TECL"
    assert not res.accepted and "earnings 2026-09-10" in res.reason
    assert broker._orders == {}


def test_engine_derives_expiry_collateral_from_existing_ledger_exposure():
    """Existing Oct-16 exposure must count against another Oct-16 open (any ticker),
    while an Oct-23 open on the same ticker has its own budget."""
    caps = Caps(per_order=Decimal(20000), per_expiry=Decimal("9000"))
    eng, broker, _ = _engine_for(SYM, caps=caps)
    assert run(eng.submit(_plan(qty=1), _kill())).accepted  # $8,000 on 2026-10-16

    def plan_for(sym, exp):
        broker.quotes[sym] = Quote(sym, Decimal("1.50"), Decimal("1.60"), QuoteSource.ROBINHOOD, NOW)
        basis = PriceBasis("sto_bid", QuoteSource.ROBINHOOD, Decimal("1.50"), Decimal("1.60"), NOW)
        return OrderPlan(
            client_order_id("pcs_skewivz_10w", sym, OrderIntent.STO, NOW.date()),
            "pcs_skewivz_10w",
            sym,
            OrderIntent.STO,
            1,
            Decimal("1.50"),
            Decimal("80"),
            exp,
            basis,
            NOW,
        )

    res = run(eng.submit(plan_for("O:QLD261016P00080000", EXP), _kill()))
    assert not res.accepted and "expiry cap (2026-10-16)" in res.reason
    res = run(eng.submit(plan_for("O:QLD261023P00080000", date(2026, 10, 23)), _kill()))
    assert res.accepted, res.reason


# ── in-flight collateral (Sep 23 2026 same-tick overshoot) ──────────────────


def _qld_plan():
    qld_sym = "O:QLD261016P00080000"
    basis = PriceBasis("sto_bid", QuoteSource.ROBINHOOD, Decimal("1.50"), Decimal("1.60"), NOW)
    return qld_sym, OrderPlan(
        client_order_id("pcs_skewivz_10w", qld_sym, OrderIntent.STO, NOW.date()),
        "pcs_skewivz_10w",
        qld_sym,
        OrderIntent.STO,
        1,
        Decimal("1.50"),
        Decimal("80"),
        EXP,
        basis,
        NOW,
    )


def _working(plan, state, filled=0, shadow=False):
    from dataclasses import replace

    from agentic_guardrails.ledger import OrderRecord

    return replace(OrderRecord.from_plan(plan, shadow=shadow, now=NOW), state=state, filled_qty=filled)


def test_an_unsettled_open_counts_against_the_factor_cap():
    """A TQQQ open that is PLACED but not yet filled must still commit its
    collateral, so a QLD open in the same tick hits the NDX factor cap. Before
    the fix the caps read filled positions only and the second order overshot."""
    caps = Caps(per_order=Decimal(20000), per_factor=Decimal("9000"))
    eng, broker, ledger = _engine_for(SYM, caps=caps)
    run(ledger.upsert(_working(_plan(qty=1), OrderState.PLACED)))
    qld_sym, qld = _qld_plan()
    broker.quotes[qld_sym] = Quote(qld_sym, Decimal("1.50"), Decimal("1.60"), QuoteSource.ROBINHOOD, NOW)
    res = run(eng.submit(qld, _kill()))
    assert not res.accepted
    assert "factor cap (NDX)" in res.reason


def test_an_unknown_open_counts_but_a_canceled_or_shadow_one_does_not():
    caps = Caps(per_order=Decimal(20000), per_factor=Decimal("9000"))
    for state, shadow, blocked in (
        (OrderState.UNKNOWN, False, True),
        (OrderState.CANCELED, False, False),
        (OrderState.PLACED, True, False),
    ):
        eng, broker, ledger = _engine_for(SYM, caps=caps)
        run(ledger.upsert(_working(_plan(qty=1), state, shadow=shadow)))
        qld_sym, qld = _qld_plan()
        broker.quotes[qld_sym] = Quote(qld_sym, Decimal("1.50"), Decimal("1.60"), QuoteSource.ROBINHOOD, NOW)
        res = run(eng.submit(qld, _kill()))
        assert res.accepted is (not blocked), (state, shadow, res.reason)


def test_inflight_remainders_count_only_the_unfilled_part_and_skip_closes_and_self():
    from agentic_guardrails.ledger import inflight_open_remainders

    partial = _working(_plan(qty=3), OrderState.PARTIAL, filled=1)
    close = _working(_plan(intent=OrderIntent.BTC, qty=2, strategy="x"), OrderState.PLACED)
    out = inflight_open_remainders([partial, close])
    assert [(r.client_order_id, n) for r, n in out] == [(partial.client_order_id, 2)]
    assert inflight_open_remainders([partial], exclude=partial.client_order_id) == []


def test_an_interrupted_reviewed_open_does_not_reserve_collateral():
    """A REVIEWED row another submission can see never reached the broker
    (submit holds the lock through settle), and poll_working never resolves it,
    so it must not reserve collateral forever (Devin, PR #629)."""
    caps = Caps(per_order=Decimal(20000), per_factor=Decimal("9000"))
    eng, broker, ledger = _engine_for(SYM, caps=caps)
    run(ledger.upsert(_working(_plan(qty=1), OrderState.REVIEWED)))
    qld_sym, qld = _qld_plan()
    broker.quotes[qld_sym] = Quote(qld_sym, Decimal("1.50"), Decimal("1.60"), QuoteSource.ROBINHOOD, NOW)
    assert run(eng.submit(qld, _kill())).accepted
