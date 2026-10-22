"""Broker adapter protocol + the SimBroker test double (docs plan 3.1, 5.2).

`BrokerAdapter` is the six-verb order vocabulary plus account reads, all async
(the real transport is network I/O). The real `RobinhoodMCPBroker` lands in a
later slice; here we ship `SimBroker`, an in-memory broker with a scriptable
market and a fault schedule, because with NO broker sandbox the SimBroker is the
only place every failure mode can be rehearsed against the decision core.

SimBroker is deterministic: each order carries a SCRIPT (a list of
(state, cumulative_filled) snapshots revealed one per status read, so a test
dials in exactly the lifecycle it wants), and each method has a per-call FAULT
queue (timeout / auth-expiry / transient-5xx / malformed / reject / duplicate).
It is idempotent by client_order_id (a re-place of the same id returns the
existing order, never a second one), which is what the engine's idempotency
guard relies on.
"""

from __future__ import annotations

import enum
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from agentic_guardrails.ids import broker_ref_id
from agentic_guardrails.types import Account, BrokerOrder, OrderIntent, OrderPlan, OrderState, Position, Quote, QuoteSource

logger = logging.getLogger("options_executor.broker")


# ── Error taxonomy (docs plan 5.5) ────────────────────────────────────────────
class BrokerError(Exception):
    """Base for every broker-side failure the engine must classify."""


class BrokerTimeout(BrokerError):
    """A call did not return a terminal answer. After a PLACE this is the
    UNKNOWN class: the order may or may not exist; the reconciler resolves it.
    Reads may retry with backoff; PLACES never blind-retry."""


class BrokerAuthExpired(BrokerError):
    """The session token expired mid-flight. Halt and re-auth, never retry blind."""


class BrokerRejected(BrokerError):
    """The broker refused the order (terminal REJECTED)."""


class BrokerTransient(BrokerError):
    """A 5xx / transport blip. Reads retry with backoff."""


class BrokerMalformed(BrokerError):
    """A response missing/!type-ing a field we require. Halts (internal-class)."""


class BrokerAdapter(Protocol):
    """The whole surface the engine speaks. All values already normalized to
    Decimal / OCC by the implementation."""

    async def quote(self, contract_symbol: str) -> Quote: ...
    async def review(self, plan: OrderPlan) -> bool: ...
    async def place(self, plan: OrderPlan) -> BrokerOrder: ...
    async def cancel(self, broker_order_id: str) -> BrokerOrder: ...
    async def status(self, broker_order_id: str) -> BrokerOrder: ...
    async def positions(self) -> list[Position]: ...
    async def account(self) -> Account: ...
    # Recovery for an ambiguous placement: the broker order id carrying our
    # deterministic client_order_id, or None if not (yet) present. Never places.
    async def find_broker_order_id(self, client_order_id: str) -> str | None: ...


class Fault(str, enum.Enum):
    NONE = "none"
    TIMEOUT = "timeout"
    AUTH = "auth"
    TRANSIENT = "transient"
    MALFORMED = "malformed"
    REJECT = "reject"


@dataclass
class _SimOrder:
    client_order_id: str
    broker_order_id: str
    plan: OrderPlan
    # (state, cumulative_filled) snapshots revealed one per status read. The
    # last snapshot repeats forever (a settled/working order holds its state).
    script: list[tuple[OrderState, int]]
    cursor: int = 0
    canceled_requested: bool = False

    def current(self) -> tuple[OrderState, int]:
        return self.script[min(self.cursor, len(self.script) - 1)]

    def advance(self) -> tuple[OrderState, int]:
        # Reveal the NEXT snapshot: place() already showed script[0] (via
        # current), so the first status read moves to script[1]. The last
        # snapshot repeats forever once reached.
        if self.cursor < len(self.script) - 1:
            self.cursor += 1
        return self.current()


