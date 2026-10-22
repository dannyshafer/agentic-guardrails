"""Interactive Brokers Client Portal Web API adapter (the local gateway, `https://localhost:5000/v1/api`,
or IBKR's hosted OAuth gateway). Written against the published API; tested offline, not yet
against a live account. The institutional and RIA path: this is the venue advisers custody at.

Venue facts: contracts are addressed by `conid`, resolved from (root, expiry, strike, right)
through `/iserver/secdef/search` + `/iserver/secdef/info`; a place can return a chain of
confirmation questions that must each be answered through `/iserver/reply/{id}` before an order
id appears (this adapter confirms them, up to a bound, and surfaces the text in the audit log);
`cOID` is a first-class client order id echoed as `order_ref`; a market-data snapshot may need a
second request before bid/ask populate; quantity on a position is signed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from agentic_guardrails.broker import BrokerError, BrokerMalformed, BrokerRejected, parse_occ
from agentic_guardrails.brokers import _common as c
from agentic_guardrails.brokers.rest import RestTransport, body_dict, body_list, raise_for
from agentic_guardrails.types import Account, BrokerOrder, OrderIntent, OrderPlan, OrderState, Position, Quote, QuoteSource

_STATES = {
    "pendingsubmit": OrderState.PLACED,
    "presubmitted": OrderState.PLACED,
    "submitted": OrderState.PLACED,
    "pendingcancel": OrderState.PLACED,
    "filled": OrderState.FILLED,
    "cancelled": OrderState.CANCELED,
    "apicancelled": OrderState.CANCELED,
    "inactive": OrderState.REJECTED,
    "rejected": OrderState.REJECTED,
}
_SIDE = {
    OrderIntent.STO: "SELL",
    OrderIntent.CC: "SELL",
    OrderIntent.BTC: "BUY",
    OrderIntent.PCS_OPEN: "SELL",
    OrderIntent.PCS_CLOSE: "BUY",
}
_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
MAX_CONFIRMATIONS = 5
FIELD_BID, FIELD_ASK, FIELD_LAST, FIELD_DELTA = "84", "86", "31", "7308"


class IBKRBroker:
    def __init__(self, transport: RestTransport, *, account_id: str, clock: Callable[[], datetime] | None = None) -> None:
        if not account_id:
            raise BrokerMalformed("IBKR account id must be pinned before trading")
        self._t = transport
        self._acct = account_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._conid: dict[str, str] = {}
        self._occ: dict[str, str] = {}

    # ── contract resolution ──────────────────────────────────────────────────
    async def conid(self, contract_symbol: str) -> str:
        if contract_symbol in self._conid:
            return self._conid[contract_symbol]
        occ = parse_occ(contract_symbol)
        yyyy, mm, dd = occ["expiration"].split("-")
        month = f"{_MONTHS[int(mm) - 1]}{yyyy[2:]}"
        r = await self._t.request("GET", "/iserver/secdef/search", params={"symbol": occ["root"], "secType": "STK"})
        raise_for(r, f"secdef search {occ['root']}")
        under = None
        for rec in body_list(r, "secdef search"):
            if str(rec.get("symbol", "")).upper() == occ["root"]:
                under = str(c.get(rec, "conid"))
                break
        if under is None:
            raise BrokerMalformed(f"no IBKR underlying for {occ['root']}")
        r = await self._t.request(
            "GET",
            "/iserver/secdef/info",
            params={
                "conid": under,
                "secType": "OPT",
                "month": month,
                "strike": str(occ["strike"].normalize()),
                "right": "P" if occ["type"] == "put" else "C",
            },
        )
        raise_for(r, f"secdef info {contract_symbol}")
        want = f"{yyyy}{mm}{dd}"
        for rec in body_list(r, "secdef info"):
            if str(rec.get("maturityDate", "")) == want and c.dec(c.get(rec, "strike"), field="strike") == occ["strike"]:
                cid = str(c.get(rec, "conid"))
                self._conid[contract_symbol], self._occ[cid] = cid, contract_symbol
                return cid
        raise BrokerMalformed(f"no IBKR contract for {contract_symbol}")

    # ── reads ────────────────────────────────────────────────────────────────
    async def quote(self, contract_symbol: str) -> Quote:
        cid = await self.conid(contract_symbol)
        fields = ",".join((FIELD_BID, FIELD_ASK, FIELD_LAST, FIELD_DELTA))
        rec = None
        for _ in range(2):  # the first snapshot after subscribing may carry no fields yet
            r = await self._t.request("GET", "/iserver/marketdata/snapshot", params={"conids": cid, "fields": fields})
            raise_for(r, f"snapshot {contract_symbol}")
            recs = body_list(r, "snapshot")
            rec = recs[0] if recs else None
            if rec and rec.get(FIELD_BID) is not None and rec.get(FIELD_ASK) is not None:
                break
        if not rec or rec.get(FIELD_BID) is None or rec.get(FIELD_ASK) is None:
            raise BrokerMalformed(f"IBKR snapshot for {contract_symbol} carried no bid/ask")
        bid, ask = c.dec(rec[FIELD_BID], field="bid"), c.dec(rec[FIELD_ASK], field="ask")
        return Quote(
            contract_symbol=contract_symbol,
            bid=bid,
            ask=ask,
            source=QuoteSource.ROBINHOOD,
            as_of=self._clock(),
            delta=c.opt_dec(rec.get(FIELD_DELTA), field="delta"),
            mark=(bid + ask) / 2,
        )

    async def account(self) -> Account:
        r = await self._t.request("GET", f"/portfolio/{self._acct}/summary")
        raise_for(r, "summary")
        d = body_dict(r, "summary")
        cash = c.dec(c.get(c.get(d, "totalcashvalue"), "amount"), field="cash")
        bp = c.dec(c.get(c.get(d, "buyingpower", "availablefunds"), "amount"), field="buying_power")
        return Account(free_cash=cash, buying_power=bp)

    async def positions(self) -> list[Position]:
        r = await self._t.request("GET", f"/portfolio/{self._acct}/positions/0")
        raise_for(r, "positions")
        out = []
        for rec in body_list(r, "positions"):
            if str(rec.get("assetClass", "")).upper() != "OPT":
                continue
            qty = c.whole(c.get(rec, "position"), field="position")
            if qty == 0:
                continue
            occ = self._occ_from_position(rec)
            mult = c.whole(c.get(rec, "multiplier", default=100), field="multiplier")
            avg = c.dec(c.get(rec, "avgPrice", "avgCost", default=0), field="avgPrice")
            if mult and "avgPrice" not in rec and "avgCost" in rec:
                avg = (avg / Decimal(mult)).quantize(Decimal("0.0001"))
            out.append(Position(contract_symbol=occ, quantity=qty, avg_price=avg, multiplier=mult or 100))
        return out

    def _occ_from_position(self, rec: dict) -> str:
        cid = str(c.get(rec, "conid"))
        if cid in self._occ:
            return self._occ[cid]
        root = str(c.get(rec, "ticker", "undSym")).upper()
        exp = str(c.get(rec, "expiry"))  # YYYYMMDD
        right = str(c.get(rec, "putOrCall")).upper()[:1]
        strike = c.dec(c.get(rec, "strike"), field="strike")
        if len(exp) != 8 or right not in ("P", "C"):
            raise BrokerMalformed(f"cannot build OCC from IBKR position {rec!r}")
        occ = f"O:{root}{exp[2:]}{right}{int((strike * 1000).to_integral_value()):08d}"
        self._occ[cid], self._conid[occ] = occ, cid
        return occ

    # ── order verbs ──────────────────────────────────────────────────────────
    async def _order(self, plan: OrderPlan) -> dict:
        cid = await self.conid(plan.contract_symbol)
        body = {
            "orderType": "LMT",
            "side": _SIDE[plan.intent],
            "quantity": plan.quantity,
            "price": float(plan.limit_price),
            "tif": "DAY" if plan.time_in_force == "gfd" else "GTC",
            "cOID": plan.client_order_id,
            "listingExchange": "SMART",
        }
        if plan.long_contract_symbol:
            wing = await self.conid(plan.long_contract_symbol)
            under = await self._underlying_conid(plan.contract_symbol)
            # A combo: short leg ratio +1 (sold), wing -1, as one BAG on SMART; price is the net.
            body["conidex"] = f"{under};;;{cid}/1,{wing}/-1"
            body["side"] = "SELL" if plan.intent == OrderIntent.PCS_OPEN else "BUY"
        else:
            body["conid"] = int(cid)
            body["secType"] = f"{cid}:OPT"
        return body

    async def _underlying_conid(self, contract_symbol: str) -> str:
        occ = parse_occ(contract_symbol)
        r = await self._t.request("GET", "/iserver/secdef/search", params={"symbol": occ["root"], "secType": "STK"})
        raise_for(r, f"secdef search {occ['root']}")
        for rec in body_list(r, "secdef search"):
            if str(rec.get("symbol", "")).upper() == occ["root"]:
                return str(c.get(rec, "conid"))
        raise BrokerMalformed(f"no IBKR underlying for {occ['root']}")

    async def review(self, plan: OrderPlan) -> bool:
        r = await self._t.request("POST", f"/iserver/account/{self._acct}/orders/whatif", json={"orders": [await self._order(plan)]})
        if 400 <= r.status < 500 and r.status not in (401, 403, 429):
            return False
        raise_for(r, "whatif")
        body = r.body
        return not (isinstance(body, dict) and body.get("error"))

    async def place(self, plan: OrderPlan) -> BrokerOrder:
        r = await self._t.request("POST", f"/iserver/account/{self._acct}/orders", json={"orders": [await self._order(plan)]})
        raise_for(r, "place", placing=True)
        recs = r.body if isinstance(r.body, list) else [r.body]
        for _ in range(MAX_CONFIRMATIONS):
            rec = recs[0] if recs and isinstance(recs[0], dict) else {}
            if rec.get("order_id") is not None:
                return await self.status(str(rec["order_id"]))
            if rec.get("error"):
                raise BrokerRejected(f"place rejected: {rec['error']}")
            reply_id = rec.get("id")
            if reply_id is None:
                raise BrokerError(f"place ambiguous: unrecognised reply {rec!r}")
            # A confirmation question (price cap, size warning): answer yes, keep the text on the audit log.
            r = await self._t.request("POST", f"/iserver/reply/{reply_id}", json={"confirmed": True})
            raise_for(r, "confirm", placing=True)
            recs = r.body if isinstance(r.body, list) else [r.body]
        raise BrokerError("place ambiguous: confirmation chain did not yield an order id")

    async def cancel(self, broker_order_id: str) -> BrokerOrder:
        r = await self._t.request("DELETE", f"/iserver/account/{self._acct}/order/{broker_order_id}")
        raise_for(r, f"cancel {broker_order_id}")
        return await self.status(broker_order_id)

    async def status(self, broker_order_id: str) -> BrokerOrder:
        r = await self._t.request("GET", f"/iserver/account/order/status/{broker_order_id}")
        raise_for(r, f"status {broker_order_id}")
        rec = body_dict(r, "status")
        if str(c.get(rec, "order_id", "orderId", default=broker_order_id)) != str(broker_order_id):
            raise BrokerMalformed(f"status returned order {rec.get('order_id')} for {broker_order_id}")
        filled = c.whole(c.get(rec, "cum_fill", "filledQuantity", default=0), field="cum_fill")
        total = c.whole(c.get(rec, "total_size", "size", default=0), field="size") or None
        px = c.opt_dec(c.get(rec, "avg_price", "avgPrice", default=None), field="avg_price") if filled > 0 else None
        state = c.map_state(c.get(rec, "order_status", "status", default=""), _STATES, filled=filled, total=total, venue="ibkr")
        return BrokerOrder(broker_order_id=str(broker_order_id), state=state, filled_qty=filled, avg_fill_price=px, raw=rec)

    async def find_broker_order_id(self, client_order_id: str) -> str | None:
        r = await self._t.request("GET", "/iserver/account/orders")
        raise_for(r, "orders")
        orders = body_dict(r, "orders").get("orders") or []
        matches = [str(c.get(o, "orderId")) for o in orders if isinstance(o, dict) and str(o.get("order_ref", "")) == client_order_id]
        if len(matches) > 1:
            raise BrokerMalformed(f"{len(matches)} IBKR orders carry cOID {client_order_id}; ambiguous")
        return matches[0] if matches else None

    async def min_tick(self, contract_symbol: str, price: Decimal) -> Decimal:
        return c.nickel_grid(price)
