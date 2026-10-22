"""The venue adapters, offline, over a scripted transport. Each one must: build the venue's order
shape from a plan, parse a place/status/positions/account/quote, map a 4xx on place to a reject
and a 401 to an expired session, read a short as negative, never substitute buying power for
cash, and map an unknown state to UNKNOWN."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from agentic_guardrails.broker import BrokerAuthExpired, BrokerMalformed, BrokerRejected, BrokerTransient
from agentic_guardrails.brokers import AlpacaBroker, IBKRBroker, SchwabBroker, TastytradeBroker, TradierBroker
from agentic_guardrails.brokers import schwab, tastytrade, tradier
from agentic_guardrails.brokers.alpaca import order_body as alpaca_body
from agentic_guardrails.brokers.rest import RestResponse, raise_for
from agentic_guardrails.brokers.symbols import compact, core_occ, padded
from agentic_guardrails.types import OrderIntent, OrderPlan, OrderState, PriceBasis, QuoteSource

NOW = datetime(2026, 10, 22, 15, 0, tzinfo=timezone.utc)
PUT = "O:TQQQ261120P00080000"
WING = "O:TQQQ261120P00072000"


def _plan(intent=OrderIntent.STO, wing=None, tif="gfd", coid="sat_abc123"):
    return OrderPlan(
        coid,
        "arm",
        PUT,
        intent,
        1,
        Decimal("1.05"),
        Decimal(80),
        date(2026, 11, 20),
        PriceBasis("bid", QuoteSource.ROBINHOOD, Decimal(1), Decimal("1.1"), NOW),
        NOW,
        long_contract_symbol=wing,
        long_strike=Decimal(72) if wing else None,
        time_in_force=tif,
    )


class Fake:
    """Scripted transport: routes[(METHOD, path)] = one response, or a TUPLE of responses revealed
    in order (the last repeats); a list is a JSON list body. Records every call."""

    def __init__(self, routes):
        self.routes = {k: (list(v) if isinstance(v, tuple) else [v]) for k, v in routes.items()}
        self.calls = []

    async def request(self, method, path, *, params=None, json=None, data=None, headers=None):
        self.calls.append((method, path, params, json, data))
        key = (method, path)
        if key not in self.routes:
            raise AssertionError(f"unscripted {method} {path}")
        q = self.routes[key]
        r = q.pop(0) if len(q) > 1 else q[0]
        return r if isinstance(r, RestResponse) else RestResponse(200, r)


def run(c):
    return asyncio.run(c)


def test_symbols_round_trip():
    assert padded(PUT) == "TQQQ  261120P00080000" and compact(PUT) == "TQQQ261120P00080000"
    assert core_occ("TQQQ  261120P00080000") == PUT and core_occ("TQQQ261120P00080000") == PUT
    assert core_occ("NVDL1 261120P00034000") == "O:NVDL1261120P00034000"
    with pytest.raises(BrokerMalformed):
        core_occ("TQQQ")


def test_raise_for_taxonomy():
    raise_for(RestResponse(200, {}), "x")
    for status, exc in (
        (401, BrokerAuthExpired),
        (403, BrokerAuthExpired),
        (429, BrokerTransient),
        (503, BrokerTransient),
        (400, BrokerMalformed),
    ):
        with pytest.raises(exc):
            raise_for(RestResponse(status, {"e": 1}), "x")
    with pytest.raises(BrokerRejected):
        raise_for(RestResponse(422, {"e": 1}), "x", placing=True)


# ── Schwab ───────────────────────────────────────────────────────────────────
def _schwab(extra=None):
    routes = {
        ("GET", "/trader/v1/accounts/accountNumbers"): [{"accountNumber": "123", "hashValue": "H1"}],
        ("GET", "/trader/v1/accounts/H1"): {
            "securitiesAccount": {
                "currentBalances": {"cashBalance": "19000", "buyingPower": "40000", "optionBuyingPower": "19000"},
                "positions": [
                    {
                        "shortQuantity": 1,
                        "longQuantity": 0,
                        "averagePrice": 1.05,
                        "instrument": {"assetType": "OPTION", "symbol": "TQQQ  261120P00080000"},
                    },
                    {"shortQuantity": 0, "longQuantity": 10, "averagePrice": 90, "instrument": {"assetType": "EQUITY", "symbol": "TQQQ"}},
                ],
            }
        },
        ("GET", "/marketdata/v1/quotes"): {
            "TQQQ  261120P00080000": {"quote": {"bidPrice": 1.0, "askPrice": 1.1, "delta": -0.25, "mark": 1.05}}
        },
        ("POST", "/trader/v1/accounts/H1/orders"): RestResponse(
            201, None, {"Location": "https://api.schwabapi.com/trader/v1/accounts/H1/orders/9001"}
        ),
        ("GET", "/trader/v1/accounts/H1/orders/9001"): (
            {"orderId": 9001, "status": "WORKING", "quantity": 1, "filledQuantity": 0},
            {
                "orderId": 9001,
                "status": "FILLED",
                "quantity": 1,
                "filledQuantity": 1,
                "orderActivityCollection": [{"executionLegs": [{"price": 1.07, "quantity": 1}]}],
            },
        ),
        ("DELETE", "/trader/v1/accounts/H1/orders/9001"): RestResponse(200, None),
    }
    routes.update(extra or {})
    t = Fake(routes)
    return SchwabBroker(t, account_number="123", clock=lambda: NOW), t


def test_schwab_order_body_single_and_vertical():
    b = schwab.order_body(_plan(OrderIntent.BTC, tif="gtc"))
    assert b["orderType"] == "LIMIT" and b["duration"] == "GOOD_TILL_CANCEL" and b["orderLegCollection"][0]["instruction"] == "BUY_TO_CLOSE"
    assert b["orderLegCollection"][0]["instrument"]["symbol"] == "TQQQ  261120P00080000"
    v = schwab.order_body(_plan(OrderIntent.PCS_OPEN, wing=WING))
    assert (
        v["orderType"] == "NET_CREDIT"
        and v["complexOrderStrategyType"] == "VERTICAL"
        and v["orderLegCollection"][1]["instruction"] == "BUY_TO_OPEN"
    )


def test_schwab_place_status_cancel_positions_account_quote():
    br, t = _schwab()
    o = run(br.place(_plan()))
    assert o.broker_order_id == "9001" and o.state == OrderState.PLACED
    s = run(br.status("9001"))
    assert s.state == OrderState.FILLED and s.avg_fill_price == Decimal("1.0700")
    assert run(br.find_broker_order_id("sat_abc123")) == "9001" and run(br.find_broker_order_id("other")) is None
    pos = run(br.positions())
    assert pos == [pos[0]] and pos[0].contract_symbol == PUT and pos[0].quantity == -1
    a = run(br.account())
    assert a.free_cash == Decimal(19000) and a.buying_power == Decimal(19000)
    q = run(br.quote(PUT))
    assert (q.bid, q.ask, q.delta) == (Decimal("1.0"), Decimal("1.1"), Decimal("-0.25"))
    assert run(br.cancel("9001")).state == OrderState.FILLED  # cancel re-reads the authoritative state


def test_schwab_wrong_account_place_reject_auth_and_unknown_state():
    br, t = _schwab({("GET", "/trader/v1/accounts/accountNumbers"): [{"accountNumber": "999", "hashValue": "H9"}]})
    with pytest.raises(BrokerMalformed):
        run(br.account())
    br, t = _schwab({("POST", "/trader/v1/accounts/H1/orders"): RestResponse(400, {"message": "bad"})})
    with pytest.raises(BrokerRejected):
        run(br.place(_plan()))
    br, t = _schwab({("GET", "/trader/v1/accounts/H1"): RestResponse(401, {})})
    with pytest.raises(BrokerAuthExpired):
        run(br.account())
    br, t = _schwab({("GET", "/trader/v1/accounts/H1/orders/9001"): {"orderId": 9001, "status": "SOMETHING_NEW", "filledQuantity": 0}})
    assert run(br.status("9001")).state == OrderState.UNKNOWN
    br, t = _schwab({("GET", "/trader/v1/accounts/H1"): {"securitiesAccount": {"currentBalances": {"buyingPower": "40000"}}}})
    with pytest.raises(BrokerMalformed):  # no cash field: never substitute buying power
        run(br.account())


# ── Tradier ──────────────────────────────────────────────────────────────────
def _tradier(extra=None):
    routes = {
        ("GET", "/markets/quotes"): {
            "quotes": {"quote": {"symbol": "TQQQ261120P00080000", "bid": 1.0, "ask": 1.1, "greeks": {"delta": -0.25}}}
        },
        ("GET", "/accounts/A1/balances"): {"balances": {"total_cash": 19000, "cash": {"option_buying_power": 18000}}},
        ("GET", "/accounts/A1/positions"): {
            "positions": {
                "position": [
                    {"symbol": "TQQQ261120P00080000", "quantity": -1, "cost_basis": -105.0},
                    {"symbol": "TQQQ", "quantity": 10, "cost_basis": 900},
                ]
            }
        },
        ("POST", "/accounts/A1/orders"): {"order": {"id": 777, "status": "ok"}},
        ("GET", "/accounts/A1/orders/777"): (
            {"order": {"id": 777, "status": "open", "quantity": 1, "exec_quantity": 0, "tag": "sat-abc123"}},
            {"order": {"id": 777, "status": "filled", "quantity": 1, "exec_quantity": 1, "avg_fill_price": 1.06, "tag": "sat-abc123"}},
        ),
        ("GET", "/accounts/A1/orders"): {"orders": {"order": [{"id": 777, "tag": "sat-abc123"}, {"id": 778, "tag": "x"}]}},
        ("DELETE", "/accounts/A1/orders/777"): {"order": {"id": 777, "status": "ok"}},
    }
    routes.update(extra or {})
    t = Fake(routes)
    return TradierBroker(t, account_id="A1", clock=lambda: NOW), t


def test_tradier_form_place_status_find_positions():
    f = tradier.order_form(_plan())
    assert (
        f["class"] == "option" and f["option_symbol"] == "TQQQ261120P00080000" and f["side"] == "sell_to_open" and f["tag"] == "sat-abc123"
    )
    m = tradier.order_form(_plan(OrderIntent.PCS_OPEN, wing=WING))
    assert m["class"] == "multileg" and m["type"] == "credit" and m["side[1]"] == "buy_to_open"
    br, t = _tradier()
    assert run(br.review(_plan())) is True and t.calls[-1][4]["preview"] == "true"
    o = run(br.place(_plan()))
    assert o.broker_order_id == "777" and o.state == OrderState.PLACED
    s = run(br.status("777"))
    assert s.state == OrderState.FILLED and s.avg_fill_price == Decimal("1.06")
    assert run(br.find_broker_order_id("sat_abc123")) == "777"
    p = run(br.positions())
    assert len(p) == 1 and p[0].quantity == -1 and p[0].avg_price == Decimal("1.0500")
    a = run(br.account())
    assert a.free_cash == Decimal(19000) and a.buying_power == Decimal(18000)
    assert run(br.quote(PUT)).delta == Decimal("-0.25")


def test_tradier_errors_in_body_and_null_positions():
    br, t = _tradier({("POST", "/accounts/A1/orders"): {"errors": {"error": "insufficient"}}})
    with pytest.raises(BrokerRejected):
        run(br.place(_plan()))
    assert run(br.review(_plan())) is False
    br, t = _tradier({("GET", "/accounts/A1/positions"): {"positions": "null"}})
    assert run(br.positions()) == []


# ── tastytrade ───────────────────────────────────────────────────────────────
def _tasty(extra=None):
    routes = {
        ("GET", "/market-data/by-type"): {
            "data": {"items": [{"symbol": "TQQQ  261120P00080000", "bid": "1.0", "ask": "1.1", "mark": "1.05"}]}
        },
        ("GET", "/accounts/5WX/balances"): {"data": {"cash-balance": "19000", "derivative-buying-power": "18000"}},
        ("GET", "/accounts/5WX/positions"): {
            "data": {
                "items": [
                    {
                        "symbol": "TQQQ  261120P00080000",
                        "instrument-type": "Equity Option",
                        "quantity": 1,
                        "quantity-direction": "Short",
                        "average-open-price": "1.05",
                        "multiplier": 100,
                    }
                ]
            }
        },
        ("POST", "/accounts/5WX/orders/dry-run"): {"data": {"order": {"status": "Received"}, "warnings": []}},
        ("POST", "/accounts/5WX/orders"): {"data": {"order": {"id": 31, "status": "Routed", "size": 1, "legs": [{"fills": []}]}}},
        ("GET", "/accounts/5WX/orders/31"): {
            "data": {"id": 31, "status": "Filled", "size": 1, "legs": [{"fills": [{"quantity": 1, "fill-price": "1.07"}]}]}
        },
        ("DELETE", "/accounts/5WX/orders/31"): {"data": {"id": 31, "status": "Cancel Requested"}},
    }
    routes.update(extra or {})
    t = Fake(routes)
    return TastytradeBroker(t, account_number="5WX", clock=lambda: NOW), t


def test_tastytrade_body_place_status_positions():
    b = tastytrade.order_body(_plan(OrderIntent.PCS_OPEN, wing=WING))
    assert (
        b["time-in-force"] == "Day"
        and b["price-effect"] == "Credit"
        and b["legs"][1]["action"] == "Buy to Open"
        and b["legs"][0]["symbol"] == "TQQQ  261120P00080000"
    )
    br, t = _tasty()
    assert run(br.review(_plan())) is True
    o = run(br.place(_plan()))
    assert o.broker_order_id == "31" and o.state == OrderState.PLACED
    s = run(br.status("31"))
    assert s.state == OrderState.FILLED and s.filled_qty == 1 and s.avg_fill_price == Decimal("1.0700")
    assert run(br.find_broker_order_id("sat_abc123")) == "31"
    p = run(br.positions())
    assert p[0].quantity == -1 and p[0].contract_symbol == PUT
    a = run(br.account())
    assert a.free_cash == Decimal(19000) and a.buying_power == Decimal(18000)
    assert run(br.quote(PUT)).ask == Decimal("1.1")


def test_tastytrade_dry_run_refusal_and_place_reject():
    br, t = _tasty({("POST", "/accounts/5WX/orders/dry-run"): RestResponse(422, {"error": {"message": "bp"}})})
    assert run(br.review(_plan())) is False
    br, t = _tasty({("POST", "/accounts/5WX/orders"): RestResponse(422, {"error": {"message": "bp"}})})
    with pytest.raises(BrokerRejected):
        run(br.place(_plan()))


# ── IBKR ─────────────────────────────────────────────────────────────────────
def _ibkr(extra=None):
    routes = {
        ("GET", "/iserver/secdef/search"): [{"conid": 1, "symbol": "TQQQ"}],
        ("GET", "/iserver/secdef/info"): [
            {"conid": 501, "maturityDate": "20261113", "strike": 80},
            {"conid": 502, "maturityDate": "20261120", "strike": 80},
        ],
        ("GET", "/iserver/marketdata/snapshot"): ([{"conid": 502}], [{"conid": 502, "84": "1.00", "86": "1.10", "7308": "-0.25"}]),
        ("GET", "/portfolio/U1/summary"): {"totalcashvalue": {"amount": 19000}, "buyingpower": {"amount": 18000}},
        ("GET", "/portfolio/U1/positions/0"): [
            {
                "conid": 502,
                "assetClass": "OPT",
                "position": -1,
                "avgPrice": 1.05,
                "ticker": "TQQQ",
                "expiry": "20261120",
                "strike": 80,
                "putOrCall": "P",
                "multiplier": 100,
            },
            {"conid": 1, "assetClass": "STK", "position": 10},
        ],
        ("POST", "/iserver/account/U1/orders"): [{"id": "reply-1", "message": ["You are about to submit..."]}],
        ("POST", "/iserver/reply/reply-1"): [{"order_id": "8001", "order_status": "Submitted"}],
        ("GET", "/iserver/account/order/status/8001"): (
            {"order_id": "8001", "order_status": "Submitted", "cum_fill": 0, "total_size": 1},
            {"order_id": "8001", "order_status": "Filled", "cum_fill": 1, "total_size": 1, "avg_price": "1.06"},
        ),
        ("GET", "/iserver/account/orders"): {"orders": [{"orderId": 8001, "order_ref": "sat_abc123"}]},
        ("DELETE", "/iserver/account/U1/order/8001"): {"msg": "Request was submitted"},
        ("POST", "/iserver/account/U1/orders/whatif"): {"amount": {}},
    }
    routes.update(extra or {})
    t = Fake(routes)
    return IBKRBroker(t, account_id="U1", clock=lambda: NOW), t


def test_ibkr_resolves_conid_confirms_and_reads():
    br, t = _ibkr()
    assert run(br.conid(PUT)) == "502"
    q = run(br.quote(PUT))
    assert (q.bid, q.ask, q.delta) == (Decimal("1.00"), Decimal("1.10"), Decimal("-0.25"))
    assert run(br.review(_plan())) is True
    o = run(br.place(_plan()))
    assert o.broker_order_id == "8001" and o.state == OrderState.PLACED
    body = next(j for m, p, _, j, _ in t.calls if p == "/iserver/account/U1/orders")
    assert body["orders"][0]["cOID"] == "sat_abc123" and body["orders"][0]["side"] == "SELL" and body["orders"][0]["conid"] == 502
    s = run(br.status("8001"))
    assert s.state == OrderState.FILLED and s.avg_fill_price == Decimal("1.06")
    assert run(br.find_broker_order_id("sat_abc123")) == "8001"
    p = run(br.positions())
    assert len(p) == 1 and p[0].contract_symbol == PUT and p[0].quantity == -1
    a = run(br.account())
    assert a.free_cash == Decimal(19000) and a.buying_power == Decimal(18000)


def test_ibkr_place_errors_are_rejects_and_a_dead_snapshot_fails_closed():
    br, t = _ibkr({("POST", "/iserver/account/U1/orders"): [{"error": "insufficient funds"}]})
    with pytest.raises(BrokerRejected):
        run(br.place(_plan()))
    br, t = _ibkr({("GET", "/iserver/marketdata/snapshot"): [{"conid": 502}]})
    with pytest.raises(BrokerMalformed):
        run(br.quote(PUT))


# ── Alpaca ───────────────────────────────────────────────────────────────────
def _alpaca(extra=None):
    trading = {
        ("GET", "/v2/account"): {"cash": "19000", "options_buying_power": "18000", "buying_power": "38000"},
        ("GET", "/v2/positions"): [
            {"symbol": "TQQQ261120P00080000", "asset_class": "us_option", "qty": "-1", "avg_entry_price": "1.05"},
            {"symbol": "TQQQ", "asset_class": "us_equity", "qty": "10"},
        ],
        ("POST", "/v2/orders"): {"id": "o-1", "status": "accepted", "qty": "1", "filled_qty": "0", "client_order_id": "sat_abc123"},
        ("GET", "/v2/orders/o-1"): {"id": "o-1", "status": "filled", "qty": "1", "filled_qty": "1", "filled_avg_price": "1.06"},
        ("DELETE", "/v2/orders/o-1"): RestResponse(204, None),
        ("GET", "/v2/orders:by_client_order_id"): {"id": "o-1"},
    }
    data = {
        ("GET", "/v1beta1/options/snapshots"): {
            "snapshots": {"TQQQ261120P00080000": {"latestQuote": {"bp": 1.0, "ap": 1.1}, "greeks": {"delta": -0.25}}}
        }
    }
    trading.update(extra or {})
    t, d = Fake(trading), Fake(data)
    return AlpacaBroker(t, d, clock=lambda: NOW), t


def test_alpaca_body_place_status_find():
    b = alpaca_body(_plan())
    assert b["symbol"] == "TQQQ261120P00080000" and b["position_intent"] == "sell_to_open" and b["client_order_id"] == "sat_abc123"
    m = alpaca_body(_plan(OrderIntent.PCS_OPEN, wing=WING))
    assert m["order_class"] == "mleg" and m["legs"][1]["position_intent"] == "buy_to_open"
    br, t = _alpaca()
    o = run(br.place(_plan()))
    assert o.broker_order_id == "o-1" and o.state == OrderState.PLACED
    s = run(br.status("o-1"))
    assert s.state == OrderState.FILLED and s.avg_fill_price == Decimal("1.06")
    assert run(br.find_broker_order_id("sat_abc123")) == "o-1"
    p = run(br.positions())
    assert len(p) == 1 and p[0].quantity == -1
    a = run(br.account())
    assert a.free_cash == Decimal(19000) and a.buying_power == Decimal(18000)
    assert run(br.quote(PUT)).delta == Decimal("-0.25")
    assert run(br.cancel("o-1")).state == OrderState.FILLED


def test_alpaca_not_found_client_id_and_place_reject():
    br, t = _alpaca({("GET", "/v2/orders:by_client_order_id"): RestResponse(404, {"message": "not found"})})
    assert run(br.find_broker_order_id("sat_abc123")) is None
    br, t = _alpaca({("POST", "/v2/orders"): RestResponse(403, {"message": "insufficient"})})
    with pytest.raises(BrokerAuthExpired):
        run(br.place(_plan()))
    br, t = _alpaca({("POST", "/v2/orders"): RestResponse(422, {"message": "bad"})})
    with pytest.raises(BrokerRejected):
        run(br.place(_plan()))