@dataclass
class SimBroker:
    """In-memory broker. quotes/account/positions are directly settable by a
    test; faults[method] is a queue popped per call."""

    quotes: dict[str, Quote] = field(default_factory=dict)
    account_state: Account = field(default_factory=lambda: Account(free_cash=Decimal(0), buying_power=Decimal(0)))
    positions_state: list[Position] = field(default_factory=list)
    faults: dict[str, list[Fault]] = field(default_factory=lambda: defaultdict(list))
    # Per-order fill scripts a test wants applied when the order is placed,
    # keyed by client_order_id. Absent -> the happy path (PLACED then FILLED).
    scripts: dict[str, list[tuple[OrderState, int]]] = field(default_factory=dict)
    ticks: dict[str, Decimal] = field(default_factory=dict)  # OCC -> min tick (default a penny)
    _orders: dict[str, _SimOrder] = field(default_factory=dict)  # by broker_order_id
    _by_client: dict[str, str] = field(default_factory=dict)  # client_order_id -> broker_order_id
    _seq: int = 0

    # ── fault plumbing ────────────────────────────────────────────────────────
    def _pop_fault(self, method: str) -> Fault:
        q = self.faults.get(method)
        return q.pop(0) if q else Fault.NONE

    @staticmethod
    def _raise_for(fault: Fault) -> None:
        if fault == Fault.TIMEOUT:
            raise BrokerTimeout("sim timeout")
        if fault == Fault.AUTH:
            raise BrokerAuthExpired("sim auth expired")
        if fault == Fault.TRANSIENT:
            raise BrokerTransient("sim 5xx")
        if fault == Fault.MALFORMED:
            raise BrokerMalformed("sim malformed response")

    def _order_view(self, o: _SimOrder, *, advance: bool) -> BrokerOrder:
        st, filled = o.advance() if advance else o.current()
        px = o.plan.limit_price if filled > 0 else None
        return BrokerOrder(broker_order_id=o.broker_order_id, state=st, filled_qty=filled, avg_fill_price=px, raw={"sim": True})

    # ── adapter surface ───────────────────────────────────────────────────────
    async def quote(self, contract_symbol: str) -> Quote:
        self._raise_for(self._pop_fault("quote"))
        q = self.quotes.get(contract_symbol)
        if q is None:
            raise BrokerMalformed(f"no sim quote for {contract_symbol}")
        return q

    async def review(self, plan: OrderPlan) -> bool:
        fault = self._pop_fault("review")
        self._raise_for(fault)
        return fault != Fault.REJECT

    async def place(self, plan: OrderPlan) -> BrokerOrder:
        fault = self._pop_fault("place")
        if fault == Fault.REJECT:
            raise BrokerRejected("sim rejected")
        # Idempotency: a re-place of a known client_order_id returns the existing
        # order (the broker deduped it), never a second order.
        existing = self._by_client.get(plan.client_order_id)
        if existing is not None:
            self._raise_for(fault)  # a timeout on the dup re-place still surfaces
            return self._order_view(self._orders[existing], advance=False)
        self._seq += 1
        boid = f"sim-{self._seq}"
        script = self.scripts.get(plan.client_order_id) or [(OrderState.PLACED, 0), (OrderState.FILLED, plan.quantity)]
        o = _SimOrder(client_order_id=plan.client_order_id, broker_order_id=boid, plan=plan, script=script)
        self._orders[boid] = o
        self._by_client[plan.client_order_id] = boid
        # A TIMEOUT after accept: the order EXISTS (recorded above) but we never
        # got the ack — the UNKNOWN class. The reconciler finds it by client id.
        self._raise_for(fault)
        return self._order_view(o, advance=False)  # PLACED, 0 filled

    async def cancel(self, broker_order_id: str) -> BrokerOrder:
        self._raise_for(self._pop_fault("cancel"))
        o = self._orders.get(broker_order_id)
        if o is None:
            raise BrokerMalformed(f"no sim order {broker_order_id}")
        o.canceled_requested = True
        # A cancel is a request, not a terminal fact: it advances the script (a
        # scripted slow fill may still complete after the cancel), and the caller
        # must re-read status to learn the true outcome.
        return self._order_view(o, advance=True)

    async def status(self, broker_order_id: str) -> BrokerOrder:
        self._raise_for(self._pop_fault("status"))
        o = self._orders.get(broker_order_id)
        if o is None:
            raise BrokerMalformed(f"no sim order {broker_order_id}")
        return self._order_view(o, advance=True)

    async def positions(self) -> list[Position]:
        self._raise_for(self._pop_fault("positions"))
        return list(self.positions_state)

    async def account(self) -> Account:
        self._raise_for(self._pop_fault("account"))
        return self.account_state

    async def find_broker_order_id(self, client_order_id: str) -> str | None:
        self._raise_for(self._pop_fault("find_broker_order_id"))
        return self._by_client.get(client_order_id)

    async def min_tick(self, contract_symbol: str, price: Decimal) -> Decimal:
        return self.ticks.get(contract_symbol, Decimal("0.01"))


# ── The real broker: Robinhood Agentic Trading MCP ────────────────────────────
#
# SURFACE VERIFIED against the live MCP (Sep 9 2026 Phase-0 discovery), with ONE
# exception noted at `_LEG_OPTION_KEY`. The shapes this class parses:
#   - every response is enveloped `{data: <payload>, guide: <chatbot text>}`;
#     `_data()` unwraps it and the `guide` field is ignored.
#   - balances come from get_portfolio(account_number): data.cash +
#     data.buying_power.buying_power (the account object carries NO cash).
#   - an OCC symbol maps to a Robinhood instrument id via
#     get_option_instruments(chain_symbol, expiration_dates=<str>, type,
#     strike_price); quotes take instrument_ids and nest bid/ask under `.quote`.
#   - orders are MULTI-LEG (a `legs` list + a net `direction` credit/debit) and
#     carry `ref_id` (our idempotency key) + `processed_quantity` (the fill).
# Every parser still FAILS CLOSED: a missing/mistyped field raises BrokerMalformed,
# which surfaces as a FAILED self-test and halts the executor, so a shape drift is
# loud and blocking, never a silently-wrong trade. Pin recorded-response fixtures
# (plan 5.3) before the pilot.

