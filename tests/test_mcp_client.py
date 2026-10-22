"""Offline tests for the Robinhood MCP transport + broker adapter.

No network: the transport is driven by a fake httpx client and the broker by a
fake MCP client. These pin the parse/refresh/error-taxonomy behavior that the
live surface (open questions #2-#6) will be validated against with recorded-
response fixtures at Phase 0. The point here is that every parser FAILS CLOSED
and every transport error maps to the right broker error, so a wrong guess about
the live shape is loud and blocking, never a silently-wrong trade.
"""

from __future__ import annotations

import json
import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from agentic_guardrails.broker import (
    BrokerAuthExpired,
    BrokerError,
    BrokerMalformed,
    BrokerRejected,
    BrokerTimeout,
    BrokerTransient,
    RhToolNames,
    RobinhoodMCPBroker,
    _occ_of,
    parse_occ,
)
from agentic_guardrails.mcp_client import (
    MCPAuthError,
    MCPProtocolError,
    MCPTimeout,
    MCPToolResult,
    MCPTransportError,
    RobinhoodMCPClient,
    TokenBundle,
    TokenStore,
)
from agentic_guardrails.ids import broker_ref_id
from agentic_guardrails.types import OrderIntent, OrderPlan, OrderState, PriceBasis, QuoteSource

CANARY = "O:TQQQ261016P00080000"
# The broker echoes the UUID ref_id we send (derived from our client_order_id),
# not the raw sat_* id, so test records must carry the derived form.
REF_X = broker_ref_id("sat_x")
REF_SP = broker_ref_id("sat_sp")
TOOLS = RhToolNames(
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

# TQQQ 2026-10-16 $80 put, matching CANARY, as get_option_instruments returns it.
_INSTR = {
    "data": {
        "instruments": [{"id": "iid1", "strike_price": "80.0000", "type": "put", "expiration_date": "2026-10-16", "chain_symbol": "TQQQ"}]
    }
}


# ── fakes ─────────────────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, status_code=200, headers=None, text="", json_body=None):
        self.status_code = status_code
        self.headers = headers or {"content-type": "application/json"}
        self._json = json_body
        self.text = text if text else (json.dumps(json_body) if json_body is not None else "")

    def json(self):
        if self._json is not None:
            return self._json
        return json.loads(self.text)


class FakeHTTP:
    """Stands in for httpx.AsyncClient. `endpoint_responses` is a queue popped per
    MCP POST; `token_response` answers the refresh POST. A response may be an
    Exception to raise."""

    def __init__(self, endpoint_responses, token_response=None):
        self._eps = list(endpoint_responses)
        self._token = token_response
        self.token_posts = 0
        self.closed = False

    async def post(self, url, json=None, headers=None, data=None):
        if data is not None:  # the token refresh call uses form data
            self.token_posts += 1
            r = self._token
            if isinstance(r, Exception):
                raise r
            return r
        r = self._eps.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    async def aclose(self):
        self.closed = True


def _init_ok():
    return _Resp(
        headers={"content-type": "application/json", "mcp-session-id": "sess-1"},
        json_body={"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18"}},
    )


def _notif_ok():
    return _Resp(status_code=202, text="")


def _client(responses, token_response=None):
    # The MCP handshake is TWO posts (initialize + notifications/initialized), so
    # every client auto-consumes them first; a test lists only its tools/call
    # response(s).
    # The token is injected below, but a 401 refresh persists it, so the store needs a
    # real throwaway path (an empty path wrote a stray `.tmp` into the cwd).
    store = TokenStore(str(Path(tempfile.mkdtemp()) / "token.json"))
    full = [_init_ok(), _notif_ok(), *responses]
    c = RobinhoodMCPClient("https://x/mcp", store, client_factory=lambda: FakeHTTP(full, token_response))
    c._token = TokenBundle(access_token="tok0", refresh_token="ref0", token_url="https://x/token", client_id="cid")
    return c


def _rpc_result(rid, result):
    return _Resp(json_body={"jsonrpc": "2.0", "id": rid, "result": result})


class FakeMCPClient:
    """Stands in for RobinhoodMCPClient at the broker level: call_tool returns a
    scripted MCPToolResult keyed by tool name, or raises a scripted exception."""

    def __init__(self, by_tool):
        self.by_tool = by_tool
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        v = self.by_tool[name]
        if isinstance(v, Exception):
            raise v
        return v

    async def aclose(self):
        pass


