"""The smallest honest open loop: one signal in, one guarded order out, against no account.

    python examples/open_loop.py

Swap `DryRunBroker` for a venue adapter and `SqliteLedger`/`JsonFileHealthStore` paths for real
ones and the same code runs live; the config stays dark (enabled=False, shadow=True) until you
flip it on purpose. What this shows is the ORDER of things: health first (reconcile, self-test),
then the kill state from real freshness and real realized P&L, then the vetting floors, then one
`engine.submit`, which runs every guard before anything reaches the broker.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal

from agentic_guardrails import Engine, GuardrailConfig, InMemoryHealthStore, InMemoryLedger
from agentic_guardrails.brokers import DryRunBroker
from agentic_guardrails.health import kill_state_freshness, record_reconcile, run_selftest_cycle
from agentic_guardrails.ids import client_order_id
from agentic_guardrails.runtime import open_kill_state, open_limit, open_sanity_reason, realized_pnl_today
from agentic_guardrails.types import OrderIntent, OrderPlan, PriceBasis, Quote, QuoteSource

SHORT = "O:TQQQ261120P00080000"  # a signal: sell this put


async def quotes(sym: str) -> Quote:
    return Quote(sym, Decimal("1.00"), Decimal("1.10"), QuoteSource.ROBINHOOD, datetime.now(timezone.utc), delta=Decimal("-0.25"))


async def main() -> None:
    cfg = GuardrailConfig(enabled=True, shadow=False)  # live against the dry-run broker only
    broker = DryRunBroker(quote_feed=quotes, cash=Decimal(20000))
    ledger, health = InMemoryLedger(), InMemoryHealthStore()
    now = datetime.now(timezone.utc)
    today = now.date()

    # 1. Health: the dead-man gate halts opens unless both of these are fresh and passing.
    await record_reconcile(health, findings_blocking=False, now=now)
    await run_selftest_cycle(health, broker, ledger, SHORT, now=now, force=True)
    rec_fresh, self_fresh = await kill_state_freshness(health)

    # 2. Caps from live cash; engine in the config's mode.
    account = await broker.account()
    engine = Engine(broker, ledger, cfg.engine_config(cfg.caps_for(account.free_cash)), clock=lambda: now)

    # 3. The kill state from REAL inputs: today's realized P&L paired from the ledger, never a sentinel.
    by_strategy, total, ok = realized_pnl_today(await ledger.all_orders(), today)
    if not ok:
        print("a close today could not be attributed; standing down")
        return
    kill = open_kill_state(cfg, rec_fresh, self_fresh, by_strategy.get("demo", Decimal(0)), total)

    # 4. Price and vet the pick against the live quote.
    q = await broker.quote(SHORT)
    limit = open_limit(recorded_bid=Decimal("0.95"), live_bid=q.bid, live_ask=q.ask)
    expiration = date(2026, 11, 20)
    reason = open_sanity_reason(
        cfg=cfg,
        ticker="TQQQ",
        strike=Decimal(80),
        expiration=expiration,
        limit=limit,
        spot=Decimal(95),
        today=today,
        records=await ledger.all_orders(),
        equity=account.free_cash,
    )
    if reason:
        print("stand down:", reason)
        return

    # 5. One order through every guard.
    plan = OrderPlan(
        client_order_id=client_order_id("demo", SHORT, OrderIntent.STO, today),
        strategy="demo",
        contract_symbol=SHORT,
        intent=OrderIntent.STO,
        quantity=1,
        limit_price=limit,
        strike=Decimal(80),
        expiration=expiration,
        basis=PriceBasis("sto_bid", q.source, q.bid, q.ask, now),
        created_at=now,
    )
    res = await engine.submit(plan, kill)
    print("accepted" if res.accepted else "refused", res.reason, res.record.state.value if res.record else "")
    print("dry-run broker saw:", broker.log)
    print("positions:", await broker.positions(), "cash:", (await broker.account()).free_cash)


if __name__ == "__main__":
    asyncio.run(main())