_MISSING = object()


@dataclass(frozen=True)
class RhToolNames:
    """MCP tool names, config-driven (OPT_RH_TOOL_*). Overridable from .env."""

    quote: str
    place: str
    cancel: str
    review: str
    order_status: str
    positions: str
    account: str
    portfolio: str  # balances (cash / buying power) live here, not on the account
    instruments: str  # OCC -> Robinhood option instrument id


# Robinhood order-state strings -> our OrderState. Tolerant: an unknown string
# maps to UNKNOWN (fail closed) rather than guessing terminal.
_STATE_MAP: dict[str, OrderState] = {
    "queued": OrderState.PLACED,
    "unconfirmed": OrderState.PLACED,
    "confirmed": OrderState.PLACED,
    "new": OrderState.PLACED,
    "open": OrderState.PLACED,
    "working": OrderState.PLACED,
    "pending": OrderState.PLACED,
    "partially_filled": OrderState.PARTIAL,
    "partial": OrderState.PARTIAL,
    "filled": OrderState.FILLED,
    "cancelled": OrderState.CANCELED,
    "canceled": OrderState.CANCELED,
    "rejected": OrderState.REJECTED,
    "failed": OrderState.REJECTED,
    "voided": OrderState.CANCELED,
    "expired": OrderState.EXPIRED,
}

# For the multi-leg order API: each leg carries a (side, position_effect); the
# order carries a net direction. Selling to open a put / covered call COLLECTS a
# credit; buying to close PAYS a debit. A vertical (Phase 2) is ONE order with
# TWO legs: the short leg first, then the wing; the net direction is the
# spread's (credit on open, debit on close) and `price` is the NET per share.
_INTENT_LEG: dict[OrderIntent, tuple[str, str]] = {
    OrderIntent.STO: ("sell", "open"),
    OrderIntent.CC: ("sell", "open"),
    OrderIntent.BTC: ("buy", "close"),
    OrderIntent.PCS_OPEN: ("sell", "open"),
    OrderIntent.PCS_CLOSE: ("buy", "close"),
}
# The WING's (side, position_effect) for the spread intents: opposite side, same effect.
_INTENT_WING_LEG: dict[OrderIntent, tuple[str, str]] = {
    OrderIntent.PCS_OPEN: ("buy", "open"),
    OrderIntent.PCS_CLOSE: ("sell", "close"),
}
_INTENT_DIRECTION: dict[OrderIntent, str] = {
    OrderIntent.STO: "credit",
    OrderIntent.CC: "credit",
    OrderIntent.BTC: "debit",
    OrderIntent.PCS_OPEN: "credit",
    OrderIntent.PCS_CLOSE: "debit",
}
# The leg field that names the option instrument. Robinhood's REST leg uses
# "option" (a URL); the MCP addresses instruments by id everywhere else
# (option_id on positions/exercise, instrument_ids on quotes), so option_id is
# the consistent choice. This is the ONE detail Phase-0 could not confirm
# read-only (a live review probe is a trading action and was correctly blocked),
# so it is isolated here: a wrong key fails the FIRST real review/place (loud,
# under per-trade approval), never a silent mis-trade. Confirm with one dry
# review before the pilot and adjust if needed.
_LEG_OPTION_KEY = "option_id"


def parse_occ(contract_symbol: str) -> dict:
    """'O:TQQQ261016P00080000' -> {root, expiration(YYYY-MM-DD), type(put/call),
    strike(Decimal)}. The OCC layout: ROOT + YYMMDD + C/P + strike*1000 in 8
    digits. Raises BrokerMalformed on a symbol that is not OCC-shaped."""
    s = contract_symbol.removeprefix("O:").strip().upper()
    # Split from the RIGHT: the date+CP+strike tail is fixed width (15), so an
    # OCC-ADJUSTED root that carries a digit ('NVDL1', issued after a reverse
    # split) still parses. Splitting at the first digit made every adjusted
    # contract raise, and the assignment guard then silently skipped the very
    # short it exists to watch.
    root, tail = s[:-15], s[-15:]
    if not root or not root.isalnum() or not root[0].isalpha() or len(tail) != 15 or tail[6] not in ("C", "P"):
        raise BrokerMalformed(f"not an OCC symbol: {contract_symbol!r}")
    yy, mm, dd = tail[0:2], tail[2:4], tail[4:6]
    cp = "call" if tail[6] == "C" else "put"
    try:
        strike = Decimal(tail[7:]) / Decimal(1000)
    except InvalidOperation as exc:
        raise BrokerMalformed(f"bad strike in {contract_symbol!r}") from exc
    return {"root": root, "expiration": f"20{yy}-{mm}-{dd}", "type": cp, "strike": strike}


def _payload(result) -> Any:
    """The machine-readable body of a tool result: structuredContent if present,
    else the first text content block parsed as JSON. Raises BrokerMalformed if
    neither yields a dict/list."""
    if result.structured is not None:
        return result.structured
    for block in result.content:
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
            try:
                return json.loads(block["text"])
            except json.JSONDecodeError:
                continue
    raise BrokerMalformed("tool result had no structured JSON payload")