def _tool_ok(structured=None, content=None, is_error=False):
    return MCPToolResult(is_error=is_error, structured=structured, content=content or [], raw={})


def _broker(by_tool, account_id="AG-1"):
    return RobinhoodMCPBroker(FakeMCPClient(by_tool), TOOLS, account_id=account_id, clock=lambda: datetime(2026, 9, 9, tzinfo=timezone.utc))


# ── TokenStore ────────────────────────────────────────────────────────────────
def test_token_store_roundtrip_and_0600(tmp_path):
    p = tmp_path / "tok.json"
    store = TokenStore(str(p))
    store.save(TokenBundle(access_token="a", refresh_token="r", token_url="u", client_id="c"))
    loaded = store.load()
    assert loaded.access_token == "a" and loaded.refresh_token == "r" and loaded.token_url == "u"
    assert (p.stat().st_mode & 0o777) == 0o600


def test_token_store_missing_and_empty(tmp_path):
    with pytest.raises(MCPAuthError):
        TokenStore(str(tmp_path / "nope.json")).load()
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"refresh_token": "r"}))  # no access_token
    with pytest.raises(MCPAuthError):
        TokenStore(str(p)).load()


# ── transport ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_call_tool_json_happy():
    c = _client([_rpc_result(2, {"structuredContent": {"ok": 1}, "content": []})])
    res = await c.call_tool("get_accounts", {})
    assert res.structured == {"ok": 1} and not res.is_error
    assert c._session_id == "sess-1"  # captured from the initialize header


@pytest.mark.asyncio
async def test_sse_response_parsing():
    sse = "event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"structuredContent": {"v": 9}}}) + "\n\n"
    c = _client([_Resp(headers={"content-type": "text/event-stream"}, text=sse)])
    res = await c.call_tool("get_options_quotes", {})
    assert res.structured == {"v": 9}


@pytest.mark.asyncio
async def test_401_refreshes_then_succeeds():
    token_resp = _Resp(json_body={"access_token": "tok1", "refresh_token": "ref1", "expires_in": 3600})
    # initialize ok, then tools/call gets 401, refresh, retry ok
    c = _client([_Resp(status_code=401, text="unauthorized"), _rpc_result(2, {"structuredContent": {"x": 1}})], token_response=token_resp)
    res = await c.call_tool("get_accounts", {})
    assert res.structured == {"x": 1}
    assert c._token.access_token == "tok1"  # refreshed in memory


@pytest.mark.asyncio
async def test_401_without_refresh_token_raises_auth():
    c = _client([_Resp(status_code=401, text="unauthorized")])
    c._token = TokenBundle(access_token="tok0")  # no refresh material
    with pytest.raises(MCPAuthError):
        await c.call_tool("get_accounts", {})


@pytest.mark.asyncio
async def test_jsonrpc_error_raises_protocol():
    c = _client([_Resp(json_body={"jsonrpc": "2.0", "id": 2, "error": {"code": -32000, "message": "boom"}})])
    with pytest.raises(MCPProtocolError):
        await c.call_tool("get_accounts", {})


@pytest.mark.asyncio
async def test_5xx_is_transport_and_timeout_maps():
    c = _client([_Resp(status_code=503, text="down")])
    with pytest.raises(MCPTransportError):
        await c.call_tool("get_accounts", {})
    c2 = _client([httpx.TimeoutException("slow")])
    with pytest.raises(MCPTimeout):
        await c2.call_tool("get_accounts", {})


# ── OCC parsing ───────────────────────────────────────────────────────────────
def test_parse_occ():
    occ = parse_occ(CANARY)
    assert occ == {"root": "TQQQ", "expiration": "2026-10-16", "type": "put", "strike": Decimal("80")}
    assert parse_occ("NVDL260918P00030000")["strike"] == Decimal("30")


def test_parse_occ_malformed():
    for bad in ("TQQQ", "O:TQQQ2610", "O:12345P00080000", "O:TQQQ261016X00080000"):
        with pytest.raises(BrokerMalformed):
            parse_occ(bad)


def env(data):
    """Wrap a payload in the Robinhood {data, guide} envelope, as a tool result."""
    return _tool_ok({"data": data, "guide": "chatbot text, ignored"})


