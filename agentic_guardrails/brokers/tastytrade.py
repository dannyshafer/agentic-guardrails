"""tastytrade Open API adapter (`api.tastyworks.com`; `api.cert.tastyworks.com` for the sandbox;
the session token goes in a bare `Authorization` header). Written against the published API;
tested offline, not yet against a live account.

Venue facts: every response is enveloped `{data: ...}`; a place has a real venue review at
`/orders/dry-run`; option symbols are the padded 21-character form; a vertical is one order
with two legs and a net `price-effect`; there is no client order id, so idempotency recovery
is by this process's memory only (keep the engine's one-in-flight guard armed).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from agentic_guardrails.broker import BrokerMalformed
from agentic_guardrails.brokers import _common as c
from agentic_guardrails.brokers.rest import RestTransport, body_dict, raise_for
from agentic_guardrails.brokers.symbols import core_occ, padded
from agentic_guardrails.types import Account, BrokerOrder, OrderIntent, OrderPlan, OrderState, Position, Quote, QuoteSource

_STATES = {
    "received": OrderState.PLACED,
    "routed": OrderState.PLACED,
    "in flight": OrderState.PLACED,
    "live": OrderState.PLACED,
    "cancel requested": OrderState.PLACED,
    "replace requested": OrderState.PLACED,
    "contingent": OrderState.PLACED,
    "filled": OrderState.FILLED,
    "cancelled": OrderState.CANCELED,
    "removed": OrderState.CANCELED,
    "partially removed": OrderState.CANCELED,
    "rejected": OrderState.REJECTED,
    "expired": OrderState.EXPIRED,
}
_ACTION = {
    OrderIntent.STO: "Sell to Open",
    OrderIntent.CC: "Sell to Open",
    OrderIntent.BTC: "Buy to Close",
    OrderIntent.PCS_OPEN: "Sell to Open",
    OrderIntent.PCS_CLOSE: "Buy to Close",
}
_WING = {OrderIntent.PCS_OPEN: "Buy to Open", OrderIntent.PCS_CLOSE: "Sell to Close"}
_EFFECT = {
    OrderIntent.STO: "Credit",
    OrderIntent.CC: "Credit",
    OrderIntent.BTC: "Debit",
    OrderIntent.PCS_OPEN: "Credit",
    OrderIntent.PCS_CLOSE: "Debit",
}


def order_body(plan: OrderPlan) -> dict:
    legs = [
        {
            "instrument-type": "Equity Option",
            "symbol": padded(plan.contract_symbol),
            "action": _ACTION[plan.intent],
            "quantity": plan.quantity,
        }
    ]
    if plan.long_contract_symbol:
        legs.append(
            {
                "instrument-type": "Equity Option",
                "symbol": padded(plan.long_contract_symbol),
                "action": _WING[plan.intent],
                "quantity": plan.quantity,
            }
        )
    return {
        "time-in-force": "Day" if plan.time_in_force == "gfd" else "GTC",
        "order-type": "Limit",
        "price": str(plan.limit_price),
        "price-effect": _EFFECT[plan.intent],
        "legs": legs,
    }


class TastytradeBroker:
    def __init__(self, transport: RestTransport, *, account_number: str, clock: Callable[[], datetime] | None = None) -> None:
        if not account_number:
            raise BrokerMalformed("tastytrade account number must be pinned before trading")
        self._t = transport
        self._acct = account_number
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._by_client: dict[str, str] = {}

    @staticmethod
    def _data(r, what: str):
        return c.get(body_dict(r, what), "data")

    async def quote(self, contract_symbol: str) -> Quote:
        sym = padded(contract_symbol)
        r = await self._t.request("GET", "/market-data/by-type", params={"equity-option": sym})
        raise_for(r, f"quote {contract_symbol}")
        items = [
            x
            for x in (self._data(r, "quote").get("items") or [])
            if isinstance(x, dict) and str(x.get("symbol", "")).strip() == sym.strip()
        ]
        rec = c.one(items, f"quote {contract_symbol}")
        bid, ask = c.dec(c.get(rec, "bid"), field="bid"), c.dec(c.get(rec, "ask"), field="ask")
        return Quote(
            contract_symbol=contract_symbol,
            bid=bid,
            ask=ask,
            source=QuoteSource.ROBINHOOD,
            as_of=self._clock(),
            delta=None,
            mark=c.opt_dec(rec.get("mark"), field="mark") or (bid + ask) / 2,
        )

    async def account(self) -> Account:
        r = await self._t.request("GET", f"/accounts/{self._acct}/balances")
        raise_for(r, "balances")
        d = self._data(r, "balances")
        cash = c.dec(c.get(d, "cash-balance"), field="cash")
        bp = c.dec(c.get(d, "derivative-buying-power", "equity-buying-power"), field="buying_power")
        return Account(free_cash=cash, buying_power=bp)

    async def positions(self) -> list[Position]:
        r = await self._t.request("GET", f"/accounts/{self._acct}/positions")
        raise_for(r, "positions")
        out = []
        for rec in self._data(r, "positions").get("items") or []:
            if not isinstance(rec, dict):
                raise BrokerMalformed("positions list has a non-object entry")
            if str(c.get(rec, "instrument-type", default="")) != "Equity Option":
                continue
            qty = c.whole(c.get(rec, "quantity"), field="quantity")
            if qty == 0:
                continue
            if str(c.get(rec, "quantity-direction", default="Long")).lower() == "short":
                qty = -abs(qty)
            mult = c.whole(c.get(rec, "multiplier", default=100), field="multiplier")
            out.append(
                Position(
                    contract_symbol=core_occ(str(c.get(rec, "symbol"))),
                    quantity=qty,
                    avg_price=c.dec(c.get(rec, "average-open-price", default=0), field="average-open-price"),
                    multiplier=mult,
                )
            )
        return out

    async def review(self, plan: OrderPlan) -> bool:
        r = await self._t.request("POST", f"/accounts/{self._acct}/orders/dry-run", json=order_body(plan))
        if 400 <= r.status < 500 and r.status not in (401, 403, 429):
            return False
        raise_for(r, "dry-run")
        return "error" not in body_dict(r, "dry-run")

    async def place(self, plan: OrderPlan) -> BrokerOrder:
        r = await self._t.request("POST", f"/accounts/{self._acct}/orders", json=order_body(plan))
        raise_for(r, "place", placing=True)
        order = c.get(self._data(r, "place"), "order")
        boid = str(c.get(order, "id"))
        self._by_client[plan.client_order_id] = boid
        return self._to_order(order, boid)

    async def cancel(self, broker_order_id: str) -> BrokerOrder:
        r = await self._t.request("DELETE", f"/accounts/{self._acct}/orders/{broker_order_id}")
        raise_for(r, f"cancel {broker_order_id}")
        return await self.status(broker_order_id)

    async def status(self, broker_order_id: str) -> BrokerOrder:
        r = await self._t.request("GET", f"/accounts/{self._acct}/orders/{broker_order_id}")
        raise_for(r, f"status {broker_order_id}")
        return self._to_order(self._data(r, "status"), broker_order_id)

    async def find_broker_order_id(self, client_order_id: str) -> str | None:
        return self._by_client.get(client_order_id)

    async def min_tick(self, contract_symbol: str, price: Decimal) -> Decimal:
        return c.nickel_grid(price)

    def _to_order(self, rec: dict, boid: str) -> BrokerOrder:
        if str(c.get(rec, "id", default=boid)) != str(boid):
            raise BrokerMalformed(f"status returned order {rec.get('id')} for {boid}")
        legs = rec.get("legs") or []
        fills = (legs[0].get("fills") or []) if legs and isinstance(legs[0], dict) else []
        filled, dollars = Decimal(0), Decimal(0)
        for f in fills:
            q = c.dec(c.get(f, "quantity"), field="fill quantity")
            filled += q
            dollars += q * c.dec(c.get(f, "fill-price"), field="fill-price")
        n = int(filled)
        px = (dollars / filled).quantize(Decimal("0.0001")) if filled > 0 else None
        total = c.whole(c.get(rec, "size", default=0), field="size") or None
        state = c.map_state(c.get(rec, "status", default=""), _STATES, filled=n, total=total, venue="tastytrade")
        return BrokerOrder(broker_order_id=boid, state=state, filled_qty=n, avg_fill_price=px, raw=rec)
