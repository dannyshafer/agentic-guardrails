"""Tradier Brokerage API adapter (`api.tradier.com/v1`, bearer token; `sandbox.tradier.com` for
paper). Written against the published API; tested offline, not yet against a live account.

Venue facts: orders are form-encoded; the `tag` field carries our client order id (letters,
digits, dashes), so idempotency recovery is exact; `preview=true` on a place is a real venue
review; option symbols are the compact OCC form; a multi-leg order is `class=multileg` with
indexed `option_symbol[i]`/`side[i]`/`quantity[i]` and a net `type=credit|debit`.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from agentic_guardrails.broker import BrokerMalformed, BrokerRejected
from agentic_guardrails.brokers import _common as c
from agentic_guardrails.brokers.rest import RestTransport, body_dict, raise_for
from agentic_guardrails.brokers.symbols import compact, core_occ
from agentic_guardrails.engine import ticker_of
from agentic_guardrails.types import Account, BrokerOrder, OrderIntent, OrderPlan, OrderState, Position, Quote, QuoteSource

_STATES = {
    "pending": OrderState.PLACED,
    "open": OrderState.PLACED,
    "submitted": OrderState.PLACED,
    "partially_filled": OrderState.PARTIAL,
    "filled": OrderState.FILLED,
    "canceled": OrderState.CANCELED,
    "expired": OrderState.EXPIRED,
    "rejected": OrderState.REJECTED,
    "error": OrderState.REJECTED,
    "held": OrderState.PLACED,
    "calculated": OrderState.PLACED,
    "accepted_for_bidding": OrderState.PLACED,
}
_SIDE = {
    OrderIntent.STO: "sell_to_open",
    OrderIntent.CC: "sell_to_open",
    OrderIntent.BTC: "buy_to_close",
    OrderIntent.PCS_OPEN: "sell_to_open",
    OrderIntent.PCS_CLOSE: "buy_to_close",
}
_WING = {OrderIntent.PCS_OPEN: "buy_to_open", OrderIntent.PCS_CLOSE: "sell_to_close"}


def tag_of(client_order_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]", "-", client_order_id)[:255]


def order_form(plan: OrderPlan) -> dict:
    tif = "day" if plan.time_in_force == "gfd" else "gtc"
    if plan.long_contract_symbol:
        return {
            "class": "multileg",
            "symbol": ticker_of(plan.contract_symbol),
            "type": "credit" if plan.intent == OrderIntent.PCS_OPEN else "debit",
            "duration": tif,
            "price": str(plan.limit_price),
            "option_symbol[0]": compact(plan.contract_symbol),
            "side[0]": _SIDE[plan.intent],
            "quantity[0]": str(plan.quantity),
            "option_symbol[1]": compact(plan.long_contract_symbol),
            "side[1]": _WING[plan.intent],
            "quantity[1]": str(plan.quantity),
            "tag": tag_of(plan.client_order_id),
        }
    return {
        "class": "option",
        "symbol": ticker_of(plan.contract_symbol),
        "option_symbol": compact(plan.contract_symbol),
        "side": _SIDE[plan.intent],
        "quantity": str(plan.quantity),
        "type": "limit",
        "duration": tif,
        "price": str(plan.limit_price),
        "tag": tag_of(plan.client_order_id),
    }


def _as_list(v) -> list:
    if v is None or v == "null":
        return []
    return v if isinstance(v, list) else [v]


class TradierBroker:
    def __init__(self, transport: RestTransport, *, account_id: str, clock: Callable[[], datetime] | None = None) -> None:
        if not account_id:
            raise BrokerMalformed("Tradier account id must be pinned before trading")
        self._t = transport
        self._acct = account_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._hdr = {"Accept": "application/json"}

    async def quote(self, contract_symbol: str) -> Quote:
        r = await self._t.request(
            "GET", "/markets/quotes", params={"symbols": compact(contract_symbol), "greeks": "true"}, headers=self._hdr
        )
        raise_for(r, f"quote {contract_symbol}")
        recs = _as_list(c.get(c.get(body_dict(r, "quote"), "quotes"), "quote", default=None))
        rec = c.one([x for x in recs if isinstance(x, dict)], f"quote {contract_symbol}")
        greeks = rec.get("greeks") if isinstance(rec.get("greeks"), dict) else {}
        bid, ask = c.dec(c.get(rec, "bid"), field="bid"), c.dec(c.get(rec, "ask"), field="ask")
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
        r = await self._t.request("GET", f"/accounts/{self._acct}/balances", headers=self._hdr)
        raise_for(r, "balances")
        bal = c.get(body_dict(r, "balances"), "balances")
        cash = c.dec(c.get(bal, "total_cash"), field="cash")
        sub = bal.get("cash") if isinstance(bal.get("cash"), dict) else bal.get("margin") if isinstance(bal.get("margin"), dict) else {}
        bp = c.dec(
            c.get(sub, "option_buying_power", "cash_available", default=c.get(bal, "option_buying_power", default=cash)),
            field="buying_power",
        )
        return Account(free_cash=cash, buying_power=bp)

    async def positions(self) -> list[Position]:
        r = await self._t.request("GET", f"/accounts/{self._acct}/positions", headers=self._hdr)
        raise_for(r, "positions")
        wrap = c.get(body_dict(r, "positions"), "positions", default=None)
        recs = _as_list(c.get(wrap, "position", default=None)) if isinstance(wrap, dict) else []
        out = []
        for rec in recs:
            if not isinstance(rec, dict):
                raise BrokerMalformed("positions list has a non-object entry")
            sym = str(c.get(rec, "symbol"))
            if len(sym) < 16:
                continue  # an equity, not an option
            qty = c.whole(c.get(rec, "quantity"), field="quantity")
            if qty == 0:
                continue
            basis = c.dec(c.get(rec, "cost_basis", default=0), field="cost_basis")
            out.append(
                Position(
                    contract_symbol=core_occ(sym),
                    quantity=qty,
                    avg_price=abs(basis / (Decimal(100) * Decimal(abs(qty)))).quantize(Decimal("0.0001")),
                )
            )
        return out

    async def review(self, plan: OrderPlan) -> bool:
        r = await self._t.request("POST", f"/accounts/{self._acct}/orders", data={**order_form(plan), "preview": "true"}, headers=self._hdr)
        if r.status >= 400 and r.status not in (401, 403, 429) and r.status < 500:
            return False
        raise_for(r, "preview")
        body = body_dict(r, "preview")
        if "errors" in body:
            return False
        return str(c.get(c.get(body, "order", default={}), "status", default="ok")).lower() == "ok"

    async def place(self, plan: OrderPlan) -> BrokerOrder:
        r = await self._t.request("POST", f"/accounts/{self._acct}/orders", data=order_form(plan), headers=self._hdr)
        raise_for(r, "place", placing=True)
        body = body_dict(r, "place")
        if "errors" in body:
            raise BrokerRejected(f"place rejected: {body['errors']}")
        boid = str(c.get(c.get(body, "order"), "id"))
        return await self.status(boid)

    async def cancel(self, broker_order_id: str) -> BrokerOrder:
        r = await self._t.request("DELETE", f"/accounts/{self._acct}/orders/{broker_order_id}", headers=self._hdr)
        raise_for(r, f"cancel {broker_order_id}")
        return await self.status(broker_order_id)

    async def status(self, broker_order_id: str) -> BrokerOrder:
        r = await self._t.request("GET", f"/accounts/{self._acct}/orders/{broker_order_id}", headers=self._hdr)
        raise_for(r, f"status {broker_order_id}")
        return self._to_order(c.get(body_dict(r, "status"), "order"), broker_order_id)

    async def find_broker_order_id(self, client_order_id: str) -> str | None:
        r = await self._t.request("GET", f"/accounts/{self._acct}/orders", headers=self._hdr)
        raise_for(r, "orders")
        wrap = c.get(body_dict(r, "orders"), "orders", default=None)
        recs = _as_list(c.get(wrap, "order", default=None)) if isinstance(wrap, dict) else []
        tag = tag_of(client_order_id)
        matches = [str(c.get(o, "id")) for o in recs if isinstance(o, dict) and str(o.get("tag", "")) == tag]
        if len(matches) > 1:
            raise BrokerMalformed(f"{len(matches)} Tradier orders carry tag {tag}; ambiguous")
        return matches[0] if matches else None

    async def min_tick(self, contract_symbol: str, price: Decimal) -> Decimal:
        return c.tick_for(contract_symbol, price)

    def _to_order(self, rec: dict, boid: str) -> BrokerOrder:
        if str(c.get(rec, "id", default=boid)) != str(boid):
            raise BrokerMalformed(f"status returned order {rec.get('id')} for {boid}")
        filled = c.whole(c.get(rec, "exec_quantity", default=0), field="exec_quantity")
        total = c.whole(c.get(rec, "quantity", default=0), field="quantity") or None
        px = c.opt_dec(c.get(rec, "avg_fill_price", default=None), field="avg_fill_price") if filled > 0 else None
        state = c.map_state(c.get(rec, "status", default=""), _STATES, filled=filled, total=total, venue="tradier")
        return BrokerOrder(broker_order_id=boid, state=state, filled_qty=filled, avg_fill_price=px, raw=rec)
