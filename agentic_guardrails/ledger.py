"""Executor ledger: our durable record of every order and its transitions (Phase 1 of
docs/ROBINHOOD_MCP_EXECUTION_PLAN.md section 3.1).

Two store implementations behind one small Protocol so the engine is testable with no DB:
`InMemoryLedger` (tests + shadow mode) and `SqlLedger` (async SQLAlchemy Core over the
`options_live_orders` / `options_live_order_events` tables, migration 074). Positions are DERIVED
from filled orders (net short quantity per contract), never stored separately, so the ledger cannot
disagree with itself; the reconciler compares that derivation to the broker's positions read.

Invariants carried from the copy book: booked size comes only from the
broker-confirmed high-water fill (`OrderView.filled_qty`), an event row is written for EVERY state
transition with the verbatim broker payload, and a restart re-derives the same client_order_id so
the ledger row is re-adopted rather than duplicated (`upsert` is keyed on it).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Protocol

from agentic_guardrails.state_machine import OrderView
from agentic_guardrails.types import OPENING_INTENTS, SPREAD_INTENTS, OrderIntent, OrderPlan, OrderState


@dataclass(frozen=True)
class OrderRecord:
    client_order_id: str
    strategy: str
    contract_symbol: str
    intent: OrderIntent
    quantity: int
    limit_price: Decimal
    strike: Decimal
    expiration: date
    state: OrderState
    filled_qty: int
    avg_fill_price: Decimal | None
    broker_order_id: str | None
    basis_rule: str
    basis_source: str
    shadow: bool  # True = never sent to the broker (log-only placement)
    created_at: datetime
    updated_at: datetime
    # Phase 2 spreads: the wing. None on every single-leg record (migration 077
    # adds the columns nullable, so pre-existing rows read back as single-leg).
    long_contract_symbol: str | None = None
    long_strike: Decimal | None = None

    @classmethod
    def from_plan(cls, plan: OrderPlan, *, shadow: bool, now: datetime) -> OrderRecord:
        return cls(
            long_contract_symbol=plan.long_contract_symbol,
            long_strike=plan.long_strike,
            client_order_id=plan.client_order_id,
            strategy=plan.strategy,
            contract_symbol=plan.contract_symbol,
            intent=plan.intent,
            quantity=plan.quantity,
            limit_price=plan.limit_price,
            strike=plan.strike,
            expiration=plan.expiration,
            state=OrderState.PLANNED,
            filled_qty=0,
            avg_fill_price=None,
            broker_order_id=None,
            basis_rule=plan.basis.rule,
            basis_source=plan.basis.quote_source.value,
            shadow=shadow,
            created_at=now,
            updated_at=now,
        )

    def with_view(self, view: OrderView, *, broker_order_id: str | None, now: datetime) -> OrderRecord:
        return replace(
            self,
            state=view.current,
            filled_qty=view.filled_qty,
            avg_fill_price=view.avg_fill_price,
            broker_order_id=broker_order_id if broker_order_id is not None else self.broker_order_id,
            updated_at=now,
        )

    def view(self) -> OrderView:
        return OrderView(current=self.state, filled_qty=self.filled_qty, avg_fill_price=self.avg_fill_price)

    @property
    def is_spread(self) -> bool:
        return self.intent in SPREAD_INTENTS

    def width(self) -> Decimal:
        return (self.strike - self.long_strike) if self.long_strike is not None else Decimal(0)

    def collateral_per_contract(self) -> Decimal:
        """What ONE contract of this record ties up: strike x 100 for a cash-secured
        short, width x 100 for a vertical (the defined max loss, gross of credit).
        Mirrors OrderPlan.collateral so the ledger and the plan can never disagree
        about what a filled order commits."""
        per = self.width() if self.is_spread else self.strike
        return per * Decimal(100)

    def signed_filled(self) -> int:
        """The SHORT leg's signed fill: negative when sold to open, positive when
        bought to close. For a spread this is the short leg only; use leg_fills for
        both legs."""
        return -self.filled_qty if self.intent in OPENING_INTENTS else self.filled_qty

    def leg_fills(self) -> dict[str, int]:
        """Signed filled contracts per leg. A single-leg record is {short: signed}.
        A spread OPEN is {short: -q, wing: +q}; a spread CLOSE reverses both. The
        wing is a LONG (positive) position, which is what lets net_positions and
        the reconciler see the broker's two legs as two legs."""
        out = {self.contract_symbol: self.signed_filled()}
        if self.long_contract_symbol:
            out[self.long_contract_symbol] = -self.signed_filled()
        return out