def _get(d: dict, *keys: str, default=_MISSING):
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] is not None:
            return d[k]
    if default is _MISSING:
        raise BrokerMalformed(f"missing any of {keys} in {list(d)[:8] if isinstance(d, dict) else type(d)}")
    return default


def _dec(v, *, field: str) -> Decimal:
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise BrokerMalformed(f"non-numeric {field}: {v!r}") from exc


def _first_record(payload: Any, *list_keys: str) -> dict:
    """A tool may return {results: [...]}, {orders: [...]}, a bare list, or a
    bare object. Return the single most-relevant record."""
    if isinstance(payload, dict):
        for k in list_keys:
            v = payload.get(k)
            if isinstance(v, list) and v:
                return v[0]
            if isinstance(v, dict):
                return v
        return payload
    if isinstance(payload, list) and payload:
        return payload[0]
    raise BrokerMalformed(f"expected an object/list, got {type(payload)}")


def _as_object_list(items: list, what: str) -> list[dict]:
    """A copy of `items`, FAILING CLOSED if any entry is not an object."""
    if not all(isinstance(x, dict) for x in items):
        raise BrokerMalformed(what)
    return list(items)


def _records_from_dict(payload: dict, list_keys: tuple[str, ...]) -> list[dict]:
    for k in list_keys:
        v = payload.get(k)
        if isinstance(v, list):
            return _as_object_list(v, f"{k} list has a non-object entry")
        # A SINGULAR keyed object, e.g. place_option_order returns
        # {"data":{"order":{...}}} (verified live Sep 10 2026). Treat a
        # dict-valued key as one record — without this it fell through to
        # `return [payload]` (the WRAPPER {"order":{...}}), whose top level
        # has no id/state, so a filled place read as UNKNOWN with no
        # broker_order_id and the ledger never recorded the live fill.
        if isinstance(v, dict):
            return [v]
    return [payload]


def _records(payload: Any, *list_keys: str) -> list[dict]:
    """Every record as a dict, FAILING CLOSED on a non-object entry. A list with a
    non-dict element is a malformed payload, not something to silently filter: a
    dropped position understates exposure and can arm the health gate against a
    short we do not know we hold (caught in review of PR #476). So a stray
    non-object raises rather than being skipped."""
    if isinstance(payload, dict):
        return _records_from_dict(payload, list_keys)
    if isinstance(payload, list):
        return _as_object_list(payload, "record list has a non-object entry")
    raise BrokerMalformed(f"expected an object or list of objects, got {type(payload)}")