# ── broker reads ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_quote_resolves_instrument_and_parses_nested_quote():
    b = _broker(
        {
            "get_option_instruments": _tool_ok(_INSTR),
            "get_option_quotes": env(
                {
                    "results": [
                        {
                            "quote": {
                                "instrument_id": "iid1",
                                "bid_price": "1.20",
                                "ask_price": "1.30",
                                "delta": "-0.24",
                                "mark_price": "1.25",
                            }
                        }
                    ]
                }
            ),
        }
    )
    q = await b.quote(CANARY)
    assert q.bid == Decimal("1.20") and q.ask == Decimal("1.30") and q.source == QuoteSource.ROBINHOOD
    # greeks/mark are surfaced for the assignment guard; absent -> None
    assert q.delta == Decimal("-0.24") and q.mark == Decimal("1.25")


@pytest.mark.asyncio
async def test_quote_without_greeks_leaves_them_none():
    b = _broker(
        {
            "get_option_instruments": _tool_ok(_INSTR),
            "get_option_quotes": env({"results": [{"quote": {"bid_price": "1.20", "ask_price": "1.30"}}]}),
        }
    )
    q = await b.quote(CANARY)
    assert q.delta is None and q.mark is None


@pytest.mark.asyncio
async def test_quote_missing_field_fails_closed():
    b = _broker(
        {
            "get_option_instruments": _tool_ok(_INSTR),
            "get_option_quotes": env({"results": [{"quote": {"bid_price": "1.20"}}]}),  # no ask
        }
    )
    with pytest.raises(BrokerMalformed):
        await b.quote(CANARY)


@pytest.mark.asyncio
async def test_instrument_not_found_fails_closed():
    # instruments returned, but none at the requested strike -> fail closed
    b = _broker({"get_option_instruments": env({"instruments": [{"id": "x", "strike_price": "70.0000", "type": "put"}]})})
    with pytest.raises(BrokerMalformed):
        await b.quote(CANARY)


def _accts(agentic=True, level="option_level_3", num="AG-1"):
    return env({"accounts": [{"account_number": num, "agentic_allowed": agentic, "option_level": level}]})


@pytest.mark.asyncio
async def test_account_reads_balances_from_portfolio():
    b = _broker({"get_accounts": _accts(), "get_portfolio": env({"cash": "5000", "buying_power": {"buying_power": "9000"}})})
    acct = await b.account()
    assert acct.free_cash == Decimal("5000") and acct.buying_power == Decimal("9000")


@pytest.mark.asyncio
async def test_account_guards_wrong_id_not_agentic_and_no_option_level():
    port = env({"cash": "5000", "buying_power": {"buying_power": "9000"}})
    # wrong pinned id
    with pytest.raises(BrokerMalformed):
        await _broker({"get_accounts": _accts(num="AG-1"), "get_portfolio": port}, account_id="AG-999").account()
    # agentic not allowed
    with pytest.raises(BrokerMalformed):
        await _broker({"get_accounts": _accts(agentic=False), "get_portfolio": port}).account()
    # options not enabled on the account (the empty-option_level launch blocker)
    with pytest.raises(BrokerMalformed):
        await _broker({"get_accounts": _accts(level=""), "get_portfolio": port}).account()


@pytest.mark.asyncio
async def test_account_requires_pinned_id():
    # a blank account id fails closed BEFORE any read on every trading-scoped verb.
    b = _broker({}, account_id="")
    with pytest.raises(BrokerMalformed):
        await b.account()
    with pytest.raises(BrokerMalformed):
        await b.positions()
    with pytest.raises(BrokerMalformed):
        await b.place(_plan())


@pytest.mark.asyncio
async def test_buying_power_never_substitutes_for_cash():
    # portfolio has buying_power but no cash -> fail closed, NEVER read bp as cash.
    b = _broker({"get_accounts": _accts(), "get_portfolio": env({"buying_power": {"buying_power": "9000"}})})
    with pytest.raises(BrokerMalformed):
        await b.account()


@pytest.mark.asyncio
async def test_positions_short_is_negative_and_resolves_occ_by_option_id():
    # a real position carries option_id + direction (short/long), NOT the strike;
    # the strike/put-call is resolved via get_option_instruments.
    payload = {"positions": [{"option_id": "iid1", "quantity": "2", "type": "short", "average_price": "1.10"}]}
    b = _broker({"get_option_positions": env(payload), "get_option_instruments": _tool_ok(_INSTR)})
    pos = await b.positions()
    assert len(pos) == 1 and pos[0].quantity == -2 and pos[0].contract_symbol == CANARY


