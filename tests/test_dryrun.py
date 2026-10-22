"""DryRunBroker: the whole engine runs against no account, fills by policy, idempotent by id."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal

from agentic_guardrails.broker import BrokerRejected
from agentic_guardrails.brokers import DryRunBroker
from agentic_guardrails.config import GuardrailConfig
from agentic_guardrails.engine import Engine
from agentic_guardrails.ledger import InMemoryLedger
from agentic_guardrails.runtime import guard_kill_state
from agentic_guardrails.types import OrderIntent, OrderPlan, OrderState, PriceBasis, Quote, QuoteSource

NOW = datetime(2026, 10, 22, 15, 0, tzinfo=timezone.utc)
PUT = "O:TQQQ261218P00080000"


async def feed(sym):
    return Quote(sym, Decimal("1.00"), Decimal("1.10"), QuoteSource.ROBINHOOD, NOW)


def _plan(coid="sat_1", intent=OrderIntent.STO, limit="1.00"):
    return OrderPlan(
        coid,
        "demo",
        PUT,
        intent,
        1,
        Decimal(limit),
        Decimal(80),
        date(2026, 12, 18),
        PriceBasis("bid", QuoteSource.ROBINHOOD, Decimal(1), Decimal("1.1"), NOW),
        NOW,
    )


def test_engine_end_to_end_fills_at_limit_books_position_and_cash():
    broker = DryRunBroker(quote_feed=feed, cash=Decimal(20000))
    cfg = GuardrailConfig(enabled=True, shadow=False)
    engine = Engine(broker, InMemoryLedger(), cfg.engine_config(cfg.caps_for(Decimal(20000))), clock=lambda: NOW)
    res = asyncio.run(engine.submit(_plan(), guard_kill_state(NOW)))
    assert res.accepted and res.record.state == OrderState.FILLED and res.record.avg_fill_price == Decimal("1.00")
    assert asyncio.run(broker.positions())[0].quantity == -1
    assert asyncio.run(broker.account()).free_cash == Decimal(20100)
    assert [v for v, _ in broker.log] == ["review", "place"]
    again = asyncio.run(engine.submit(_plan(), guard_kill_state(NOW)))
    assert not again.accepted  # idempotent at the engine; the broker also dedups by id
    assert asyncio.run(broker.find_broker_order_id("sat_1")) == "dry-1"


def test_fill_policies():
    none = DryRunBroker(quote_feed=feed, fill="none")
    o = asyncio.run(none.place(_plan()))
    assert asyncio.run(none.status(o.broker_order_id)).state == OrderState.PLACED
    assert asyncio.run(none.cancel(o.broker_order_id)).state == OrderState.CANCELED
    mk = DryRunBroker(quote_feed=feed, fill="marketable")
    o = asyncio.run(mk.place(_plan(limit="1.50")))  # a sell above the bid rests
    assert asyncio.run(mk.status(o.broker_order_id)).state == OrderState.PLACED
    o = asyncio.run(mk.place(_plan("sat_2", limit="0.90")))  # at or under the bid fills AT the bid
    s = asyncio.run(mk.status(o.broker_order_id))
    assert s.state == OrderState.FILLED and s.avg_fill_price == Decimal("1.00")


def test_reject_policy_raises_the_terminal_reject():
    b = DryRunBroker(quote_feed=feed, reject=lambda p: "no" if p.quantity else None)
    try:
        asyncio.run(b.place(_plan()))
    except BrokerRejected:
        return
    raise AssertionError("expected BrokerRejected")