def _match_record(payload: Any, want: str, id_keys: tuple[str, ...], list_keys: tuple[str, ...]) -> dict:
    """Return the SINGLE record whose id (any of `id_keys`) equals `want`. A tool
    that returns several orders must not have its first element booked as the one
    we asked about (another order's fills would corrupt our position/collateral,
    caught in review of PR #476). No unique match fails closed."""
    recs = _records(payload, *list_keys)
    matches = [r for r in recs if str(_get(r, *id_keys, default="")) == str(want)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise BrokerMalformed(f"no record matching id {want!r} (of {len(recs)} returned)")
    raise BrokerMalformed(f"{len(matches)} records match id {want!r}; ambiguous")


def _data(result) -> Any:
    """Unwrap the Robinhood MCP envelope. Every tool returns
    {data: <payload>, guide: <chatbot instructions>}; return `data` when present,
    else the raw payload so a future flattened response still parses."""
    payload = _payload(result)
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _occ_of(instr: dict) -> str:
    """Build the OCC symbol from a Robinhood option-instrument record
    (chain_symbol + expiration_date + type + strike_price). The inverse of
    parse_occ, used to key broker positions the same way the ledger does. Reads
    `option_type` first: on an instrument the call/put field is `type`, but on a
    position `type` is the direction (short/long) and the call/put lives in
    `option_type`."""
    root = str(_get(instr, "chain_symbol", "symbol")).upper()
    exp = str(_get(instr, "expiration_date"))  # YYYY-MM-DD
    typ = str(_get(instr, "option_type", "type")).lower()
    strike = _dec(_get(instr, "strike_price", "strike"), field="strike")
    if len(exp) < 10 or typ[:1] not in ("p", "c"):
        raise BrokerMalformed(f"cannot build OCC from instrument {instr!r}")
    cp = "P" if typ.startswith("p") else "C"
    strike_milli = int((strike * Decimal(1000)).to_integral_value())
    return f"O:{root}{exp[2:4]}{exp[5:7]}{exp[8:10]}{cp}{strike_milli:08d}"


def _multiplier_of(rec: dict) -> int:
    """trade_value_multiplier as an int; 100 when absent (the standard
    contract). An UNPARSEABLE value is reported as 0, i.e. non-standard, so the
    position reads as adjusted and is never auto-traded (fail safe)."""
    raw = _get(rec, "trade_value_multiplier", default=None)
    if raw is None:
        return 100
    try:
        d = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return 0
    return int(d) if d == d.to_integral_value() else 0


def _realized_fill_price(rec: dict, filled: int) -> Decimal | None:
    """Per-share price the order actually EXECUTED at, never the limit.

    Robinhood fills a marketable limit at the better price (a 0.34 STO limit
    filled at 0.40 on Sep 25 2026), and ``price`` echoes the LIMIT, so booking
    it understated realized P&L. ``processed_premium`` is the executed dollars
    (net for a multi-leg), so premium / (filled x multiplier) is the true
    per-share fill; next the leg executions; the limit only as a last resort."""
    if filled <= 0:
        return None
    prem = _get(rec, "processed_premium", default=None)
    mult = _get(rec, "trade_value_multiplier", default="100")
    try:
        if prem is not None and Decimal(str(prem)) > 0:
            return (Decimal(str(prem)) / (Decimal(filled) * Decimal(str(mult)))).quantize(Decimal("0.0001"))
    except (ArithmeticError, ValueError):
        pass
    legs = rec.get("legs") or []
    if len(legs) == 1:
        execs = legs[0].get("executions") or []
        try:
            qty = sum(Decimal(str(e["quantity"])) for e in execs)
            if qty > 0:
                return (sum(Decimal(str(e["price"])) * Decimal(str(e["quantity"])) for e in execs) / qty).quantize(Decimal("0.0001"))
        except (KeyError, ArithmeticError, ValueError):
            pass
    avg_raw = _get(rec, "average_price", "avg_fill_price", "price", default=None)
    return _dec(avg_raw, field="avg_fill_price") if avg_raw is not None else None


class RobinhoodMCPBroker:
    """`BrokerAdapter` over the Robinhood MCP. Owns the OCC<->Robinhood mapping,
    normalization to Decimal/Quote/Position/Account, and mapping the transport's
    error taxonomy onto the executor's. NO trading decisions here (those are the
    engine's); this only speaks the six verbs plus account reads."""

    def __init__(self, client, tools: RhToolNames, *, account_id: str = "", clock=None) -> None:
        self._c = client
        self._tools = tools
        self._account_id = account_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._instrument_cache: dict[str, str] = {}  # OCC -> instrument_id
        self._occ_cache: dict[str, str] = {}  # instrument_id -> OCC
        self._mult_cache: dict[str, int] = {}  # OCC -> trade_value_multiplier (adjusted-contract tell)
        self._tick_cache: dict[str, dict] = {}  # OCC -> Robinhood min_ticks

    async def aclose(self) -> None:
        close = getattr(self._c, "aclose", None)
        if close is not None:
            await close()

    async def _call(self, tool: str, args: dict) -> Any:
        """Call a tool, map transport errors onto broker errors, and unwrap the
        payload. A tool-level isError is surfaced as BrokerRejected by callers
        that place; here it raises BrokerMalformed for reads."""
        from agentic_guardrails.mcp_client import (
            MCPAuthError,
            MCPProtocolError,
            MCPTimeout,
            MCPTransportError,
        )

        if not tool:
            raise BrokerMalformed("tool name not configured (Phase-0 discovery not done)")
        try:
            return await self._c.call_tool(tool, args)
        except MCPAuthError as exc:
            raise BrokerAuthExpired(str(exc)) from exc
        except MCPTimeout as exc:
            raise BrokerTimeout(str(exc)) from exc
        except MCPTransportError as exc:
            raise BrokerTransient(str(exc)) from exc
        except MCPProtocolError as exc:
            raise BrokerMalformed(str(exc)) from exc

    # ── instrument resolution (OCC <-> Robinhood option id) ────────────────────
    async def _instrument_id(self, contract_symbol: str) -> str:
        """OCC -> Robinhood option instrument id, cached. Resolves via
        get_option_instruments filtered by chain/expiration/type, matching the
        exact strike. Not finding it fails closed."""
        cached = self._instrument_cache.get(contract_symbol)
        if cached is not None:
            return cached
        occ = parse_occ(contract_symbol)
        result = await self._call(
            self._tools.instruments,
            {"chain_symbol": occ["root"], "expiration_dates": occ["expiration"], "type": occ["type"]},
        )
        if result.is_error:
            raise BrokerMalformed(f"instrument lookup error for {contract_symbol}: {result.content}")
        want = occ["strike"]
        for rec in _records(_data(result), "instruments", "results"):
            if (
                _dec(_get(rec, "strike_price", "strike"), field="strike") == want
                and str(_get(rec, "type", default="")).lower() == occ["type"]
            ):
                iid = _get(rec, "id", "instrument_id", default=None)
                if iid is None:
                    continue
                iid = str(iid)
                self._instrument_cache[contract_symbol] = iid
                ticks = _get(rec, "min_ticks", default=None)
                if isinstance(ticks, dict):
                    self._tick_cache[contract_symbol] = ticks
                self._occ_cache[iid] = contract_symbol
                return iid
        raise BrokerMalformed(f"no Robinhood option instrument for {contract_symbol}")

    async def min_tick(self, contract_symbol: str, price: Decimal) -> Decimal:
        """The contract's price increment at `price` (Robinhood `min_ticks`:
        below_tick under cutoff_price, above_tick at or over it). NVDX trades in
        nickels under $3 while NVDL/TQQQ trade in pennies (read live Sep 24 2026),
        so a resting limit must be rounded per contract or the venue refuses it.
        An unreadable tick falls back to the coarse nickel/dime grid, which is
        valid on every listed option."""
        if contract_symbol not in self._tick_cache:
            self._instrument_cache.pop(contract_symbol, None)  # force a fresh lookup that carries min_ticks
            await self._instrument_id(contract_symbol)
        ticks = self._tick_cache.get(contract_symbol) or {}
        try:
            cutoff = Decimal(str(ticks.get("cutoff_price", "3.00")))
            key = "above_tick" if price >= cutoff else "below_tick"
            tick = Decimal(str(ticks[key]))
            if tick > 0:
                return tick
        except (KeyError, ArithmeticError, ValueError, TypeError):
            pass
        return Decimal("0.10") if price >= Decimal("3") else Decimal("0.05")

    async def _occ_from_option_id(self, option_id: str) -> str:
        cached = self._occ_cache.get(option_id)
        if cached is not None:
            return cached
        result = await self._call(self._tools.instruments, {"ids": option_id})
        if result.is_error:
            raise BrokerMalformed(f"instrument lookup error for option_id {option_id}: {result.content}")
        instr = _first_record(_data(result), "instruments", "results")
        occ = _occ_of(instr)
        self._mult_cache[occ] = _multiplier_of(instr)
        self._occ_cache[option_id] = occ
        self._instrument_cache[occ] = str(option_id)
        return occ

    # ── reads ─────────────────────────────────────────────────────────────────
    async def quote(self, contract_symbol: str) -> Quote:
        iid = await self._instrument_id(contract_symbol)
        result = await self._call(self._tools.quote, {"instrument_ids": [iid]})
        if result.is_error:
            raise BrokerMalformed(f"quote tool error for {contract_symbol}: {result.content}")
        rec = _first_record(_data(result), "results", "quotes")
        q = _get(rec, "quote", default=rec)  # bid/ask nest under `quote`
        bid = _dec(_get(q, "bid_price", "bid", "best_bid"), field="bid")
        ask = _dec(_get(q, "ask_price", "ask", "best_ask"), field="ask")
        # Greeks/mark are optional: present on Robinhood quotes, used by the
        # assignment guard. A missing or unparseable field stays None rather than
        # failing the quote (bid/ask are the only required fields).
        delta_raw = _get(q, "delta", default=None)
        mark_raw = _get(q, "mark_price", "adjusted_mark_price", default=None)
        delta = _dec(delta_raw, field="delta") if delta_raw is not None else None
        mark = _dec(mark_raw, field="mark") if mark_raw is not None else None
        return Quote(
            contract_symbol=contract_symbol, bid=bid, ask=ask, source=QuoteSource.ROBINHOOD, as_of=self._clock(), delta=delta, mark=mark
        )

    def _require_account(self) -> None:
        """A trading account identity MUST be pinned before any read the health
        gate depends on, or any order verb, runs. Without it a passing self-test
        would not prove orders target the FUNDED Agentic account (wrong-account is
        a named failure mode), so a blank id fails closed here rather than
        defaulting to whatever account the read returns first (caught in review of
        PR #476). Read-only tool DISCOVERY does not go through here."""
        if not self._account_id:
            raise BrokerMalformed(
                "OPT_RH_ACCOUNT_ID is not set; the Agentic account identity must be pinned before trading or health can pass"
            )

    async def account(self) -> Account:
        self._require_account()
        # Identity + eligibility on the pinned account (agentic + options level).
        acct_res = await self._call(self._tools.account, {})
        if acct_res.is_error:
            raise BrokerMalformed(f"account tool error: {acct_res.content}")
        self._select_account(_records(_data(acct_res), "accounts", "results"))
        # Balances come from the PORTFOLIO tool; the account object has no cash.
        port = await self._call(self._tools.portfolio, {"account_number": self._account_id})
        if port.is_error:
            raise BrokerMalformed(f"portfolio tool error: {port.content}")
        data = port_data = _data(port)
        if not isinstance(data, dict):
            raise BrokerMalformed("portfolio payload is not an object")
        # NEVER substitute buying power for settled cash: a missing cash field
        # fails closed rather than reading buying power (which is >= cash on a
        # margin account and would let the cash-secured guard over-commit; the
        # PR #476 finding). `buying_power` is a nested object on this API.
        cash = _dec(_get(data, "cash", "settled_cash"), field="cash")
        bp_field = _get(port_data, "buying_power", "options_buying_power")
        bp = _dec(_get(bp_field, "buying_power") if isinstance(bp_field, dict) else bp_field, field="buying_power")
        return Account(free_cash=cash, buying_power=bp)

    def _select_account(self, recs: list[dict]) -> dict:
        """Pick the pinned Agentic account and assert it can actually trade
        options for the agent: it must be agentic_allowed AND carry a non-zero
        options level. Wrong-account and not-enabled both fail closed (the
        empty-option_level state that blocked launch would trip here)."""
        if not recs:
            raise BrokerMalformed("account read returned no accounts")
        for r in recs:
            if str(_get(r, "account_number", "id", "account_id", default="")) == self._account_id:
                if not _get(r, "agentic_allowed", default=False):
                    raise BrokerMalformed(f"account {self._account_id} is not agentic_allowed; the agent cannot place orders there")
                lvl = str(_get(r, "option_level", default=""))
                if not lvl or lvl.endswith("_0"):
                    raise BrokerMalformed(f"account {self._account_id} has no options level ({lvl!r}); options trading not enabled")
                return r
        raise BrokerMalformed(f"pinned Agentic account {self._account_id} not present in account read (wrong-account guard)")

    async def positions(self) -> list[Position]:
        self._require_account()
        result = await self._call(self._tools.positions, {"account_number": self._account_id})
        if result.is_error:
            raise BrokerMalformed(f"positions tool error: {result.content}")
        out: list[Position] = []
        for rec in _records(_data(result), "positions", "results"):
            # FAIL CLOSED on every record: a position missing its quantity is an
            # unreadable book, and a silently dropped short understates exposure
            # enough to arm the health gate against a position we do not know we
            # hold (PR #476). Only an EXPLICIT zero quantity is a legitimate skip.
            raw_qty = _get(rec, "quantity", "qty", "position")
            qty_dec = _dec(raw_qty, field="quantity")
            if qty_dec != qty_dec.to_integral_value():
                raise BrokerMalformed(f"fractional option quantity {raw_qty!r}")
            qty = int(qty_dec)
            if qty == 0:
                continue
            occ = await self._position_occ(rec)
            # A short option position may be signed already, or flagged by a
            # type/side field. Normalize: short puts/calls are NEGATIVE.
            side = str(_get(rec, "type", "side", "position_type", default="")).lower()
            if qty > 0 and side in ("short", "sell", "sell_to_open"):
                qty = -qty
            avg = _dec(_get(rec, "average_price", "avg_price", "cost_basis", default=0), field="average_price")
            mult = self._mult_cache.get(occ)
            if _get(rec, "trade_value_multiplier", default=None) is not None:
                mult = _multiplier_of(rec)
            out.append(Position(contract_symbol=occ, quantity=qty, avg_price=avg, multiplier=mult if mult is not None else 100))
        return out

    async def _position_occ(self, rec: dict) -> str:
        """OCC for a position record. A Robinhood option position does NOT carry
        the strike / call-put directly (its `type` is the short/long direction);
        its guide says to resolve those via get_option_instruments by option id.
        So always go through the (cached) instrument lookup. A record that already
        carries strike + option_type + chain + expiration is built directly."""
        if _get(rec, "option_type", default=None) is not None and all(
            _get(rec, k, default=None) is not None for k in ("chain_symbol", "expiration_date", "strike_price")
        ):
            return _occ_of(rec)
        oid = _get(rec, "option_id", "option", "instrument_id", default=None)
        if oid is None:
            raise BrokerMalformed("position record has no option id to resolve its contract")
        return await self._occ_from_option_id(str(oid))

    # ── order verbs ───────────────────────────────────────────────────────────
    async def _order_body(self, plan: OrderPlan, *, include_ref: bool) -> dict:
        self._require_account()
        iid = await self._instrument_id(plan.contract_symbol)
        side, effect = _INTENT_LEG[plan.intent]
        # Robinhood's option-order schema is MIXED-typed (both verified live
        # Sep 10 2026 via review_option_order -32602 errors): the top-level
        # `quantity` must be a STRING ("type: 1 has type integer, want string"),
        # but the leg's `ratio_quantity` must be an INTEGER ("has type string,
        # want integer"). price is a string, as before.
        legs = [{_LEG_OPTION_KEY: iid, "side": side, "position_effect": effect, "ratio_quantity": 1}]
        if plan.long_contract_symbol:
            # The wing rides in the SAME order, so the broker fills the vertical
            # atomically at the net price: never two single-leg orders, which
            # could leave a naked short if the wing did not fill.
            wing_iid = await self._instrument_id(plan.long_contract_symbol)
            wside, weffect = _INTENT_WING_LEG[plan.intent]
            legs.append({_LEG_OPTION_KEY: wing_iid, "side": wside, "position_effect": weffect, "ratio_quantity": 1})
        body: dict = {
            "account_number": self._account_id,
            "legs": legs,
            "direction": _INTENT_DIRECTION[plan.intent],
            "type": "limit",
            "quantity": str(plan.quantity),
            "price": str(plan.limit_price),
            # Day order by default. A resting take-profit close asks for "gtc"
            # (review accepted it live Sep 24 2026; a place-time refusal falls
            # back to a daily re-armed "gfd" in the scanner's close50 loop).
            "time_in_force": plan.time_in_force,
        }
        if include_ref:
            # The broker requires a UUID ref_id; derive it deterministically from
            # our client_order_id so the id stays stable across reprices/restarts.
            body["ref_id"] = broker_ref_id(plan.client_order_id)  # idempotency key echoed back on the order
        return body

    async def review(self, plan: OrderPlan) -> bool:
        if not self._tools.review:
            logger.info("executor: no review tool configured; skipping broker review for %s", plan.client_order_id)
            return True
        result = await self._call(self._tools.review, await self._order_body(plan, include_ref=False))
        if result.is_error:
            logger.info("executor: broker review rejected %s: %s", plan.client_order_id, result.content)
            return False
        return True

    async def place(self, plan: OrderPlan) -> BrokerOrder:
        from agentic_guardrails.mcp_client import MCPProtocolError

        try:
            result = await self._call(self._tools.place, await self._order_body(plan, include_ref=True))
        except BrokerMalformed as exc:
            # A protocol-level error on PLACE is the UNKNOWN class, not a clean
            # reject: the order may have landed. Re-raise as a generic BrokerError
            # so the engine fails closed to UNKNOWN and the reconciler resolves it.
            if isinstance(exc.__cause__, MCPProtocolError):
                raise BrokerError(f"place ambiguous: {exc}") from exc
            raise
        if result.is_error:
            raise BrokerRejected(f"place rejected: {result.content}")
        # Identify the created order by OUR ref_id, not by taking the first record.
        recs = _records(_data(result), "orders", "results", "order")
        if len(recs) == 1:
            return self._to_broker_order(recs[0])
        ref = broker_ref_id(plan.client_order_id)
        matches = [r for r in recs if str(_get(r, "ref_id", "client_order_id", default="")) == ref]
        if len(matches) != 1:
            # The order landed (no isError) but we cannot uniquely identify it:
            # the UNKNOWN class, not a reject. poll_working re-adopts it by ref_id.
            raise BrokerError(f"place response ambiguous: {len(recs)} orders, {len(matches)} match {plan.client_order_id}")
        return self._to_broker_order(matches[0])

    async def cancel(self, broker_order_id: str) -> BrokerOrder:
        result = await self._call(self._tools.cancel, {"account_number": self._account_id, "order_id": broker_order_id})
        if result.is_error:
            raise BrokerMalformed(f"cancel tool error for {broker_order_id}: {result.content}")
        for rec in _records(_data(result), "orders", "results", "order"):
            if str(_get(rec, "id", "order_id", default="")) == str(broker_order_id):
                return self._to_broker_order(rec)
        # Cancel returned an ack, not the order body: read the authoritative state.
        return await self.status(broker_order_id)

    async def status(self, broker_order_id: str) -> BrokerOrder:
        result = await self._call(self._tools.order_status, {"account_number": self._account_id, "order_id": broker_order_id})
        if result.is_error:
            raise BrokerMalformed(f"status tool error for {broker_order_id}: {result.content}")
        rec = _match_record(_data(result), broker_order_id, ("id", "order_id"), ("orders", "results", "order"))
        return self._to_broker_order(rec)

    async def find_broker_order_id(self, client_order_id: str) -> str | None:
        """List orders and return the broker id of the one carrying OUR ref_id,
        or None if not present yet. The recovery path for an ambiguous placement
        (place timed out / returned unparseably): the order may have landed under
        our deterministic ref_id, and this is how poll_working adopts it instead
        of leaving it UNKNOWN forever (docs plan 4.4; PR #476). Never places or
        retries; a not-found returns None so the next tick retries."""
        self._require_account()
        result = await self._call(self._tools.order_status, {"account_number": self._account_id})
        if result.is_error:
            raise BrokerMalformed(f"order-list tool error: {result.content}")
        ref = broker_ref_id(client_order_id)
        matches = []
        for rec in _records(_data(result), "orders", "results", "order"):
            if str(_get(rec, "ref_id", "client_order_id", default="")) == ref:
                boid = _get(rec, "id", "order_id", default=None)
                if boid is not None:
                    matches.append(str(boid))
        if len(matches) > 1:
            raise BrokerMalformed(f"{len(matches)} orders carry ref_id {client_order_id}; ambiguous")
        return matches[0] if matches else None

    def _to_broker_order(self, rec: dict) -> BrokerOrder:
        boid = _get(rec, "id", "order_id", default=None)
        raw_state = str(_get(rec, "state", "status", default="")).lower()
        state = _STATE_MAP.get(raw_state, OrderState.UNKNOWN)
        if raw_state and state == OrderState.UNKNOWN:
            logger.warning("executor: unmapped Robinhood order state %r -> UNKNOWN (fail closed)", raw_state)
        filled = int(_dec(_get(rec, "processed_quantity", "cumulative_quantity", "filled_quantity", default=0), field="filled_qty"))
        avg = _realized_fill_price(rec, filled)
        return BrokerOrder(
            broker_order_id=(str(boid) if boid is not None else None), state=state, filled_qty=filled, avg_fill_price=avg, raw=rec
        )