@dataclass(frozen=True)
class OrderEvent:
    client_order_id: str
    from_state: OrderState
    to_state: OrderState
    filled_qty: int
    avg_fill_price: Decimal | None
    at: datetime
    raw: dict = field(default_factory=dict)


def net_positions(records: list[OrderRecord]) -> dict[str, int]:
    """Signed net contracts per OCC symbol from filled quantities (shadow rows excluded: they
    were never sent, so the broker cannot hold them)."""
    out: dict[str, int] = {}
    for r in records:
        if r.shadow or r.filled_qty <= 0:
            continue
        for sym, q in r.leg_fills().items():
            out[sym] = out.get(sym, 0) + q
    return {k: v for k, v in out.items() if v != 0}


def _structure_key(r: OrderRecord) -> tuple[str, str | None]:
    """The identity a close nets against: the short leg for a single-leg order,
    the (short, wing) PAIR for a vertical. Two spreads sharing a short but
    wearing different wings are two structures, so closing one must not read
    the other as closed (or as still open, Devin's PR #529 finding)."""
    return (r.contract_symbol, r.long_contract_symbol)


def open_contracts_by_record(records: list[OrderRecord]) -> dict[str, int]:
    """client_order_id -> how many of that OPENING record's contracts are still
    open. Closes are netted against opens of the SAME structure key and applied
    to the OLDEST opens first (FIFO), so the residual open contracts sit on the
    newest opening records and two opens of one structure never both claim the
    same residual. A close that
    does not share the structure key (e.g. the assignment guard buying back a
    spread's short leg alone) leaves the spread reading OPEN here: that is the
    conservative direction for collateral and concurrency (net_positions still
    shows the true per-leg book for the reconciler)."""
    residual: dict[tuple[str, str | None], int] = {}
    for r in records:
        if r.shadow or r.filled_qty <= 0:
            continue
        k = _structure_key(r)
        residual[k] = residual.get(k, 0) + r.signed_filled()
    remaining = {k: -v for k, v in residual.items() if v < 0}
    out: dict[str, int] = {}
    for r in sorted(records, key=lambda x: (x.created_at, x.client_order_id), reverse=True):
        if r.shadow or r.intent not in OPENING_INTENTS or r.filled_qty <= 0:
            continue
        k = _structure_key(r)
        left = remaining.get(k, 0)
        if left <= 0:
            continue
        n = min(r.filled_qty, left)
        remaining[k] = left - n
        out[r.client_order_id] = n
    return out


def open_collateral(records: list[OrderRecord], *, ticker_of=lambda sym: sym) -> tuple[Decimal, dict[str, Decimal], dict[str, Decimal]]:
    """(total, by_ticker, by_strategy) collateral tied up by net-short filled positions.
    A vertical commits its width (max loss), a cash-secured short its strike."""
    total = Decimal(0)
    by_ticker: dict[str, Decimal] = {}
    by_strategy: dict[str, Decimal] = {}
    open_by = open_contracts_by_record(records)
    for r in records:
        n = open_by.get(r.client_order_id, 0)
        if n <= 0:
            continue
        c = r.collateral_per_contract() * Decimal(n)
        total += c
        t = ticker_of(r.contract_symbol)
        by_ticker[t] = by_ticker.get(t, Decimal(0)) + c
        by_strategy[r.strategy] = by_strategy.get(r.strategy, Decimal(0)) + c
    return total, by_ticker, by_strategy


# REVIEWED is deliberately absent. Engine.submit holds its lock from context
# through settle, so a REVIEWED row any other submission can see is one whose
# placement was interrupted (no broker order exists), and poll_working never
# resolves REVIEWED: counting it would reserve collateral forever (Devin, PR #629).
_INFLIGHT_STATES = frozenset({OrderState.PLACED, OrderState.PARTIAL, OrderState.UNKNOWN})