@pytest.mark.asyncio
async def test_positions_fail_closed_on_malformed():
    # missing quantity, fractional quantity, no option id, and a non-object entry
    # each fail closed (a silently dropped short understates exposure).
    bad_payloads = [
        {"positions": [{"option_id": "iid1", "type": "short"}]},  # no quantity
        {"positions": [{"option_id": "iid1", "quantity": "1.5"}]},  # fractional
        {"positions": [{"quantity": "1", "type": "short"}]},  # no option id to resolve
        {"positions": [{"option_id": "iid1", "quantity": "1"}, "junk"]},  # non-object entry
    ]
    for pay in bad_payloads:
        b = _broker({"get_option_positions": env(pay), "get_option_instruments": _tool_ok(_INSTR)})
        with pytest.raises(BrokerMalformed):
            await b.positions()
    # an EXPLICIT zero quantity is a legitimate omission, not an error
    b = _broker({"get_option_positions": env({"positions": [{"option_id": "iid1", "quantity": "0"}]})})
    assert await b.positions() == []


# ── broker order verbs ────────────────────────────────────────────────────────
def _plan(intent=OrderIntent.STO):
    return OrderPlan(
        client_order_id="sat_x",
        strategy="dip200_7dte",
        contract_symbol=CANARY,
        intent=intent,
        quantity=1,
        limit_price=Decimal("1.20"),
        strike=Decimal("80"),
        expiration=date(2026, 10, 16),
        basis=PriceBasis("sto_bid", QuoteSource.ROBINHOOD, Decimal("1.20"), Decimal("1.30"), datetime(2026, 9, 9, tzinfo=timezone.utc)),
        created_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
    )


def _place_broker(order_result):
    return _broker({"get_option_instruments": _tool_ok(_INSTR), "place_option_order": order_result})


@pytest.mark.asyncio
async def test_place_happy_builds_leg_and_maps_state():
    b = _place_broker(env({"orders": [{"id": "rh-1", "state": "queued", "ref_id": REF_X, "processed_quantity": 0}]}))
    bo = await b.place(_plan())
    assert bo.broker_order_id == "rh-1" and bo.state == OrderState.PLACED and bo.filled_qty == 0
    # the built order carried a single sell/open leg with our instrument + ref_id
    name, args = [c for c in b._c.calls if c[0] == "place_option_order"][0]
    # The broker requires a UUID ref_id (it began rejecting the raw sat_* id Oct 2026).
    import uuid as _uuid

    assert args["ref_id"] == REF_X and _uuid.UUID(args["ref_id"]) and args["direction"] == "credit"
    assert args["legs"][0]["side"] == "sell" and args["legs"][0]["position_effect"] == "open"
    # Robinhood's schema is mixed-typed (both verified live Sep 10 2026): the
    # top-level quantity is a STRING, the leg's ratio_quantity is an INTEGER,
    # price is a string.
    assert args["quantity"] == "1" and isinstance(args["quantity"], str)
    assert args["legs"][0]["ratio_quantity"] == 1 and isinstance(args["legs"][0]["ratio_quantity"], int)
    assert isinstance(args["price"], str)


@pytest.mark.asyncio
async def test_place_parses_singular_order_envelope():
    # Robinhood's real place_option_order returns {"data":{"order":{...}}} — a
    # SINGULAR keyed object, not a list (verified live Sep 10 2026). The order
    # filled; the broker must read the id/state/fill off the inner object, not
    # the {"order":{...}} wrapper. The wrapper bug read a filled order as UNKNOWN
    # with no broker_order_id and left the ledger blind to a live position.
    b = _place_broker(env({"order": {"id": "rh-9", "state": "filled", "ref_id": REF_X, "processed_quantity": 1, "price": "1.02"}}))
    bo = await b.place(_plan())
    assert bo.broker_order_id == "rh-9" and bo.state == OrderState.FILLED and bo.filled_qty == 1


@pytest.mark.asyncio
async def test_place_btc_builds_buy_close_debit_leg():
    # The assignment guard closes via a BTC order; its body must be a buy-to-close
    # debit, not a sell/open. Validates the live-facing close order structure.
    b = _place_broker(env({"order": {"id": "rh-2", "state": "filled", "ref_id": REF_X, "processed_quantity": 1, "price": "1.10"}}))
    bo = await b.place(_plan(OrderIntent.BTC))
    assert bo.broker_order_id == "rh-2" and bo.state == OrderState.FILLED
    name, args = [c for c in b._c.calls if c[0] == "place_option_order"][0]
    assert args["direction"] == "debit"
    assert args["legs"][0]["side"] == "buy" and args["legs"][0]["position_effect"] == "close"


