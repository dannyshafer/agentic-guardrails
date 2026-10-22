"""Alpaca Trading API adapter (`api.alpaca.markets` or `paper-api.alpaca.markets` for orders and
positions; `data.alpaca.markets` for quotes; key-pair headers). Written against the published
API; tested offline, not yet against a live account.

Venue facts: `client_order_id` is a first-class field (exact idempotency recovery via
`/v2/orders:by_client_order_id`); option symbols are the compact OCC form; a vertical is
`order_class=mleg` with legs; there is no preview endpoint, so `review` validates locally;
quotes and greeks come from the options snapshots endpoint on the data host, so this adapter
takes two transports.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from agentic_guardrails.broker import BrokerMalformed
from agentic_guardrails.brokers import _common as c
from agentic_guardrails.brokers.rest import RestResponse, RestTransport, body_dict, body_list, raise_for
from agentic_guardrails.brokers.symbols import compact, core_occ
from agentic_guardrails.types import Account, BrokerOrder, OrderIntent, OrderPlan, OrderState, Position, Quote, QuoteSource

_STATES = {
    "new": OrderState.PLACED,
    "accepted": OrderState.PLACED,
    "pending_new": OrderState.PLACED,
    "accepted_for_bidding": OrderState.PLACED,
    "held": OrderState.PLACED,
    "pending_cancel": OrderState.PLACED,
    "pending_replace": OrderState.PLACED,
    "partially_filled": OrderState.PARTIAL,
    "filled": OrderState.FILLED,
    "done_for_day": OrderState.EXPIRED,
    "canceled": OrderState.CANCELED,
    "replaced": OrderState.CANCELED,
    "stopped": OrderState.CANCELED,
    "suspended": OrderState.PLACED,
    "calculated": OrderState.PLACED,
    "expired": OrderState.EXPIRED,
    "rejected": OrderState.REJECTED,
}
_SIDE = {
    OrderIntent.STO: ("sell", "sell_to_open"),
    OrderIntent.CC: ("sell", "sell_to_open"),
    OrderIntent.BTC: ("buy", "buy_to_close"),
    OrderIntent.PCS_OPEN: ("sell", "sell_to_open"),
    OrderIntent.PCS_CLOSE: ("buy", "buy_to_close"),
}
_WING = {OrderIntent.PCS_OPEN: ("buy", "buy_to_open"), OrderIntent.PCS_CLOSE: ("sell", "sell_to_close")}


def order_body(plan: OrderPlan) -> dict:
    tif = "day" if plan.time_in_force == "gfd" else "gtc"
    side, intent = _SIDE[plan.intent]
    if plan.long_contract_symbol:
        wside, wintent = _WING[plan.intent]
        return {
            "order_class": "mleg",
            "qty": str(plan.quantity),
            "type": "limit",
            "limit_price": str(plan.limit_price),
            "time_in_force": tif,
            "client_order_id": plan.client_order_id[:128],
            "legs": [
                {"symbol": compact(plan.contract_symbol), "ratio_qty": "1", "side": side, "position_intent": intent},
                {"symbol": compact(plan.long_contract_symbol), "ratio_qty": "1", "side": wside, "position_intent": wintent},
            ],
        }
    return {
        "symbol": compact(plan.contract_symbol),
        "qty": str(plan.quantity),
        "side": side,
        "position_intent": intent,
        "type": "limit",
        "time_in_force": tif,
        "limit_price": str(plan.limit_price),
        "client_order_id": plan.client_order_id[:128],
    }


class AlpacaBroker:
    def __init__(self, trading: RestTransport, data: RestTransport, *, clock: Callable[[], datetime] | None = None) -> None:
        self._t, self._d = trading, data
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def quote(self, contract_symbol: str) -> Quote:
        sym = compact(contract_symbol)
        r = await self._d.request("GET", "/v1beta1/options/snapshots", params={"symbols": sym})
        raise_for(r, f"quote {contract_symbol}")
        snap = (body_dict(r, "snapshot").get("snapshots") or {}).get(sym)
        if not isinstance(snap, dict):
            raise BrokerMalformed(f"no Alpaca snapshot for {contract_symbol}")
        q = c.get(snap, "latestQuote")
        greeks = snap.get("greeks") if isinstance(snap.get("greeks"), dict) else {}
        bid, ask = c.dec(c.get(q, "bp"), field="bid"), c.dec(c.get(q, "ap"), field="ask")
        return Quote(
            contract_symbol=contract_symbol,
            bid=bid,
            ask=ask,
            source=QuoteSource.ROBINHOOD,
            as_of=self._clock(),
            delta=c.opt_dec(greeks.get("delta"), field="delta"),
            mark=(bid + ask) / 2,
        )

    async def account(self) -> Account:
        r = await self._t.request("GET", "/v2/account")
        raise_for(r, "account")
        a = body_dict(r, "account")
        return Account(
            free_cash=c.dec(c.get(a, "cash"), field="cash"),
            buying_power=c.dec(c.get(a, "options_buying_power", "buying_power"), field="buying_power"),
        )

    async def positions(self) -> list[Position]:
        r = await self._t.request("GET", "/v2/positions")
        raise_for(r, "positions")
        out = []
        for rec in body_list(r, "positions"):
            if str(c.get(rec, "asset_class", default="")) != "us_option":
                continue
            qty = c.whole(c.get(rec, "qty"), field="qty")
            if qty == 0:
                continue
            out.append(
                Position(
                    contract_symbol=core_occ(str(c.get(rec, "symbol"))),
                    quantity=qty,
                    avg_price=c.dec(c.get(rec, "avg_entry_price", default=0), field="avg_entry_price"),
                )
            )
        return out

    async def review(self, plan: OrderPlan) -> bool:
        order_body(plan)
        return True

    async def place(self, plan: OrderPlan) -> BrokerOrder:
        r = await self._t.request("POST", "/v2/orders", json=order_body(plan))
        raise_for(r, "place", placing=True)
        return self._to_order(body_dict(r, "place"), None)

    async def cancel(self, broker_order_id: str) -> BrokerOrder:
        r = await self._t.request("DELETE", f"/v2/orders/{broker_order_id}")
        if r.status != 422:  # 422 = already terminal; read the state below
            raise_for(r, f"cancel {broker_order_id}")
        return await self.status(broker_order_id)

    async def status(self, broker_order_id: str) -> BrokerOrder:
        r = await self._t.request("GET", f"/v2/orders/{broker_order_id}")
        raise_for(r, f"status {broker_order_id}")
        return self._to_order(body_dict(r, "status"), broker_order_id)

    async def find_broker_order_id(self, client_order_id: str) -> str | None:
        r: RestResponse = await self._t.request("GET", "/v2/orders:by_client_order_id", params={"client_order_id": client_order_id[:128]})
        if r.status == 404:
            return None
        raise_for(r, "find order")
        return str(c.get(body_dict(r, "find"), "id"))

    async def min_tick(self, contract_symbol: str, price: Decimal) -> Decimal:
        return c.tick_for(contract_symbol, price)

    def _to_order(self, rec: dict, want: str | None) -> BrokerOrder:
        boid = str(c.get(rec, "id"))
        if want is not None and boid != str(want):
            raise BrokerMalformed(f"status returned order {boid} for {want}")
        filled = c.whole(c.get(rec, "filled_qty", default=0), field="filled_qty")
        total = c.whole(c.get(rec, "qty", default=0), field="qty") or None
        px = c.opt_dec(c.get(rec, "filled_avg_price", default=None), field="filled_avg_price") if filled > 0 else None
        state = c.map_state(c.get(rec, "status", default=""), _STATES, filled=filled, total=total, venue="alpaca")
        return BrokerOrder(
            broker_order_id=boid, state=state, filled_qty=filled, avg_fill_price=abs(px) if px is not None else None, raw=rec
        )