def inflight_open_remainders(records: list[OrderRecord], *, exclude: str | None = None) -> list[tuple[OrderRecord, int]]:
    """(record, unfilled contracts) for every live opening order that may still fill.

    The collateral caps read FILLED positions, so an open that is reviewed,
    resting, partially filled or UNKNOWN commits nothing to them until it
    settles. Within one scan tick the loop submits several opens, and once
    the second was judged before the first settled, so the NVDA factor
    overshot its cap by one order. Counting the unfilled remainder of every placed
    open closes that gap: an order that might still fill is treated as if it
    will. UNKNOWN is included on purpose (it may have filled at the broker).
    `exclude` drops the plan being judged, so it never counts against itself."""
    out: list[tuple[OrderRecord, int]] = []
    for r in records:
        if r.shadow or r.intent not in OPENING_INTENTS or r.state not in _INFLIGHT_STATES:
            continue
        if exclude is not None and r.client_order_id == exclude:
            continue
        left = int(r.quantity) - int(r.filled_qty)
        if left > 0:
            out.append((r, left))
    return out


def open_collateral_by_expiry(records: list[OrderRecord]) -> dict[date, Decimal]:
    """Open short collateral keyed by expiration date: the input to the expiry
    concentration cap. Same per-contract collateral as
    open_collateral (width for a vertical, strike for a cash-secured put), only
    the grouping key differs. A record with no expiration is skipped (it cannot
    be placed on the date axis; the OCC parser refuses such plans anyway)."""
    out: dict[date, Decimal] = {}
    open_by = open_contracts_by_record(records)
    for r in records:
        n = open_by.get(r.client_order_id, 0)
        if n <= 0 or r.expiration is None:
            continue
        out[r.expiration] = out.get(r.expiration, Decimal(0)) + r.collateral_per_contract() * Decimal(n)
    return out


def open_spreads(records: list[OrderRecord]) -> dict[str, int]:
    """Open spread CONTRACTS per strategy (a 3-lot vertical counts 3). The input
    to the concurrency cap that the spreads finding makes the binding risk
    control: the spread bounds loss PER TRADE and does nothing about how many
    trades lose at once, so the number open at once is what must be capped."""
    open_by = open_contracts_by_record(records)
    out: dict[str, int] = {}
    for r in records:
        if not r.is_spread:
            continue
        n = open_by.get(r.client_order_id, 0)
        if n > 0:
            out[r.strategy] = out.get(r.strategy, 0) + n
    return out


class LedgerStore(Protocol):
    async def get(self, client_order_id: str) -> OrderRecord | None: ...
    async def upsert(self, rec: OrderRecord) -> None: ...
    async def append_event(self, ev: OrderEvent) -> None: ...
    async def all_orders(self) -> list[OrderRecord]: ...
    async def working_orders(self) -> list[OrderRecord]: ...


class InMemoryLedger:
    """Dict-backed store for tests and shadow mode."""

    def __init__(self) -> None:
        self.orders: dict[str, OrderRecord] = {}
        self.events: list[OrderEvent] = []

    async def get(self, client_order_id: str) -> OrderRecord | None:
        return self.orders.get(client_order_id)

    async def upsert(self, rec: OrderRecord) -> None:
        self.orders[rec.client_order_id] = rec

    async def append_event(self, ev: OrderEvent) -> None:
        self.events.append(ev)

    async def all_orders(self) -> list[OrderRecord]:
        return list(self.orders.values())

    async def working_orders(self) -> list[OrderRecord]:
        return [r for r in self.orders.values() if r.state in (OrderState.PLACED, OrderState.PARTIAL, OrderState.UNKNOWN) and not r.shadow]