@pytest.mark.asyncio
async def test_place_is_error_rejects():
    b = _place_broker(_tool_ok(content=[{"type": "text", "text": "insufficient buying power"}], is_error=True))
    with pytest.raises(BrokerRejected):
        await b.place(_plan())


@pytest.mark.asyncio
async def test_place_protocol_error_is_unknown_class():
    # a protocol error on PLACE must surface as a generic BrokerError (UNKNOWN),
    # NOT a clean reject: the order may have landed.
    b = _place_broker(MCPProtocolError("garbled"))
    with pytest.raises(BrokerError) as ei:
        await b.place(_plan())
    assert not isinstance(ei.value, BrokerRejected)


@pytest.mark.asyncio
async def test_status_unmapped_state_is_unknown():
    b = _broker({"get_option_orders": env({"orders": [{"id": "rh-1", "state": "who_knows", "processed_quantity": 0}]})})
    bo = await b.status("rh-1")
    assert bo.state == OrderState.UNKNOWN


@pytest.mark.asyncio
async def test_status_matches_requested_id_not_first():
    payload = {
        "orders": [
            {"id": "rh-OTHER", "state": "filled", "processed_quantity": 9},
            {"id": "rh-1", "state": "queued", "processed_quantity": 0},
        ]
    }
    b = _broker({"get_option_orders": env(payload)})
    bo = await b.status("rh-1")
    assert bo.broker_order_id == "rh-1" and bo.filled_qty == 0
    # no matching record -> fail closed
    b2 = _broker({"get_option_orders": env({"orders": [{"id": "rh-OTHER", "state": "filled", "processed_quantity": 9}]})})
    with pytest.raises(BrokerMalformed):
        await b2.status("rh-1")


@pytest.mark.asyncio
async def test_place_ambiguous_multi_order_is_unknown_class():
    b = _place_broker(env({"orders": [{"id": "rh-1", "state": "queued"}, {"id": "rh-2", "state": "queued"}]}))  # neither echoes ref_id
    with pytest.raises(BrokerError) as ei:
        await b.place(_plan())
    assert not isinstance(ei.value, BrokerRejected)
    # but a multi-order response where one carries our ref_id IS resolvable
    b2 = _place_broker(env({"orders": [{"id": "rh-1", "state": "queued"}, {"id": "rh-2", "ref_id": REF_X, "state": "queued"}]}))
    assert (await b2.place(_plan())).broker_order_id == "rh-2"


@pytest.mark.asyncio
async def test_find_broker_order_id_by_ref():
    payload = {"orders": [{"id": "rh-9", "ref_id": REF_X}, {"id": "rh-8", "ref_id": "other"}]}
    b = _broker({"get_option_orders": env(payload)})
    assert await b.find_broker_order_id("sat_x") == "rh-9"
    assert await b.find_broker_order_id("nope") is None


@pytest.mark.asyncio
async def test_cancel_returns_the_matching_order_body():
    # cancel_option_order returns the order envelope: the broker reads the state
    # off the record whose id matches, never the first record (another order's
    # state must not be booked as this cancel's outcome).
    payload = {
        "orders": [
            {"id": "rh-OTHER", "state": "filled", "processed_quantity": 9},
            {"id": "rh-1", "state": "cancelled", "processed_quantity": 0},
        ]
    }
    b = _broker({"cancel_option_order": env(payload)})
    bo = await b.cancel("rh-1")
    assert bo.broker_order_id == "rh-1" and bo.state == OrderState.CANCELED and bo.filled_qty == 0
    # a cancel of a partially-filled order books the partial off the matched record
    b2 = _broker({"cancel_option_order": env({"orders": [{"id": "rh-1", "state": "cancelled", "processed_quantity": 1, "price": "1.02"}]})})
    bo2 = await b2.cancel("rh-1")
    assert bo2.state == OrderState.CANCELED and bo2.filled_qty == 1


@pytest.mark.asyncio
async def test_cancel_ack_falls_through_to_authoritative_status():
    # Robinhood may return only an ACK (no order body) for a cancel. The broker
    # must then read the authoritative state via get_option_orders rather than
    # inventing one from the ack — a cancel is a request, not a terminal fact.
    b = _broker(
        {
            "cancel_option_order": env({"success": True}),  # bare ack, no order in it
            "get_option_orders": env({"orders": [{"id": "rh-1", "state": "cancelled", "processed_quantity": 0}]}),
        }
    )
    bo = await b.cancel("rh-1")
    assert bo.broker_order_id == "rh-1" and bo.state == OrderState.CANCELED
    # the fall-through actually consulted the status tool
    assert any(c[0] == "get_option_orders" for c in b._c.calls)


