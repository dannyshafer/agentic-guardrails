"""The assignment-guard loop: the runtime that turns `assignment_guard.decide` into orders.

For each open SHORT PUT at the broker it builds the policy's snapshot (live quote greeks, a
two-feed spot, roll candidates), runs the pure policy and acts on a non-HOLD decision: CLOSE is
a marketable buy-to-close at the ask (a fill matters more than a cent of price, for the
never-assigned guarantee); ROLL is the same close first, then a best-effort sell of the chosen
candidate through the engine's full guard set, so a refused roll leaves you safely flat.

What it carries from the live book, so a deployment does not re-learn it:

- Shadow by default (`armed=False`): every non-HOLD decision goes to the alert hook, nothing
  is placed. Arming is a deliberate flag.
- A protective order after hours cannot fill; it is deferred to the next session and surfaced
  once per contract per day, not every tick.
- The close's client order id walks an attempt suffix: a terminal unfilled attempt (canceled
  after hours, rejected) mints a fresh id next tick instead of being refused by the engine's
  idempotency check forever (incident 3). Each retry pays a little more above the ask.
- A resting take-profit on the contract is cancelled first and the position re-read, or the
  guard's close is refused as a second in-flight order, or both fill and over-close into a
  long put.
- An OCC-adjusted short (reverse split) is watched and paged, never traded.
- Every error is isolated per position; a tick never crashes on one bad read.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import ROUND_UP, Decimal
from typing import Awaitable, Callable

from agentic_guardrails import assignment_guard as ag
from agentic_guardrails.broker import parse_occ
from agentic_guardrails.engine import Engine
from agentic_guardrails.ids import client_order_id
from agentic_guardrails.runtime.kill import guard_kill_state
from agentic_guardrails.runtime.spot import guard_spot
from agentic_guardrails.state_machine import is_terminal
from agentic_guardrails.types import OrderIntent, OrderPlan, PriceBasis, QuoteSource

logger = logging.getLogger(__name__)

GUARD_STRATEGY = "assign_guard"


def close_limit(ask: Decimal, coid: str) -> Decimal:
    """Buy-to-close limit: the ask on the first attempt; each retry (`_rN` suffix) reaches 5%
    (at least a nickel) further above it, the total pad capped at +50% of the ask or $0.10 on a
    sub-$0.20 ask. A buy fills at the best available ask, so the pad only costs when needed."""
    m = re.search(r"_r(\d+)$", coid or "")
    n = min(int(m.group(1)) if m else 0, 10)
    ask = Decimal(str(ask))
    pad = min(max(ask * Decimal("0.05"), Decimal("0.05")) * n, max(ask * Decimal("0.50"), Decimal("0.10")))
    return (ask + pad).quantize(Decimal("0.01"), rounding=ROUND_UP)


async def next_close_coid(ledger, contract_symbol: str, intent: OrderIntent, today: date, qty: int, max_attempts: int = 60):
    """(coid, None) to place, or (None, reason) to skip this tick. Walks base, base_r1, ...:
    absent -> place; in flight -> wait; filled >= qty -> done; terminal unfilled -> next id."""
    base = client_order_id(GUARD_STRATEGY, contract_symbol, intent, today)
    for n in range(max_attempts):
        coid = base if n == 0 else f"{base}_r{n}"
        rec = await ledger.get(coid)
        if rec is None:
            return coid, None
        if not is_terminal(rec.state):
            return None, f"an order ({coid}) is already in flight (state {rec.state.value}); waiting"
        if rec.filled_qty >= qty:
            return None, f"already closed by {coid} (filled {rec.filled_qty}); no further action"
    return None, f"exhausted {max_attempts} close attempts for {contract_symbol} today (all canceled unfilled), needs a human"


async def _noop_alert(text: str) -> None:
    logger.info("guard alert: %s", text)


async def _no_spot(ticker: str):
    return None, None, None


async def _no_candidates(ticker: str, spot):
    return []


@dataclass
class GuardLoopHooks:
    """What the loop needs from its deployment, as async callables.

    `spot(ticker) -> (primary, secondary, secondary_age_sec)`: two independent underlying
    prices (either may be None); `roll_candidates(ticker, spot) -> [RollCandidate]`;
    `alert(text)`; `market_open() -> bool` (a protective order after hours is deferred)."""

    spot: Callable[[str], Awaitable[tuple]] = _no_spot
    roll_candidates: Callable[[str, Decimal | None], Awaitable[list]] = _no_candidates
    alert: Callable[[str], Awaitable[None]] = _noop_alert
    market_open: Callable[[], bool] = lambda: True


@dataclass
class AssignmentGuardLoop:
    broker: object
    ledger: object
    engine: Engine
    hooks: GuardLoopHooks = field(default_factory=GuardLoopHooks)
    config: ag.GuardConfig = field(default_factory=ag.GuardConfig)
    armed: bool = False
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    _deferred: set = field(default_factory=set)
    _adjusted_paged: set = field(default_factory=set)

    async def tick(self) -> list[ag.GuardDecision]:
        """One pass over the broker's positions. Returns the decisions taken (HOLD included)."""
        try:
            positions = await self.broker.positions()
        except Exception:  # noqa: BLE001 - a tick never crashes on a read
            logger.exception("assignment guard: could not read positions; skipping tick")
            return []
        today = self.clock().date()
        out = []
        for pos in positions:
            try:
                d = await self._evaluate(pos, today)
                if d is not None:
                    out.append(d)
            except Exception:  # noqa: BLE001 - isolate per position
                logger.exception("assignment guard: error evaluating %s", getattr(pos, "contract_symbol", "?"))
        return out

    async def _evaluate(self, pos, today: date):
        if pos.quantity >= 0:
            return None
        if getattr(pos, "adjusted", False):
            key = (pos.contract_symbol, today)
            if key not in self._adjusted_paged:
                self._adjusted_paged.add(key)
                await self.hooks.alert(
                    f"assignment guard: {pos.contract_symbol} is an OCC-adjusted short; watched, never traded. Close it by hand."
                )
            return None
        occ = parse_occ(pos.contract_symbol)
        if occ["type"] != "put":
            return None
        ticker, strike = occ["root"], occ["strike"]
        expiration = date.fromisoformat(occ["expiration"])
        q = await self.broker.quote(pos.contract_symbol)
        primary, secondary, age = await self.hooks.spot(ticker)
        spot, _, _ = guard_spot(primary, secondary, age)
        extrinsic = None
        if q.mark is not None and spot is not None:
            extrinsic = q.mark - max(Decimal(0), strike - spot)
        candidates = await self.hooks.roll_candidates(ticker, spot)
        snap = ag.PositionSnapshot(
            contract_symbol=pos.contract_symbol,
            strike=strike,
            expiration=expiration,
            quantity=pos.quantity,
            underlying_spot=spot,
            put_delta=q.delta,
            close_ask=q.ask,
            extrinsic=extrinsic,
        )
        decision = ag.decide(snap, candidates, today, self.config)
        logger.info("assignment guard %s: %s, %s", pos.contract_symbol, decision.action.value, decision.reason)
        if decision.action != ag.GuardAction.HOLD:
            await self._act(pos, decision, q, today, expiration)
        return decision

    async def _act(self, pos, decision, q, today: date, expiration: date) -> None:
        verb = decision.action.value.upper()
        acts = decision.action in (ag.GuardAction.CLOSE, ag.GuardAction.ROLL)
        if self.armed and acts and not self.hooks.market_open():
            key = (pos.contract_symbol, today)
            if key not in self._deferred:
                self._deferred.add(key)
                await self.hooks.alert(
                    f"assignment guard {pos.contract_symbol}: would {verb} but the market is closed; deferring to the next session ({decision.reason})"
                )
            return
        mode = "ARMED" if self.armed else "SHADOW"
        await self.hooks.alert(f"assignment guard [{mode}] {verb} {pos.contract_symbol}: {decision.reason}")
        if self.armed and acts:
            await self._execute(pos, decision, q, today, expiration)

    async def _execute(self, pos, decision, q, today: date, expiration: date) -> None:
        qty = abs(pos.quantity)
        now = self.clock()
        kill = guard_kill_state(now)
        qty = await self._clear_resting(pos, qty)
        if qty is None:
            return
        coid, skip = await next_close_coid(self.ledger, pos.contract_symbol, OrderIntent.BTC, today, qty)
        if coid is None:
            await self.hooks.alert(f"assignment guard CLOSE {pos.contract_symbol}: {skip}")
            return
        limit = await self._tick_round(pos.contract_symbol, close_limit(q.ask, coid))
        btc = OrderPlan(
            client_order_id=coid,
            strategy=GUARD_STRATEGY,
            contract_symbol=pos.contract_symbol,
            intent=OrderIntent.BTC,
            quantity=qty,
            limit_price=limit,
            strike=Decimal(str(parse_occ(pos.contract_symbol)["strike"])),
            expiration=expiration,
            basis=PriceBasis("btc_marketable_ask", q.source, q.bid, q.ask, now),
            created_at=now,
        )
        res = await self.engine.submit(btc, kill)
        rec = res.record
        filled = rec.filled_qty if rec else 0
        urgent = "NEEDS A HUMAN: " if filled < qty and _attempt(coid) >= 2 else ""
        await self.hooks.alert(
            f"{urgent}assignment guard CLOSE {pos.contract_symbol}: {res.reason} (state {rec.state.value if rec else '?'}, filled {filled})"
        )
        if decision.action == ag.GuardAction.ROLL and decision.roll_to is not None and rec is not None and rec.filled_qty >= qty:
            await self._roll(pos, decision, rec, qty, today, now, kill)

    async def _clear_resting(self, pos, qty: int):
        """Cancel resting take-profit closes on the contract, then re-read how much is still short.
        None means stop this tick (a cancel that did not take, an unreadable position, or flat)."""
        cleared = await self.engine.cancel_resting(pos.contract_symbol)
        if not cleared:
            return qty
        stuck = [r.client_order_id for r in cleared if not is_terminal(r.state)]
        if stuck:
            await self.hooks.alert(f"assignment guard {pos.contract_symbol}: resting close(s) {stuck} did not cancel; retrying next tick")
            return None
        try:
            live = await self.broker.positions()
        except Exception:  # noqa: BLE001
            logger.exception("assignment guard: position re-read after cancelling resting closes failed")
            return None
        left = abs(sum(p.quantity for p in live if p.contract_symbol == pos.contract_symbol and p.quantity < 0))
        return left if left > 0 else None

    async def _tick_round(self, contract_symbol: str, price: Decimal) -> Decimal:
        tick_fn = getattr(self.broker, "min_tick", None)
        if tick_fn is None:
            return price
        try:
            tick = await tick_fn(contract_symbol, price)
            return (price / tick).quantize(1, rounding=ROUND_UP) * tick
        except Exception:  # noqa: BLE001 - an unreadable tick falls back to the unrounded price
            logger.exception("assignment guard: tick lookup failed for %s", contract_symbol)
            return price

    async def _roll(self, pos, decision, rec, qty: int, today: date, now: datetime, kill) -> None:
        rt = decision.roll_to
        if rec.avg_fill_price is None or rt.credit_bid - rec.avg_fill_price < -self.config.max_roll_debit:
            await self.hooks.alert(
                f"assignment guard {pos.contract_symbol}: closed at {rec.avg_fill_price}; roll to {rt.contract_symbol} skipped (credit no longer covers the close). Flat."
            )
            return
        coid, skip = await next_close_coid(self.ledger, rt.contract_symbol, OrderIntent.STO, today, qty)
        if coid is None:
            await self.hooks.alert(f"assignment guard ROLL {rt.contract_symbol}: {skip}")
            return
        sto = OrderPlan(
            client_order_id=coid,
            strategy=GUARD_STRATEGY,
            contract_symbol=rt.contract_symbol,
            intent=OrderIntent.STO,
            quantity=qty,
            limit_price=rt.credit_bid,
            strike=rt.strike,
            expiration=rt.expiration,
            basis=PriceBasis("roll_sto_bid", QuoteSource.ROBINHOOD, rt.credit_bid, rt.credit_bid, now),
            created_at=now,
        )
        res = await self.engine.submit(sto, kill)
        await self.hooks.alert(
            f"assignment guard ROLL {rt.contract_symbol}: {res.reason} (filled {res.record.filled_qty if res.record else 0}); the close already made you safe"
        )


def _attempt(coid: str) -> int:
    m = re.search(r"_r(\d+)$", coid or "")
    return int(m.group(1)) if m else 0