class SqlLedger:
    """Async SQLAlchemy Core store over options_live_orders / options_live_order_events."""

    def __init__(self, session) -> None:
        self._s = session

    @staticmethod
    def _row_to_rec(r) -> OrderRecord:
        return OrderRecord(
            client_order_id=r.client_order_id,
            strategy=r.strategy,
            contract_symbol=r.contract_symbol,
            intent=OrderIntent(r.intent),
            quantity=int(r.quantity),
            limit_price=Decimal(str(r.limit_price)),
            strike=Decimal(str(r.strike)),
            expiration=r.expiration,
            state=OrderState(r.state),
            filled_qty=int(r.filled_qty),
            avg_fill_price=Decimal(str(r.avg_fill_price)) if r.avg_fill_price is not None else None,
            broker_order_id=r.broker_order_id,
            basis_rule=r.basis_rule,
            basis_source=r.basis_source,
            shadow=bool(r.shadow),
            created_at=r.created_at,
            updated_at=r.updated_at,
            long_contract_symbol=getattr(r, "long_contract_symbol", None),
            long_strike=Decimal(str(r.long_strike)) if getattr(r, "long_strike", None) is not None else None,
        )

    async def get(self, client_order_id: str) -> OrderRecord | None:
        from sqlalchemy import text

        r = (await self._s.execute(text("SELECT * FROM options_live_orders WHERE client_order_id = :c"), {"c": client_order_id})).first()
        return self._row_to_rec(r) if r else None

    async def upsert(self, rec: OrderRecord) -> None:
        from sqlalchemy import text

        await self._s.execute(
            text(
                """INSERT INTO options_live_orders
                   (client_order_id, strategy, contract_symbol, intent, quantity, limit_price, strike, expiration,
                    state, filled_qty, avg_fill_price, broker_order_id, basis_rule, basis_source, shadow, created_at, updated_at,
                    long_contract_symbol, long_strike)
                   VALUES (:client_order_id, :strategy, :contract_symbol, :intent, :quantity, :limit_price, :strike, :expiration,
                    :state, :filled_qty, :avg_fill_price, :broker_order_id, :basis_rule, :basis_source, :shadow, :created_at, :updated_at,
                    :long_contract_symbol, :long_strike)
                   ON CONFLICT (client_order_id) DO UPDATE SET
                    state=excluded.state, filled_qty=GREATEST(options_live_orders.filled_qty, excluded.filled_qty),
                    avg_fill_price=excluded.avg_fill_price, broker_order_id=COALESCE(excluded.broker_order_id, options_live_orders.broker_order_id),
                    updated_at=excluded.updated_at"""
            ),
            {
                "client_order_id": rec.client_order_id,
                "strategy": rec.strategy,
                "contract_symbol": rec.contract_symbol,
                "intent": rec.intent.value,
                "quantity": rec.quantity,
                "limit_price": rec.limit_price,
                "strike": rec.strike,
                "expiration": rec.expiration,
                "state": rec.state.value,
                "filled_qty": rec.filled_qty,
                "avg_fill_price": rec.avg_fill_price,
                "broker_order_id": rec.broker_order_id,
                "basis_rule": rec.basis_rule,
                "basis_source": rec.basis_source,
                "shadow": rec.shadow,
                "created_at": rec.created_at,
                "updated_at": rec.updated_at,
                "long_contract_symbol": rec.long_contract_symbol,
                "long_strike": rec.long_strike,
            },
        )
        await self._s.commit()

    async def append_event(self, ev: OrderEvent) -> None:
        from sqlalchemy import text

        await self._s.execute(
            text(
                """INSERT INTO options_live_order_events (client_order_id, from_state, to_state, filled_qty, avg_fill_price, at, raw)
                   VALUES (:c, :f, :t, :q, :p, :at, cast(:raw as json))"""
            ),
            {
                "c": ev.client_order_id,
                "f": ev.from_state.value,
                "t": ev.to_state.value,
                "q": ev.filled_qty,
                "p": ev.avg_fill_price,
                "at": ev.at,
                "raw": json.dumps(ev.raw, default=str),
            },
        )
        await self._s.commit()

    async def all_orders(self) -> list[OrderRecord]:
        from sqlalchemy import text

        rows = (await self._s.execute(text("SELECT * FROM options_live_orders"))).all()
        return [self._row_to_rec(r) for r in rows]

    async def working_orders(self) -> list[OrderRecord]:
        from sqlalchemy import text

        rows = (
            await self._s.execute(text("SELECT * FROM options_live_orders WHERE state IN ('placed','partial','unknown') AND NOT shadow"))
        ).all()
        return [self._row_to_rec(r) for r in rows]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