@pytest.mark.asyncio
async def test_cancel_tool_error_fails_closed():
    b = _broker({"cancel_option_order": _tool_ok(content=[{"type": "text", "text": "no such order"}], is_error=True)})
    with pytest.raises(BrokerMalformed):
        await b.cancel("rh-1")


@pytest.mark.asyncio
async def test_position_with_inline_fields_builds_occ_without_instrument_lookup():
    # A position record that already carries option_type + chain + expiration +
    # strike is resolved DIRECTLY (no get_option_instruments round-trip). Here the
    # call/put lives in `option_type` while `type` is the short/long direction, so
    # the OCC is a put and the signed quantity is negative.
    payload = {
        "positions": [
            {
                "quantity": "2",
                "type": "short",
                "option_type": "put",
                "chain_symbol": "TQQQ",
                "expiration_date": "2026-10-16",
                "strike_price": "80.0000",
                "average_price": "1.10",
            }
        ]
    }
    # deliberately NO get_option_instruments entry: a lookup would KeyError the fake
    b = _broker({"get_option_positions": env(payload)})
    pos = await b.positions()
    assert len(pos) == 1 and pos[0].contract_symbol == CANARY and pos[0].quantity == -2
    assert not any(c[0] == "get_option_instruments" for c in b._c.calls)


def test_occ_of_round_trips_with_parse_occ():
    # _occ_of (position/instrument record -> OCC) is the inverse of parse_occ. It
    # reads option_type before type (on a position `type` is short/long) and
    # milli-encodes the strike; a round-trip pins the encoding both ways.
    occ = _occ_of({"chain_symbol": "TQQQ", "expiration_date": "2026-10-16", "option_type": "put", "strike_price": "80.0000"})
    assert occ == CANARY
    p = parse_occ(occ)
    assert p["root"] == "TQQQ" and p["type"] == "put" and p["strike"] == Decimal("80") and p["expiration"] == "2026-10-16"
    # a call and a fractional (half-dollar) strike encode correctly too
    call = _occ_of({"chain_symbol": "SPY", "expiration_date": "2026-12-18", "type": "call", "strike_price": "612.5000"})
    assert call == "O:SPY261218C00612500" and parse_occ(call)["strike"] == Decimal("612.5")


def test_occ_of_fails_closed_on_unbuildable_record():
    with pytest.raises(BrokerMalformed):
        _occ_of({"chain_symbol": "TQQQ", "expiration_date": "bad", "option_type": "put", "strike_price": "80"})
    with pytest.raises(BrokerMalformed):
        _occ_of({"chain_symbol": "TQQQ", "expiration_date": "2026-10-16", "option_type": "x", "strike_price": "80"})


@pytest.mark.asyncio
async def test_engine_adopts_ambiguous_placement_by_client_id():
    # place TIMES OUT after the broker accepted (UNKNOWN, no broker id in ledger).
    # poll_working must ADOPT it by client_order_id, never leave it UNKNOWN forever.
    from agentic_guardrails.broker import Fault, SimBroker
    from agentic_guardrails.engine import Engine, EngineConfig
    from agentic_guardrails.guards import Caps
    from agentic_guardrails.killswitch import Freshness, KillState
    from agentic_guardrails.ledger import InMemoryLedger
    from agentic_guardrails.types import Account, OrderState

    now = datetime(2026, 9, 9, tzinfo=timezone.utc)
    sim = SimBroker(account_state=Account(free_cash=Decimal("100000"), buying_power=Decimal("100000")))
    sim.faults["place"] = [Fault.TIMEOUT]  # accepted, then we lost the ack
    eng = Engine(
        sim, InMemoryLedger(), EngineConfig(caps=Caps(per_order=Decimal("1000000")), shadow=False, poll_attempts=4), clock=lambda: now
    )
    kill = KillState(
        executor_enabled=True,
        strategy_pnl_today=Decimal(0),
        strategy_daily_stop=None,
        global_pnl_today=Decimal(0),
        global_daily_stop=None,
        last_reconcile_ok=Freshness(now),
        last_selftest_ok=Freshness(now),
    )
    res = await eng.submit(_plan(), kill)
    assert res.record is not None and res.record.state == OrderState.UNKNOWN and res.record.broker_order_id is None
    # the reconciler/poll tick adopts it and drives it to a terminal state
    adopted = await eng.poll_working()
    assert adopted and adopted[0].broker_order_id is not None
    assert adopted[0].state == OrderState.FILLED


