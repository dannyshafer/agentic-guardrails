"""DryRunBroker: the paper adapter. Records every verb it is asked, fills by policy, never
touches a venue. Quotes come from a feed callable you supply (a quote archive, a data vendor,
or a dict), so the same engine, ledger and guards run end to end against no account.

Fill policy: `fill="limit"` fills the whole order at the limit on the first status read (the
paper-conservative basis for a sell: you only ever fill at your own price); `fill="none"` rests
the order forever (exercise the cancel and UNKNOWN paths); `fill="marketable"` fills a buy at
or below the ask and a sell at or above the bid from the current quote, else rests. Idempotent
by client order id, like a real venue with a reference id.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Awaitable, Callable

from agentic_guardrails.broker import BrokerMalformed, BrokerRejected
from agentic_guardrails.types import Account, BrokerOrder, OrderIntent, OrderPlan, OrderState, Position, Quote

_BUYS = (OrderIntent.BTC, OrderIntent.PCS_CLOSE)


@dataclass
class _Order:
    plan: OrderPlan
    broker_order_id: str
    state: OrderState = OrderState.PLACED
    filled: int = 0
    price: Decimal | None = None


@dataclass
class DryRunBroker:
    quote_feed: Callable[[str], Awaitable[Quote]] | None = None
    cash: Decimal = Decimal(25000)
    fill: str = "limit"  # limit | none | marketable
    reject: Callable[[OrderPlan], str | None] | None = None  # a policy that refuses, for tests
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    orders: dict[str, _Order] = field(default_factory=dict)
    by_client: dict[str, str] = field(default_factory=dict)
    log: list[tuple[str, dict]] = field(default_factory=list)
    _positions: dict[str, Position] = field(default_factory=dict)
    _seq: int = 0

    async def quote(self, contract_symbol: str) -> Quote:
        if self.quote_feed is None:
            raise BrokerMalformed(f"dry run has no quote feed for {contract_symbol}")
        return await self.quote_feed(contract_symbol)

    async def review(self, plan: OrderPlan) -> bool:
        self.log.append(("review", {"client_order_id": plan.client_order_id}))
        return not (self.reject and self.reject(plan))

    async def place(self, plan: OrderPlan) -> BrokerOrder:
        self.log.append(("place", {"client_order_id": plan.client_order_id, "limit": str(plan.limit_price), "qty": plan.quantity}))
        if self.reject and (why := self.reject(plan)):
            raise BrokerRejected(f"dry run rejected: {why}")
        existing = self.by_client.get(plan.client_order_id)
        if existing is not None:
            return self._view(self.orders[existing])
        self._seq += 1
        o = _Order(plan, f"dry-{self._seq}")
        self.orders[o.broker_order_id] = o
        self.by_client[plan.client_order_id] = o.broker_order_id
        return self._view(o)

    async def cancel(self, broker_order_id: str) -> BrokerOrder:
        o = self._get(broker_order_id)
        self.log.append(("cancel", {"broker_order_id": broker_order_id}))
        if o.state in (OrderState.PLACED, OrderState.PARTIAL):
            o.state = OrderState.CANCELED
        return self._view(o)

    async def status(self, broker_order_id: str) -> BrokerOrder:
        o = self._get(broker_order_id)
        if o.state == OrderState.PLACED:
            await self._maybe_fill(o)
        return self._view(o)

    async def positions(self) -> list[Position]:
        return [p for p in self._positions.values() if p.quantity != 0]

    async def account(self) -> Account:
        return Account(free_cash=self.cash, buying_power=self.cash)

    async def find_broker_order_id(self, client_order_id: str) -> str | None:
        return self.by_client.get(client_order_id)

    async def min_tick(self, contract_symbol: str, price: Decimal) -> Decimal:
        return Decimal("0.01")

    def _get(self, boid: str) -> _Order:
        o = self.orders.get(boid)
        if o is None:
            raise BrokerMalformed(f"no dry-run order {boid}")
        return o

    async def _maybe_fill(self, o: _Order) -> None:
        plan = o.plan
        if self.fill == "none":
            return
        px = plan.limit_price
        if self.fill == "marketable":
            q = await self.quote(plan.contract_symbol)
            buying = plan.intent in _BUYS
            if buying and plan.limit_price < q.ask:
                return
            if not buying and plan.limit_price > q.bid:
                return
            px = q.ask if buying else q.bid
        o.state, o.filled, o.price = OrderState.FILLED, plan.quantity, px
        self._book(plan, px)

    def _book(self, plan: OrderPlan, px: Decimal) -> None:
        sign = 1 if plan.intent in _BUYS else -1
        legs = [(plan.contract_symbol, sign, plan.strike)]
        if plan.long_contract_symbol:
            legs.append((plan.long_contract_symbol, -sign, plan.long_strike or Decimal(0)))
        for sym, s, _ in legs:
            prev = self._positions.get(sym)
            qty = (prev.quantity if prev else 0) + s * plan.quantity
            self._positions[sym] = Position(contract_symbol=sym, quantity=qty, avg_price=px)
        self.cash += Decimal(100) * Decimal(plan.quantity) * px * Decimal(-sign)

    @staticmethod
    def _view(o: _Order) -> BrokerOrder:
        return BrokerOrder(
            broker_order_id=o.broker_order_id, state=o.state, filled_qty=o.filled, avg_fill_price=o.price, raw={"dry_run": True}
        )
