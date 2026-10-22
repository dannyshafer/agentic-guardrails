"""Executor engine: the I/O shell that runs the pure decision core against a BrokerAdapter and a
LedgerStore (Phase 1 of docs/ROBINHOOD_MCP_EXECUTION_PLAN.md).

Responsibilities, in the order a submission runs them:
  1. kill switch (`killswitch.evaluate_halt`) with freshness supplied by the caller;
  2. guards (`guards.evaluate`) with a context DERIVED from the ledger + a fresh account read;
  3. broker review (a REJECT there is terminal, nothing is sent);
  4. placement: SHADOW mode records the exact order it would have placed and stops (Phase 1 posture:
     reads real, placement stubbed); LIVE mode places and then polls status through
     `apply_broker_status` until terminal or the poll budget is spent, keeping the high-water fill;
  5. a read failure AFTER a place moves the order to UNKNOWN (fail closed: guards block further opens
     for that strategy until `poll_working` resolves it).
Every transition writes an event row with the verbatim broker payload. Positions and collateral for
the guards are derived from the ledger, never from a quote or an intended size.

No wiring into the bot yet: importing this starts nothing, and `OPTIONS_EXECUTOR_ENABLED` stays the
outer gate in the kill state the caller builds. The live transport (`mcp_client.py`) is Phase 0
work that needs the Robinhood account and is deliberately not here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from agentic_guardrails import guards, killswitch
from agentic_guardrails.broker import BrokerAdapter, BrokerError, BrokerRejected
from agentic_guardrails.ledger import (
    inflight_open_remainders,
    LedgerStore,
    OrderEvent,
    OrderRecord,
    net_positions,
    open_collateral,
    open_collateral_by_expiry,
    open_spreads,
    utcnow,
)
from agentic_guardrails.reconciler import DriftClass, Finding, reconcile
from agentic_guardrails.state_machine import IllegalTransition, OrderView, apply_broker_status, is_terminal, to_unknown
from agentic_guardrails.types import OPENING_INTENTS, RESTING_TP_RULE, BrokerOrder, OrderPlan, OrderState

logger = logging.getLogger(__name__)


class FactorModel(Protocol):
    """What the engine needs from a risk-factor model: the factor a traded symbol expresses (so
    the factor cap sees TQQQ and QLD as one bet, and the earnings guard asks about the underlier)
    and whether a MISSING earnings lookup for a symbol is a refusal (single name, or unknown) or
    benign (index or basket). `guards.FACTOR_OF` is the default; a user-supplied map (the
    package's `FactorMap`) describes a different universe without editing the core."""

    def factor_of(self, ticker: str) -> str: ...

    def earnings_underlier(self, ticker: str) -> str:
        """The symbol whose earnings calendar governs `ticker` (NVDL -> NVDA). Kept SEPARATE
        from `factor_of`: a risk group ("TECH") is not a company, so a model that pools
        wrappers under a custom factor must still name the company each one tracks."""
        ...

    def underlier_has_earnings(self, ticker: str) -> bool: ...


class _GuardsFactors:
    """The module-level table in `guards`, as a FactorModel. The default, so an EngineConfig
    without `factors` behaves exactly as before (there the factor IS the underlier)."""

    factor_of = staticmethod(guards.factor_of)
    earnings_underlier = staticmethod(guards.factor_of)
    underlier_has_earnings = staticmethod(guards.underlier_has_earnings)


@dataclass(frozen=True)
class EngineConfig:
    caps: guards.Caps
    shadow: bool = True  # Phase 1 default: never send an order
    poll_attempts: int = 8
    poll_delay_s: float = 0.0  # tests run with 0; live uses ~0.75s like the copy book's settle loop
    cancel_unfilled_after_poll: bool = True
    # Async callable ticker -> date | None returning the underlier's next
    # earnings date. REQUIRED for single-name wrappers: with none configured the
    # engine reports the lookup as FAILED for those tickers, so
    # guard_no_earnings_in_window refuses to open (fail closed). Index and basket
    # wrappers have no scheduled event and are unaffected. Deliberately injected
    # rather than imported so the executor core stays free of the app dep tree.
    earnings_provider: Callable[[str], Awaitable[date | None]] | None = None
    # The risk-factor model behind the factor cap and the earnings guard's underlier lookup.
    # None means `guards.FACTOR_OF`; inject a FactorModel to describe another universe. Both
    # guards read it through the engine, so an injected map is CONNECTED, never decorative.
    factors: FactorModel | None = None


@dataclass(frozen=True)
class SubmitResult:
    accepted: bool
    reason: str
    record: OrderRecord | None


def ticker_of(contract_symbol: str) -> str:
    """'O:TQQQ261016P00080000' -> 'TQQQ' (OCC root before the 6-digit date)."""
    s = contract_symbol.removeprefix("O:")
    for i, ch in enumerate(s):
        if ch.isdigit():
            return s[:i]
    return s


def _add_to(book: dict, key, amount: Decimal) -> None:
    book[key] = book.get(key, Decimal(0)) + amount


def _committed_today(records: list[OrderRecord], today: date) -> Decimal:
    return sum(
        (
            r.collateral_per_contract() * Decimal(r.filled_qty)
            for r in records
            if not r.shadow and r.intent in OPENING_INTENTS and r.filled_qty > 0 and r.created_at.date() == today
        ),
        Decimal(0),
    )


def _inflight_states_for_plan(records: list[OrderRecord], plan: OrderPlan) -> tuple[OrderState, ...]:
    """States of live, non-terminal orders touching EITHER leg of `plan`.

    One in flight per CONTRACT, on EITHER leg: a spread whose wing is already
    working in another order (or a single-leg order on a contract this spread
    wants as its wing) is an overlap, not a new order."""
    legs = set(plan.legs())
    return tuple(
        r.state
        for r in records
        if (r.contract_symbol in legs or (r.long_contract_symbol in legs if r.long_contract_symbol else False))
        and not r.shadow
        and not is_terminal(r.state)
        and r.state != OrderState.PLANNED
        # A resting take-profit close of ANOTHER arm is not an overlap for this
        # arm's own resting close: two arms can hold one contract at different
        # credits, and each rests its own buy at its own 50%. Every other plan
        # (an open, the assignment guard's protective close) still sees it.
        and not (plan.basis.rule == RESTING_TP_RULE and r.basis_rule == RESTING_TP_RULE and r.strategy != plan.strategy)
    )


class Engine:
    def __init__(self, broker: BrokerAdapter, ledger: LedgerStore, config: EngineConfig, *, clock=utcnow) -> None:
        self.broker = broker
        self.ledger = ledger
        self.config = config
        self.clock = clock
        self._lock = asyncio.Lock()  # one submission at a time (the overlapping-cycle double-place class)

    @property
    def _factors(self) -> FactorModel:
        """Resolved from `self.config` on every use, because callers replace `engine.config`
        between submissions (caps, earnings provider) and the factor model must follow it."""
        return self.config.factors if self.config.factors is not None else _GuardsFactors()

    # ── context derivation ────────────────────────────────────────────────────

    async def _context(self, plan: OrderPlan) -> guards.ExecContext:
        records = await self.ledger.all_orders()
        total, by_ticker, by_strategy = open_collateral(records, ticker_of=ticker_of)
        today = self.clock().date()
        committed_today = _committed_today(records, today)
        unknown = any(r.strategy == plan.strategy and r.state == OrderState.UNKNOWN and not r.shadow for r in records)
        inflight = _inflight_states_for_plan(records, plan)
        spreads_open = open_spreads(records).get(plan.strategy, 0)
        acct = await self.broker.account()
        t = ticker_of(plan.contract_symbol)
        # Aggregate the per-ticker collateral onto risk factors, so the factor cap
        # sees TQQQ and QLD as one bet. Derived here rather than in
        # open_collateral so the ledger stays free of the risk model.
        by_factor: dict[str, Decimal] = {}
        for tk, amt in by_ticker.items():
            f = self._factors.factor_of(tk)
            by_factor[f] = by_factor.get(f, Decimal(0)) + amt
        # Same collateral keyed by expiration date, for the expiry cap: every
        # armed arm fires into the same weekly expiry on a scan day, and the
        # factor map cannot see that.
        by_expiry = open_collateral_by_expiry(records)
        # Live opens that have not settled still commit collateral: count their
        # unfilled remainder in every cap scope (the same-tick overshoot).
        for r, left in inflight_open_remainders(records, exclude=plan.client_order_id):
            c = r.collateral_per_contract() * Decimal(left)
            tk = ticker_of(r.contract_symbol)
            total += c
            _add_to(by_ticker, tk, c)
            _add_to(by_strategy, r.strategy, c)
            _add_to(by_factor, self._factors.factor_of(tk), c)
            _add_to(by_expiry, r.expiration, c)
            if r.created_at.date() == today:
                committed_today += c
        next_earnings, earnings_failed = await self._earnings(t)
        adjusted, adjusted_read_failed = await self._adjusted_contracts()
        return guards.ExecContext(
            free_cash=acct.free_cash,
            open_collateral_total=total,
            open_collateral_ticker=by_ticker.get(t, Decimal(0)),
            open_collateral_strategy=by_strategy.get(plan.strategy, Decimal(0)),
            committed_today=committed_today,
            ticker=t,
            strategy_has_unknown_order=unknown,
            inflight_states_for_contract=inflight,
            open_collateral_by_factor=by_factor,
            factor=self._factors.factor_of(t),
            open_collateral_by_expiry=by_expiry,
            next_earnings=next_earnings,
            earnings_lookup_failed=earnings_failed,
            expiration=plan.expiration,
            open_spreads_strategy=spreads_open,
            adjusted_contracts=adjusted,
            adjusted_read_failed=adjusted_read_failed,
        )

    async def _adjusted_contracts(self) -> tuple[frozenset[str], bool]:
        """(broker-held contracts the broker reports as OCC-adjusted, read_failed).

        Read on EVERY submission, opens and closes alike, so no automated path
        (close50, the assignment guard, the open loop) can place on a
        nonstandard-deliverable instrument. It runs inside _context, BEFORE broker
        review/placement, so it never sits between place and the settle polls.
        On a failed read the set is empty and read_failed is True: the guard then
        REFUSES an opening plan (fail closed) but lets a CLOSE through on the
        symbol-only check, because a protective buy-to-close must never be blocked
        by a broker read hiccup (the same reason closes skip the dead-man gate)."""
        try:
            positions = await self.broker.positions()
        except Exception:  # noqa: BLE001 — see docstring
            logger.warning("engine: positions read for adjusted-contract check failed", exc_info=True)
            return frozenset(), True
        return frozenset(p.contract_symbol for p in positions if getattr(p, "adjusted", False)), False

    async def _earnings(self, ticker: str) -> tuple[date | None, bool]:
        """(next_earnings, lookup_failed) for a ticker.

        Three cases, kept distinct because collapsing them is how a guard turns
        into a no-op: an index wrapper has NO event (None, False); a configured
        provider that raises or a single-name wrapper with NO provider is a
        FAILED lookup (None, True), which fails closed at the guard; a working
        provider returns its date.
        """
        if self.config.earnings_provider is None:
            return None, self._factors.underlier_has_earnings(ticker)
        # Ask about the UNDERLIER, not the wrapper. NVDL is an ETF and has no
        # earnings of its own, so querying it returns None, the lookup reads as a
        # successful "no event", and the guard permits a contract spanning NVDA's
        # report: the guard silently defeated for exactly the names it protects
        # (caught in review of PR #452). The scanner's own threshold checker has
        # always mapped through SINGLE_STOCK_UNDERLIERS for this reason; factor_of
        # is the executor-side equivalent.
        underlier = self._factors.earnings_underlier(ticker)
        try:
            # A concentrated sector wrapper (TECL -> NDX in FACTOR_OF) carries its
            # holdings' reports, which only the TRADED ticker identifies, so ask
            # about the ticker first. The provider answers a basket with its
            # earliest holding report (raising if any holding is unreadable) and
            # anything else with None, so the factor lookup below runs exactly as
            # before for every non-basket name (Devin, PR #571).
            when = await self.config.earnings_provider(ticker) if ticker != underlier else None
            if when is None:
                when = await self.config.earnings_provider(underlier)
        except Exception as exc:  # noqa: BLE001 — an unreadable event window must refuse, not crash
            logger.warning("earnings lookup failed for %s (underlier %s), standing down on opens: %s", ticker, underlier, exc)
            return None, True
        # A company ALWAYS has a next report, so None from the provider means the
        # feed could not tell us, not that no event exists, and "unreadable" is
        # the case this guard refuses. Only an index or basket legitimately has
        # no date. Without this, the NVDL bug generalizes: any ticker missing
        # from FACTOR_OF is queried by its own ETF symbol, returns None, and
        # silently loses the guard, so adding a wrapper to the watchlist without
        # a FACTOR_OF entry would quietly disarm it.
        if when is None and self._factors.underlier_has_earnings(ticker):
            logger.warning("no earnings date for %s (underlier %s); treating as unreadable and standing down on opens", ticker, underlier)
            return None, True
        return when, False

    async def _transition(self, rec: OrderRecord, view: OrderView, read: BrokerOrder | None) -> OrderRecord:
        now = self.clock()
        new = rec.with_view(view, broker_order_id=(read.broker_order_id if read else None), now=now)
        if new.state != rec.state or new.filled_qty != rec.filled_qty:
            await self.ledger.append_event(
                OrderEvent(
                    rec.client_order_id, rec.state, new.state, new.filled_qty, new.avg_fill_price, now, raw=(read.raw if read else {})
                )
            )
        await self.ledger.upsert(new)
        return new

    # ── submission ────────────────────────────────────────────────────────────

    async def submit(self, plan: OrderPlan, kill: killswitch.KillState) -> SubmitResult:
        async with self._lock:
            halt = killswitch.evaluate_halt(kill, self.clock())
            if halt.halted:
                return SubmitResult(False, halt.reason, None)
            existing = await self.ledger.get(plan.client_order_id)
            if existing is not None and (existing.state != OrderState.PLANNED or existing.shadow):
                return SubmitResult(False, f"already tracked in state {existing.state.value}", existing)
            ctx = await self._context(plan)
            g = guards.evaluate(plan, ctx, self.config.caps)
            if not g.allow:
                return SubmitResult(False, g.reason, None)
            now = self.clock()
            rec = OrderRecord.from_plan(plan, shadow=self.config.shadow, now=now)
            await self.ledger.upsert(rec)
            try:
                ok = await self.broker.review(plan)
            except BrokerError as exc:
                return SubmitResult(False, f"review failed: {exc}", rec)
            if not ok:
                rec = await self._transition(rec, OrderView(OrderState.REJECTED, 0, None), None)
                return SubmitResult(False, "broker review rejected", rec)
            rec = await self._transition(rec, OrderView(OrderState.REVIEWED, 0, None), None)
            if self.config.shadow:
                logger.info(
                    "SHADOW executor: would place %s %s%s x%d @ %s (%s)",
                    plan.intent.value,
                    plan.contract_symbol,
                    f"/{plan.long_contract_symbol}" if plan.long_contract_symbol else "",
                    plan.quantity,
                    plan.limit_price,
                    plan.client_order_id,
                )
                return SubmitResult(True, "shadow: recorded, not sent", rec)
            try:
                read = await self.broker.place(plan)
            except BrokerRejected as exc:
                rec = await self._transition(rec, OrderView(OrderState.REJECTED, 0, None), None)
                return SubmitResult(False, f"rejected: {exc}", rec)
            except BrokerError as exc:
                # The order may exist at the broker: fail closed as UNKNOWN; poll_working re-adopts it.
                rec = await self._transition(rec, to_unknown(OrderView(OrderState.PLACED, 0, None)), None)
                return SubmitResult(True, f"placed?: {exc}; UNKNOWN until reconciled", rec)
            view = apply_broker_status(rec.view(), read)
            rec = await self._transition(rec, view, read)
            rec = await self._settle(rec)
            return SubmitResult(True, f"final state {rec.state.value}, filled {rec.filled_qty}", rec)

    async def _settle(self, rec: OrderRecord) -> OrderRecord:
        """Poll to a terminal state; cancel the remainder after the budget; keep the high-water fill."""
        view = rec.view()
        boid = rec.broker_order_id
        if boid is None:
            return rec
        for _ in range(self.config.poll_attempts):
            if is_terminal(view.current):
                break
            try:
                read = await self.broker.status(boid)
            except BrokerError:
                view = to_unknown(view)
                return await self._transition(rec, view, None)
            try:
                view = apply_broker_status(view, read)
            except IllegalTransition:
                view = to_unknown(view)
                return await self._transition(rec, view, read)
            rec = await self._transition(rec, view, read)
            if self.config.poll_delay_s:
                await asyncio.sleep(self.config.poll_delay_s)
        # A resting take-profit close is MEANT to stay working at the broker; never
        # cancel it for not filling inside the settle budget.
        resting = rec.basis_rule == RESTING_TP_RULE
        if not is_terminal(view.current) and self.config.cancel_unfilled_after_poll and not resting:
            try:
                read = await self.broker.cancel(boid)
                view = apply_broker_status(view, read)
                rec = await self._transition(rec, view, read)
                # A cancel is a request: re-read once more for the slow-fill-after-cancel class.
                read = await self.broker.status(boid)
                view = apply_broker_status(view, read)
                rec = await self._transition(rec, view, read)
            except (BrokerError, IllegalTransition):
                view = to_unknown(view)
                rec = await self._transition(rec, view, None)
        return rec

    # ── maintenance ───────────────────────────────────────────────────────────

    async def cancel_resting(self, contract_symbol: str, strategy: str | None = None) -> list[OrderRecord]:
        """Cancel every working RESTING take-profit close on `contract_symbol`
        and return the records as re-read afterwards. The assignment guard calls
        this before its protective close: a resting buy left working beside the
        guard's buy could fill too and over-close into a long put, and while it
        works guard_one_in_flight refuses the guard's close. A cancel that fails
        leaves the record non-terminal, so the caller can see it did not clear.
        `strategy` narrows it to one arm's close, leaving another arm's resting
        close on the same contract working."""
        out = []
        async with self._lock:
            for rec in await self.ledger.all_orders():
                if rec.contract_symbol != contract_symbol or rec.shadow or rec.basis_rule != RESTING_TP_RULE:
                    continue
                if strategy is not None and rec.strategy != strategy:
                    continue
                if is_terminal(rec.state) or rec.state == OrderState.PLANNED or rec.broker_order_id is None:
                    continue
                view = rec.view()
                try:
                    read = await self.broker.cancel(rec.broker_order_id)
                    view = apply_broker_status(view, read)
                    rec = await self._transition(rec, view, read)
                    read = await self.broker.status(rec.broker_order_id)
                    view = apply_broker_status(view, read)
                    rec = await self._transition(rec, view, read)
                except (BrokerError, IllegalTransition) as exc:
                    logger.warning("executor cancel_resting: %s stays %s (%s)", rec.client_order_id, rec.state.value, exc)
                out.append(rec)
        return out

    async def poll_working(self) -> list[OrderRecord]:
        """Re-read every non-terminal order (PLACED/PARTIAL/UNKNOWN). Resolves UNKNOWN by broker id when
        we have one; an UNKNOWN with no broker id (an ambiguous placement) is ADOPTED by looking the
        order up by our deterministic client_order_id, so it can never sit UNKNOWN forever."""
        out = []
        for rec in await self.ledger.working_orders():
            if rec.broker_order_id is None:
                # Ambiguous placement recovery (docs plan 4.4): the order may have
                # landed under our client_order_id. Find its broker id and adopt
                # it; if not present yet, leave it for the next tick (never place).
                try:
                    boid = await self.broker.find_broker_order_id(rec.client_order_id)
                except BrokerError as exc:
                    logger.warning("executor poll_working: adopt lookup failed for %s (%s)", rec.client_order_id, exc)
                    out.append(rec)
                    continue
                if boid is None:
                    out.append(rec)
                    continue
                rec = await self._transition(
                    rec, rec.view(), BrokerOrder(broker_order_id=boid, state=rec.state, filled_qty=rec.filled_qty, avg_fill_price=None)
                )
            try:
                read = await self.broker.status(rec.broker_order_id)
                view = rec.view()
                if view.current == OrderState.UNKNOWN:
                    view = OrderView(OrderState.PLACED, view.filled_qty, view.avg_fill_price)
                view = apply_broker_status(view, read)
                rec = await self._transition(rec, view, read)
            except (BrokerError, IllegalTransition) as exc:
                logger.warning("executor poll_working: %s stays %s (%s)", rec.client_order_id, rec.state.value, exc)
            out.append(rec)
        return out

    async def reconcile_positions(self) -> tuple[list[Finding], bool]:
        """Ledger-derived net shorts vs the broker's positions. Returns (findings, blocking)."""
        records = await self.ledger.all_orders()
        ledger = net_positions(records)
        broker = {}
        adjusted: set[str] = set()
        for p in await self.broker.positions():
            broker[p.contract_symbol] = broker.get(p.contract_symbol, 0) + p.quantity
            if p.adjusted:
                adjusted.add(p.contract_symbol)
        shares = {ticker_of(sym): False for sym in set(ledger) | set(broker)}
        findings = reconcile(ledger, broker, shares_by_underlying=shares, underlying_of=ticker_of, adjusted=adjusted)
        blocking = any(f.drift in (DriftClass.OVER, DriftClass.ADJUSTED) for f in findings)
        return findings, blocking


def selftest_freshness(last_ok: datetime | None) -> killswitch.Freshness:
    return killswitch.Freshness(last_ok_at=last_ok)