# ── error taxonomy mapping (transport -> broker) ──────────────────────────────
@pytest.mark.asyncio
async def test_error_taxonomy_mapping():
    cases = [
        (MCPAuthError("a"), BrokerAuthExpired),
        (MCPTimeout("t"), BrokerTimeout),
        (MCPTransportError("x"), BrokerTransient),
        (MCPProtocolError("p"), BrokerMalformed),
    ]
    for exc, expected in cases:
        b = _broker({"get_accounts": exc})
        with pytest.raises(expected):
            await b.account()


# ── the whole stack composes: broker under the real Engine in SHADOW ──────────
@pytest.mark.asyncio
async def test_engine_shadow_over_robinhood_broker_never_places():
    from agentic_guardrails.engine import Engine, EngineConfig
    from agentic_guardrails.guards import Caps
    from agentic_guardrails.killswitch import Freshness, KillState
    from agentic_guardrails.ledger import InMemoryLedger

    now = datetime(2026, 9, 9, tzinfo=timezone.utc)
    # account+portfolio reads (for guards) + instrument resolve + review; place
    # must NEVER be called in shadow.
    place_sentinel = MCPProtocolError("place should not be called in shadow")
    b = _broker(
        {
            "get_accounts": _accts(),
            "get_portfolio": env({"cash": "50000", "buying_power": {"buying_power": "50000"}}),
            # read before every submission: the adjusted-contract identity check
            "get_option_positions": env({"positions": []}),
            "get_option_instruments": _tool_ok(_INSTR),
            "review_option_order": env({"ok": True}),
            "place_option_order": place_sentinel,
        }
    )
    eng = Engine(b, InMemoryLedger(), EngineConfig(caps=Caps(per_order=Decimal("100000")), shadow=True), clock=lambda: now)
    kill = KillState(
        executor_enabled=True,
        strategy_pnl_today=Decimal(0),
        strategy_daily_stop=None,
        global_pnl_today=Decimal(0),
        global_daily_stop=None,
        last_reconcile_ok=Freshness(now),
        last_selftest_ok=Freshness(now),
    )
    res = await eng.submit(_plan(), kill)
    assert res.accepted and "shadow" in res.reason.lower()
    assert res.record is not None and res.record.shadow
    # place_option_order was never called: only the reads + review appear
    assert [name for name, _ in b._c.calls] == [
        "get_accounts",
        "get_portfolio",
        "get_option_positions",
        "get_option_instruments",
        "review_option_order",
    ]


# ── Phase 2 verticals: one atomic two-leg order ───────────────────────────────
_INSTR_TWO = {
    "data": {
        "instruments": [
            {"id": "iid1", "strike_price": "80.0000", "type": "put", "expiration_date": "2026-10-16", "chain_symbol": "TQQQ"},
            {"id": "iid2", "strike_price": "72.0000", "type": "put", "expiration_date": "2026-10-16", "chain_symbol": "TQQQ"},
        ]
    }
}
WING = "O:TQQQ261016P00072000"


def _spread_plan(intent=OrderIntent.PCS_OPEN, limit="0.94"):
    return OrderPlan(
        client_order_id="sat_sp",
        strategy="pcs_tqqq_10w",
        contract_symbol=CANARY,
        intent=intent,
        quantity=2,
        limit_price=Decimal(limit),
        strike=Decimal("80"),
        expiration=date(2026, 10, 16),
        basis=PriceBasis(
            "pcs_open_bid_cross_ask", QuoteSource.ROBINHOOD, Decimal("0.94"), Decimal("1.10"), datetime(2026, 9, 16, tzinfo=timezone.utc)
        ),
        created_at=datetime(2026, 9, 16, tzinfo=timezone.utc),
        long_contract_symbol=WING,
        long_strike=Decimal("72"),
    )


@pytest.mark.asyncio
async def test_place_a_vertical_sends_both_legs_in_one_credit_order_at_the_net_price():
    b = _broker(
        {
            "get_option_instruments": _tool_ok(_INSTR_TWO),
            "place_option_order": env(
                {"order": {"id": "rh-sp", "state": "filled", "ref_id": REF_SP, "processed_quantity": 2, "price": "0.94"}}
            ),
        }
    )
    bo = await b.place(_spread_plan())
    assert bo.broker_order_id == "rh-sp" and bo.state == OrderState.FILLED and bo.filled_qty == 2
    name, args = [c for c in b._c.calls if c[0] == "place_option_order"][0]
    assert args["direction"] == "credit" and args["quantity"] == "2" and args["price"] == "0.94"
    assert len(args["legs"]) == 2
    short, wing = args["legs"]
    assert short["option_id"] == "iid1" and (short["side"], short["position_effect"]) == ("sell", "open")
    assert wing["option_id"] == "iid2" and (wing["side"], wing["position_effect"]) == ("buy", "open")
    assert short["ratio_quantity"] == 1 and wing["ratio_quantity"] == 1


