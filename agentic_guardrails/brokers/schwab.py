"""Schwab Trader API adapter (`api.schwabapi.com`, OAuth bearer you refresh out of band).

Written against the published Trader and Market Data APIs; tested offline, not yet against a
live account. Venue facts that shape it:

- Accounts are addressed by an opaque hash from `/trader/v1/accounts/accountNumbers`; the
  plain account number is pinned in config and resolved once.
- A place returns 201 with the new order id only in the `Location` header.
- There is NO client order id on a Schwab order, so idempotency recovery (`find_broker_order_id`)
  can only answer from this process's own memory of what it placed; across a restart an
  ambiguous placement stays UNKNOWN until the position reconcile reads it. Keep the engine's
  one-in-flight guard armed; it is the real protection here.
- There is no order preview endpoint; `review` validates the body locally and returns True.
- Option symbols are the padded 21-character form.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from agentic_guardrails.broker import BrokerMalformed
from agentic_guardrails.brokers import _common as c
from agentic_guardrails.brokers.rest import RestTransport, body_dict, body_list, raise_for
from agentic_guardrails.brokers.symbols import core_occ, padded
from agentic_guardrails.types import Account, BrokerOrder, OrderIntent, OrderPlan, OrderState, Position, Quote, QuoteSource

logger = logging.getLogger("options_executor.broker")

_STATES = {
    "awaiting_parent_order": OrderState.PLACED,
    "awaiting_condition": OrderState.PLACED,
    "awaiting_stop_condition": OrderState.PLACED,
    "awaiting_manual_review": OrderState.PLACED,
    "accepted": OrderState.PLACED,
    "awaiting_ur_out": OrderState.PLACED,
    "pending_activation": OrderState.PLACED,
    "queued": OrderState.PLACED,
    "working": OrderState.PLACED,
    "new": OrderState.PLACED,
    "awaiting_release_time": OrderState.PLACED,
    "pending_acknowledgement": OrderState.PLACED,
    "pending_recall": OrderState.PLACED,
    "pending_cancel": OrderState.PLACED,
    "pending_replace": OrderState.PLACED,
    "filled": OrderState.FILLED,
    "canceled": OrderState.CANCELED,
    "replaced": OrderState.CANCELED,
    "rejected": OrderState.REJECTED,
    "expired": OrderState.EXPIRED,
    "unknown": OrderState.UNKNOWN,
}
_INSTRUCTION = {
    OrderIntent.STO: "SELL_TO_OPEN",
    OrderIntent.CC: "SELL_TO_OPEN",
    OrderIntent.BTC: "BUY_TO_CLOSE",
    OrderIntent.PCS_OPEN: "SELL_TO_OPEN",
    OrderIntent.PCS_CLOSE: "BUY_TO_CLOSE",
}
_WING = {OrderIntent.PCS_OPEN: "BUY_TO_OPEN", OrderIntent.PCS_CLOSE: "SELL_TO_CLOSE"}
_TIF = {"gfd": "DAY", "gtc": "GOOD_TILL_CANCEL"}


def order_body(plan: OrderPlan) -> dict:
    legs = [
        {
            "instruction": _INSTRUCTION[plan.intent],
            "quantity": plan.quantity,
            "instrument": {"symbol": padded(plan.contract_symbol), "assetType": "OPTION"},
        }
    ]
    body = {
        "orderType": "NET_CREDIT"
        if plan.long_contract_symbol and plan.intent == OrderIntent.PCS_OPEN
        else "NET_DEBIT"
        if plan.long_contract_symbol
        else "LIMIT",
        "session": "NORMAL",
        "price": str(plan.limit_price),
        "duration": _TIF[plan.time_in_force],
        "orderStrategyType": "SINGLE",
        "orderLegCollection": legs,
    }
    if plan.long_contract_symbol:
        legs.append(
            {
                "instruction": _WING[plan.intent],
                "quantity": plan.quantity,
                "instrument": {"symbol": padded(plan.long_contract_symbol), "assetType": "OPTION"},
            }
        )
        body["complexOrderStrategyType"] = "VERTICAL"
    return body


class SchwabBroker:
    def __init__(self, transport: RestTransport, *, account_number: str, clock: Callable[[], datetime] | None = None) -> None:
        if not account_number:
            raise BrokerMalformed("Schwab account number must be pinned before trading")
        self._t = transport
        self._acct = account_number
        self._hash: str | None = None
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._by_client: dict[str, str] = {}

    async def _h(self) -> str:
        if self._hash is None:
            r = await self._t.request("GET", "/trader/v1/accounts/accountNumbers")
            raise_for(r, "account numbers")
            for rec in body_list(r, "account numbers"):
                if str(c.get(rec, "accountNumber")) == self._acct:
                    self._hash = str(c.get(rec, "hashValue"))
                    break
            else:
                raise BrokerMalformed(f"pinned Schwab account {self._acct} not present in the account list (wrong-account guard)")
        return self._hash

    async def quote(self, contract_symbol: str) -> Quote:
        sym = padded(contract_symbol)
        r = await self._t.request("GET", "/marketdata/v1/quotes", params={"symbols": sym, "fields": "quote"})
        raise_for(r, f"quote {contract_symbol}")
        rec = body_dict(r, "quote").get(sym) or body_dict(r, "quote").get(sym.strip())
        if not isinstance(rec, dict):
            raise BrokerMalformed(f"no Schwab quote for {contract_symbol}")
        q = c.get(rec, "quote", default=rec)
        return Quote(
            contract_symbol=contract_symbol,
            bid=c.dec(c.get(q, "bidPrice", "bid"), field="bid"),
            ask=c.dec(c.get(q, "askPrice", "ask"), field="ask"),
            source=QuoteSource.ROBINHOOD,
            as_of=self._clock(),
            delta=c.opt_dec(c.get(q, "delta", default=None), field="delta"),
            mark=c.opt_dec(c.get(q, "mark", "markPrice", default=None), field="mark"),
        )

    async def account(self) -> Account:
        r = await self._t.request("GET", f"/trader/v1/accounts/{await self._h()}")
        raise_for(r, "account")
        bal = c.get(c.get(body_dict(r, "account"), "securitiesAccount"), "currentBalances")
        # Never substitute buying power for cash: a missing cash field fails closed.
        cash = c.dec(c.get(bal, "cashBalance", "cashAvailableForTrading"), field="cash")
        bp = c.dec(c.get(bal, "optionBuyingPower", "buyingPower", "availableFunds"), field="buying_power")
        return Account(free_cash=cash, buying_power=bp)

    async def positions(self) -> list[Position]:
        r = await self._t.request("GET", f"/trader/v1/accounts/{await self._h()}", params={"fields": "positions"})
        raise_for(r, "positions")
        acct = c.get(body_dict(r, "positions"), "securitiesAccount")
        out = []
        for rec in acct.get("positions") or []:
            if not isinstance(rec, dict):
                raise BrokerMalformed("positions list has a non-object entry")
            instr = c.get(rec, "instrument")
            if str(c.get(instr, "assetType", default="")).upper() != "OPTION":
                continue
            qty = c.whole(c.get(rec, "longQuantity", default=0), field="longQuantity") - c.whole(
                c.get(rec, "shortQuantity", default=0), field="shortQuantity"
            )
            if qty == 0:
                continue
            out.append(
                Position(
                    contract_symbol=core_occ(str(c.get(instr, "symbol"))),
                    quantity=qty,
                    avg_price=c.dec(c.get(rec, "averagePrice", default=0), field="averagePrice"),
                )
            )
        return out

    async def review(self, plan: OrderPlan) -> bool:
        order_body(plan)  # a malformed plan raises here, before anything is sent
        return True

    async def place(self, plan: OrderPlan) -> BrokerOrder:
        r = await self._t.request("POST", f"/trader/v1/accounts/{await self._h()}/orders", json=order_body(plan))
        raise_for(r, "place", placing=True)
        loc = {k.lower(): v for k, v in r.headers.items()}.get("location", "")
        boid = loc.rstrip("/").rsplit("/", 1)[-1] if loc else ""
        if not boid:
            # Accepted (2xx) but no id: the UNKNOWN class, resolved by find_broker_order_id.
            raise BrokerMalformed("Schwab place returned no order id in Location; order may have landed")
        self._by_client[plan.client_order_id] = boid
        return await self.status(boid)

    async def cancel(self, broker_order_id: str) -> BrokerOrder:
        r = await self._t.request("DELETE", f"/trader/v1/accounts/{await self._h()}/orders/{broker_order_id}")
        raise_for(r, f"cancel {broker_order_id}")
        return await self.status(broker_order_id)

    async def status(self, broker_order_id: str) -> BrokerOrder:
        r = await self._t.request("GET", f"/trader/v1/accounts/{await self._h()}/orders/{broker_order_id}")
        raise_for(r, f"status {broker_order_id}")
        return self._to_order(body_dict(r, "status"), broker_order_id)

    async def find_broker_order_id(self, client_order_id: str) -> str | None:
        """Schwab carries no client id: only an id this process placed can be recovered."""
        return self._by_client.get(client_order_id)

    async def min_tick(self, contract_symbol: str, price: Decimal) -> Decimal:
        return c.tick_for(contract_symbol, price)

    def _to_order(self, rec: dict, boid: str) -> BrokerOrder:
        if str(c.get(rec, "orderId", default=boid)) != str(boid):
            raise BrokerMalformed(f"status returned order {rec.get('orderId')} for {boid}")
        filled = c.whole(c.get(rec, "filledQuantity", default=0), field="filledQuantity")
        total = c.whole(c.get(rec, "quantity", default=None), field="quantity") if rec.get("quantity") is not None else None
        px = None
        if filled > 0:
            dollars, n = Decimal(0), Decimal(0)
            for act in rec.get("orderActivityCollection") or []:
                for leg in (act.get("executionLegs") or []) if isinstance(act, dict) else []:
                    q = c.dec(c.get(leg, "quantity"), field="exec quantity")
                    dollars += c.dec(c.get(leg, "price"), field="exec price") * q
                    n += q
            px = (dollars / n).quantize(Decimal("0.0001")) if n > 0 else None
        state = c.map_state(c.get(rec, "status", default=""), _STATES, filled=filled, total=total, venue="schwab")
        return BrokerOrder(broker_order_id=boid, state=state, filled_qty=filled, avg_fill_price=px, raw=rec)
