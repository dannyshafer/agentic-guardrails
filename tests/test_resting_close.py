"""Resting take-profit closes (Sep 24 2026): a GTC buy-to-close at the close50 target
sits at the broker until the target trades, instead of a poll that only buys when it
happens to see the ask under target."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agentic_guardrails.broker import RhToolNames, RobinhoodMCPBroker, SimBroker
from agentic_guardrails.engine import Engine, EngineConfig
from agentic_guardrails.guards import Caps
from agentic_guardrails.killswitch import Freshness, KillState
from agentic_guardrails.ledger import InMemoryLedger
from agentic_guardrails.mcp_client import MCPToolResult
from agentic_guardrails.types import RESTING_TP_RULE, Account, OrderIntent, OrderPlan, OrderState, PriceBasis, QuoteSource

NOW = datetime(2026, 9, 24, 14, 0, tzinfo=timezone.utc)
EXP = date(2026, 10, 2)
SYM = "O:NVDL261002P00034000"


def _btc(coid, strategy, limit="0.14", rule=RESTING_TP_RULE, tif="gtc"):
    basis = PriceBasis(rule, QuoteSource.ROBINHOOD, Decimal("0.50"), Decimal("0.60"), NOW)
    return OrderPlan(coid, strategy, SYM, OrderIntent.BTC, 1, Decimal(limit), Decimal("34"), EXP, basis, NOW, time_in_force=tif)


def _kill():
    return KillState(True, Decimal(0), Decimal(500), Decimal(0), Decimal(1000), Freshness(NOW - timedelta(minutes=5)), Freshness(NOW))


def _engine(poll_attempts=2):
    broker = SimBroker(account_state=Account(free_cash=Decimal(50000), buying_power=Decimal(50000)))
    ledger = InMemoryLedger()
    eng = Engine(
        broker, ledger, EngineConfig(caps=Caps(per_order=Decimal(20000)), shadow=False, poll_attempts=poll_attempts), clock=lambda: NOW
    )
    return eng, broker, ledger


def run(coro):
    return asyncio.run(coro)


_WORKING = [(OrderState.PLACED, 0)] * 6


# ── plan validation ───────────────────────────────────────────────────────────
def test_only_a_closing_order_may_rest_gtc():
    _btc("a", "dip200_7dte")  # a BTC may rest GTC
    basis = PriceBasis("sto_bid", QuoteSource.ROBINHOOD, Decimal("0.4"), Decimal("0.5"), NOW)
    with pytest.raises(ValueError, match="closing order"):
        OrderPlan("b", "dip200_7dte", SYM, OrderIntent.STO, 1, Decimal("0.4"), Decimal("34"), EXP, basis, NOW, time_in_force="gtc")
    with pytest.raises(ValueError, match="time_in_force"):
        _btc("c", "dip200_7dte", tif="ioc")


def test_default_time_in_force_is_a_day_order():
    basis = PriceBasis("sto_bid", QuoteSource.ROBINHOOD, Decimal("0.4"), Decimal("0.5"), NOW)
    assert OrderPlan("d", "x", SYM, OrderIntent.STO, 1, Decimal("0.4"), Decimal("34"), EXP, basis, NOW).time_in_force == "gfd"


# ── engine behavior ───────────────────────────────────────────────────────────
def test_a_resting_close_is_left_working_not_cancelled_after_settle():
    eng, broker, ledger = _engine()
    broker.scripts["r1"] = list(_WORKING)
    res = run(eng.submit(_btc("r1", "dip200_7dte"), _kill()))
    assert res.accepted and res.record.state == OrderState.PLACED
    assert not broker._orders[res.record.broker_order_id].canceled_requested


def test_a_marketable_close_is_still_cancelled_when_it_does_not_fill():
    eng, broker, ledger = _engine()
    broker.scripts["m1"] = list(_WORKING)
    res = run(eng.submit(_btc("m1", "assign_guard", rule="btc_marketable_ask", tif="gfd"), _kill()))
    assert broker._orders[res.record.broker_order_id].canceled_requested


def test_another_arms_resting_close_on_the_same_contract_does_not_block():
    # NVDL Oct 2 $34P is held by two arms at 0.27 and 0.47; each rests its own 50%.
    eng, broker, ledger = _engine()
    broker.scripts["a1"] = list(_WORKING)
    broker.scripts["b1"] = list(_WORKING)
    assert run(eng.submit(_btc("a1", "dsl_skewrich_7dte", limit="0.14"), _kill())).accepted
    res = run(eng.submit(_btc("b1", "dip200_7dte", limit="0.24"), _kill()))
    assert res.accepted and res.record.state == OrderState.PLACED


def test_a_resting_close_still_blocks_the_same_arm_and_the_guards_close():
    eng, broker, ledger = _engine()
    broker.scripts["a1"] = list(_WORKING)
    assert run(eng.submit(_btc("a1", "dip200_7dte"), _kill())).accepted
    dup = run(eng.submit(_btc("a2", "dip200_7dte"), _kill()))
    assert not dup.accepted and "in flight" in dup.reason
    guard = run(eng.submit(_btc("g1", "assign_guard", limit="0.60", rule="btc_marketable_ask", tif="gfd"), _kill()))
    assert not guard.accepted and "in flight" in guard.reason


def test_cancel_resting_clears_the_contract_for_the_guard():
    eng, broker, ledger = _engine()
    broker.scripts["a1"] = [(OrderState.PLACED, 0)] * 3 + [(OrderState.CANCELED, 0)]
    run(eng.submit(_btc("a1", "dip200_7dte"), _kill()))
    cleared = run(eng.cancel_resting(SYM))
    assert [r.state for r in cleared] == [OrderState.CANCELED]
    guard = run(eng.submit(_btc("g1", "assign_guard", limit="0.60", rule="btc_marketable_ask", tif="gfd"), _kill()))
    assert guard.accepted


def test_cancel_resting_can_target_one_arm():
    eng, broker, ledger = _engine()
    broker.scripts["a1"] = list(_WORKING)
    broker.scripts["b1"] = [(OrderState.PLACED, 0)] * 3 + [(OrderState.CANCELED, 0)]
    run(eng.submit(_btc("a1", "dsl_skewrich_7dte", limit="0.14"), _kill()))
    run(eng.submit(_btc("b1", "dip200_7dte", limit="0.24"), _kill()))
    cleared = run(eng.cancel_resting(SYM, strategy="dip200_7dte"))
    assert [r.client_order_id for r in cleared] == ["b1"]
    assert not broker._orders[ledger.orders["a1"].broker_order_id].canceled_requested


def test_cancel_resting_leaves_other_orders_alone():
    eng, broker, ledger = _engine()
    assert run(eng.cancel_resting(SYM)) == []


def test_poll_working_books_a_resting_fill():
    eng, broker, ledger = _engine()
    broker.scripts["a1"] = [(OrderState.PLACED, 0)] * 3 + [(OrderState.FILLED, 1)]
    rec = run(eng.submit(_btc("a1", "dip200_7dte"), _kill())).record
    assert rec.state == OrderState.PLACED
    (rec,) = run(eng.poll_working())
    assert rec.state == OrderState.FILLED and rec.filled_qty == 1 and ledger.orders["a1"].state == OrderState.FILLED


# ── broker body + tick ────────────────────────────────────────────────────────
class _Fake:
    def __init__(self, by_tool):
        self.by_tool, self.calls = by_tool, []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return self.by_tool[name]


def _ok(structured):
    return MCPToolResult(is_error=False, structured=structured, content=[], raw={})


_INSTR = {
    "data": {
        "instruments": [
            {
                "id": "iid1",
                "strike_price": "34.0000",
                "type": "put",
                "expiration_date": "2026-10-02",
                "chain_symbol": "NVDL",
                "min_ticks": {"above_tick": "0.10", "below_tick": "0.05", "cutoff_price": "3.00"},
            }
        ]
    }
}


_TOOLS = RhToolNames(
    quote="get_option_quotes",
    place="place_option_order",
    cancel="cancel_option_order",
    review="review_option_order",
    order_status="get_option_orders",
    positions="get_option_positions",
    account="get_accounts",
    portfolio="get_portfolio",
    instruments="get_option_instruments",
)


def _mcp(**extra):
    fake = _Fake({"get_option_instruments": _ok(_INSTR), **extra})
    return RobinhoodMCPBroker(fake, _TOOLS, account_id="AG-1", clock=lambda: NOW), fake


@pytest.mark.asyncio
async def test_the_order_body_carries_the_plans_time_in_force():
    b, fake = _mcp(review_option_order=_ok({"data": {}}))
    assert await b.review(_btc("a1", "dip200_7dte", tif="gtc"))
    assert await b.review(_btc("a2", "dip200_7dte", tif="gfd"))
    tifs = [args["time_in_force"] for name, args in fake.calls if name == "review_option_order"]
    assert tifs == ["gtc", "gfd"]


@pytest.mark.asyncio
async def test_min_tick_reads_the_contracts_own_increment():
    b, _ = _mcp()
    assert await b.min_tick(SYM, Decimal("0.15")) == Decimal("0.05")
    assert await b.min_tick(SYM, Decimal("3.50")) == Decimal("0.10")