@pytest.mark.asyncio
async def test_place_a_vertical_close_is_a_debit_with_both_legs_closing():
    b = _broker(
        {
            "get_option_instruments": _tool_ok(_INSTR_TWO),
            "place_option_order": env(
                {"order": {"id": "rh-spc", "state": "filled", "ref_id": REF_SP, "processed_quantity": 2, "price": "0.47"}}
            ),
        }
    )
    await b.place(_spread_plan(OrderIntent.PCS_CLOSE, "0.47"))
    name, args = [c for c in b._c.calls if c[0] == "place_option_order"][0]
    assert args["direction"] == "debit"
    short, wing = args["legs"]
    assert (short["side"], short["position_effect"]) == ("buy", "close")
    assert (wing["side"], wing["position_effect"]) == ("sell", "close")


@pytest.mark.asyncio
async def test_review_of_a_vertical_carries_both_legs_without_a_ref_id():
    b = _broker({"get_option_instruments": _tool_ok(_INSTR_TWO), "review_option_order": env({"ok": True})})
    assert await b.review(_spread_plan())
    name, args = [c for c in b._c.calls if c[0] == "review_option_order"][0]
    assert len(args["legs"]) == 2 and "ref_id" not in args


def test_fill_price_is_the_execution_not_the_limit():
    # Sep 25 2026: a 0.34 STO limit filled at 0.40; the ledger booked 0.34.
    from decimal import Decimal

    from agentic_guardrails.broker import _realized_fill_price

    rec = {
        "price": "0.34000000",
        "processed_premium": "40",
        "trade_value_multiplier": "100.0000",
        "legs": [{"executions": [{"price": "0.40000000", "quantity": "1.00000"}]}],
    }
    assert _realized_fill_price(rec, 1) == Decimal("0.4000")
    rec.pop("processed_premium")
    assert _realized_fill_price(rec, 1) == Decimal("0.4000")
    assert _realized_fill_price({"price": "0.34"}, 1) == Decimal("0.34")
    assert _realized_fill_price(rec, 0) is None


# ── OCC-adjusted contracts (reverse split) through the real adapter path ─────
@pytest.mark.asyncio
async def test_positions_read_adjusted_root_and_multiplier_from_instrument():
    """Real-shaped payloads: a position carries only option_id + direction; the
    instrument lookup supplies the root and trade_value_multiplier. A digit root
    (NVDL1) and a non-100 multiplier on a standard root must both surface as
    adjusted, and a standard instrument must not."""
    instr = {
        "data": {
            "instruments": [
                {
                    "id": "iid9",
                    "strike_price": "34.0000",
                    "type": "put",
                    "expiration_date": "2026-10-02",
                    "chain_symbol": "NVDL1",
                    "trade_value_multiplier": "100.0000",
                }
            ]
        }
    }
    pos_payload = {"positions": [{"option_id": "iid9", "quantity": "2.0000", "type": "short", "average_price": "27.0000"}]}
    b = _broker({"get_option_positions": env(pos_payload), "get_option_instruments": _tool_ok(instr)})
    (p,) = await b.positions()
    assert p.contract_symbol == "O:NVDL1261002P00034000" and p.quantity == -2 and p.adjusted

    instr_mult = {"data": {"instruments": [dict(instr["data"]["instruments"][0], chain_symbol="NVDL", trade_value_multiplier="10.0000")]}}
    b = _broker({"get_option_positions": env(pos_payload), "get_option_instruments": _tool_ok(instr_mult)})
    (p,) = await b.positions()
    assert p.contract_symbol == "O:NVDL261002P00034000" and p.multiplier == 10 and p.adjusted

    instr_std = {"data": {"instruments": [dict(instr["data"]["instruments"][0], chain_symbol="NVDL")]}}
    b = _broker({"get_option_positions": env(pos_payload), "get_option_instruments": _tool_ok(instr_std)})
    (p,) = await b.positions()
    assert not p.adjusted and p.multiplier == 100
