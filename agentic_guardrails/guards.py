"""Pre-trade guards (docs plan 4), every check a pure function returning
(allow, reason). An order that fails ANY guard is refused with a ledgered
reason, NEVER resized silently. `evaluate` runs them in a fixed order and stops
at the first failure, so the reason a human sees is the first binding one.

The guards are the money-path's spine, so they are total and defensive: given
any plan and any context, they return a decision, never raise, and a missing or
unreadable input fails CLOSED (deny), never open.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from agentic_guardrails.state_machine import is_terminal
from agentic_guardrails.types import OPENING_INTENTS, SPREAD_INTENTS, OrderIntent, OrderPlan, OrderState, is_adjusted_symbol, occ_parts


@dataclass(frozen=True)
class GuardResult:
    allow: bool
    reason: str


ALLOW = GuardResult(True, "ok")


@dataclass(frozen=True)
class Caps:
    """Per-scope collateral ceilings, all in dollars. A cap of None means that
    scope is unbounded (still bounded by the others). These are config values
    with code clamps; a live change to any of them is promotion-gate-review
    territory."""

    per_order: Decimal | None = None
    per_ticker: Decimal | None = None
    per_strategy: Decimal | None = None
    per_day: Decimal | None = None
    # Ceiling on TOTAL open collateral across every name sharing one risk factor
    # (see FACTOR_OF). The per-ticker cap does not bound this: TQQQ and QLD are
    # two tickers and one bet on the Nasdaq.
    per_factor: Decimal | None = None
    # Ceiling on the WHOLE book's open collateral, across every factor. This is
    # the honest bound on "all of it is the same trade": FACTOR_OF groups a
    # wrapper with its own underlier, so NVDL, AMZU, TQQQ and UPRO are four
    # DIFFERENT factors and per_factor alone would let them stack to 4x
    # (caught in review of PR #450). Rather than invent a covariance model that
    # claims to know how correlated NVDA and the Nasdaq are, cap the book: every
    # name here is long US equity beta, so the total IS the concentration.
    per_portfolio: Decimal | None = None
    # Ceiling on open collateral that RESOLVES ON ONE EXPIRATION DATE (Oct 4
    # 2026, Dan). The factor and portfolio caps bound WHAT the book is long of;
    # nothing bounded WHEN it resolves. Every armed arm fires into the same
    # weekly expiry on a scan day, so on Sep 28 2026 83% of the live book
    # ($9,150 of $11,050) resolved on one Friday: the book's realized P&L was
    # ~1 independent draw, not ~3 (the copy-book "cluster by round, not by
    # fill" lesson on the expiry axis). This is the date-axis twin of
    # per_factor. None = unbounded.
    per_expiry: Decimal | None = None
    # Phase 2 spreads (Sep 16 2026): the most OPEN spread contracts one strategy
    # may hold at once. The spreads finding's load-bearing sizing rule: a vertical
    # caps the loss per trade and does nothing about how many trades lose at
    # once (QQQ 5%-wing lost full width on 14% of 2022 entries, 26 max losses in
    # one year), so at $9k the instrument is 3-4 concurrent TQQQ spreads, never
    # 13. None = unbounded (still bounded by collateral).
    max_open_spreads_per_strategy: int | None = None
    # Smallest net credit a vertical may be opened for, as a fraction of its
    # width. A spread that collects ~nothing relative to the width it risks is
    # the wrong structure (the 5%-wing TQQQ cell: $0.47 on $3.58, hold +$17).
    min_spread_credit_frac: Decimal | None = None


@dataclass(frozen=True)
class ExecContext:
    """Everything a guard needs to judge a plan, all broker/ledger-derived and
    passed in (guards do no I/O). Collateral figures are the CURRENTLY-COMMITTED
    open short collateral in each scope, BEFORE this plan."""

    free_cash: Decimal  # broker-read settled cash available to secure new shorts
    open_collateral_total: Decimal
    open_collateral_ticker: Decimal
    open_collateral_strategy: Decimal
    committed_today: Decimal  # collateral opened so far today (for the per-day cap)
    ticker: str  # the plan's underlying, for the ticker cap/aggregation
    # Identity/liveness of other orders for this strategy:
    strategy_has_unknown_order: bool  # any UNKNOWN order for this strategy -> block opens
    inflight_states_for_contract: tuple[OrderState, ...] = field(default_factory=tuple)
    # Open short collateral keyed by FACTOR (see FACTOR_OF), for the
    # concentration guard. Derived from the ledger the same way the per-ticker
    # figure is; an empty mapping means "nothing open", not "unknown".
    open_collateral_by_factor: Mapping[str, Decimal] = field(default_factory=dict)
    # The plan's OWN factor under the engine's factor model, so the concentration guard looks
    # up the same key the engine aggregated under. None (legacy callers) falls back to the
    # module table, which is what the engine's default model is anyway.
    factor: str | None = None
    # Open short collateral keyed by EXPIRATION DATE, for the expiry
    # concentration guard. Ledger-derived like the factor map; empty means
    # "nothing open", not "unknown".
    open_collateral_by_expiry: Mapping[date, Decimal] = field(default_factory=dict)
    # Next earnings date for the plan's underlier, None when the name has no
    # scheduled event (an index/basket wrapper) or the feed is unreadable.
    # Unreadable and absent are deliberately NOT distinguished here; the caller
    # passes earnings_lookup_failed=True when it could not read, so the guard
    # can fail closed on a genuine read failure without treating every index as
    # a failure.
    next_earnings: date | None = None
    earnings_lookup_failed: bool = False
    # Expiration of the contract being sold, for the earnings-in-window check.
    expiration: date | None = None
    # Open spread CONTRACTS this strategy already holds (ledger-derived), for the
    # concurrency cap. 0 means "none open", not "unknown".
    open_spreads_strategy: int = 0
    # Broker-confirmed OCC-adjusted contracts (digit root OR non-100 multiplier
    # from the instrument). A multiplier-only adjustment keeps a standard-looking
    # symbol, so the symbol test alone cannot see it; the engine reads this from
    # broker.positions() before every submission.
    adjusted_contracts: frozenset[str] = frozenset()
    adjusted_read_failed: bool = False  # positions read failed: opens refuse, closes use the symbol check


# ── Individual guards ─────────────────────────────────────────────────────────
def guard_has_limit(plan: OrderPlan, ctx: ExecContext, caps: Caps) -> GuardResult:
    """No market orders, ever. A plan without a positive per-share limit is
    refused outright (defense in depth: OrderPlan already enforces this at
    construction, but the guard makes it a money-path check too)."""
    if plan.limit_price <= 0:
        return GuardResult(False, "no limit price (market orders are forbidden)")
    return ALLOW


def guard_not_adjusted(plan: OrderPlan, ctx: ExecContext, caps: Caps) -> GuardResult:
    """Never place an order on an OCC-ADJUSTED contract (reverse split /
    corporate action: a digit root such as NVDL1). Its deliverable is no longer
    100 shares, so every collateral, pricing and assignment computation in this
    module is wrong for it. Applies to CLOSES too: an adjusted short is managed
    by a human, the assignment guard only alerts on it."""
    for sym in (plan.contract_symbol, plan.long_contract_symbol):
        if sym and (is_adjusted_symbol(sym) or sym in ctx.adjusted_contracts):
            return GuardResult(False, f"{sym} is an OCC-adjusted contract (non-standard deliverable): human only, never auto-traded")
    if ctx.adjusted_read_failed and plan.intent in OPENING_INTENTS:
        return GuardResult(False, "broker positions unreadable: cannot rule out an OCC-adjusted contract; refusing to open (fail closed)")
    return ALLOW


def guard_one_in_flight(plan: OrderPlan, ctx: ExecContext, caps: Caps) -> GuardResult:
    """One in-flight order per (strategy, contract) at a time. If any existing
    order for this contract is non-terminal, refuse: overlapping cycles are how
    the same copy got placed twice and one filled untracked."""
    for st in ctx.inflight_states_for_contract:
        if not is_terminal(st):
            return GuardResult(False, f"an order for this contract is already in flight (state {st.value})")
    return ALLOW


def guard_unknown_blocks_opens(plan: OrderPlan, ctx: ExecContext, caps: Caps) -> GuardResult:
    """An UNKNOWN order anywhere in this strategy blocks NEW opening orders for
    it until the reconciler resolves the unknown. Fail closed: we do not open
    new risk while we cannot account for existing risk. Closing orders (BTC) are
    allowed through, since reducing exposure while blind is the safe direction."""
    if plan.intent in OPENING_INTENTS and ctx.strategy_has_unknown_order:
        return GuardResult(False, "strategy has an UNKNOWN order; opens are blocked until it is reconciled")
    return ALLOW


def guard_cash_secured(plan: OrderPlan, ctx: ExecContext, caps: Caps) -> GuardResult:
    """A cash-secured short must be fully backed: this plan's collateral must fit
    within the broker-read free cash. Closing orders consume no collateral and
    always pass this guard. A non-positive free-cash read fails closed."""
    need = plan.collateral()
    if need <= 0:
        return ALLOW
    if ctx.free_cash <= 0:
        return GuardResult(False, "free cash unreadable or non-positive; standing down (fail closed)")
    if need > ctx.free_cash:
        return GuardResult(False, f"collateral {need} exceeds free cash {ctx.free_cash}")
    return ALLOW


def _cap_check(name: str, need: Decimal, already: Decimal, cap: Decimal | None) -> GuardResult:
    if cap is None or need <= 0:
        return ALLOW
    if already + need > cap:
        return GuardResult(False, f"{name} cap: {already} + {need} > {cap}")
    return ALLOW


def guard_caps(plan: OrderPlan, ctx: ExecContext, caps: Caps) -> GuardResult:
    """Per-order / per-ticker / per-strategy / per-day collateral ceilings. The
    plan's collateral, added to what each scope already commits, must not exceed
    that scope's cap. First binding cap wins the message."""
    need = plan.collateral()
    for res in (
        _cap_check("per-order", need, Decimal(0), caps.per_order),
        _cap_check("per-ticker", need, ctx.open_collateral_ticker, caps.per_ticker),
        _cap_check("per-strategy", need, ctx.open_collateral_strategy, caps.per_strategy),
        _cap_check("per-day", need, ctx.committed_today, caps.per_day),
    ):
        if not res.allow:
            return res
    return ALLOW


# ── Factor concentration (Sep 6 2026) ────────────────────────────────────────
# The scanned universe is almost entirely leveraged wrappers on two indices plus
# a handful of mega-cap single names. Every pick the live track has produced so
# far (NVDL, AMZU, TQQQ, UPRO) is long tech beta. The per-ticker cap does not
# see that: four tickers at the cap is four times the intended risk on one bet,
# and one gap-down morning assigns all of them together.
#
# FACTOR_OF maps a traded symbol to the risk factor it actually expresses. It is
# deliberately COARSE (a lookup, not a covariance model): the failure it prevents
# is "all of it is the same trade", which does not need a precise beta to spot.
# An unknown symbol maps to itself, so a new ticker is its own factor and is
# never silently pooled into someone else's budget.
FACTOR_OF: Mapping[str, str] = {
    # Nasdaq-100 wrappers
    "QQQ": "NDX",
    "QLD": "NDX",
    "TQQQ": "NDX",
    "ROM": "NDX",
    "TECL": "NDX",
    # S&P 500 wrappers
    "SPY": "SPX",
    "SSO": "SPX",
    "SPXL": "SPX",
    "UPRO": "SPX",
    # Russell
    "IWM": "RUT",
    "TNA": "RUT",
    # Single-name wrappers, factored to their underlier. Kept in lockstep with
    # the scanner's SINGLE_STOCK_UNDERLIERS so a wrapper the scanner treats as a
    # single-name event is (a) grouped with its siblings by the factor cap and
    # (b) earnings-resolvable to a real underlier by the open loop's provider. A
    # wrapper missing here factors to ITSELF, so its earnings lookup returns None
    # and fails closed — permanently unfundable — which is how NVDX (a 2x NVDA
    # wrapper the $9k pool can actually afford) sat blocked with the guard unable
    # to prove it clear.
    "TSLL": "TSLA",
    "NVDL": "NVDA",
    "NVDX": "NVDA",
    "AAPU": "AAPL",
    "AMZU": "AMZN",
    "GGLL": "GOOGL",
    # Commodity fund (Oct 19 2026): crude oil futures, no earnings calendar. Mapped
    # so the earnings guard reads it as event-free instead of an unreadable single
    # name (it failed closed on every USO open), and so it is its own factor.
    "USO": "OIL",
}


# Factors that are a SINGLE COMPANY, and therefore have scheduled earnings. An
# index or basket factor (NDX/SPX/RUT) does not. Kept here rather than imported
# from the scanner so the executor core stays stdlib-only and importable without
# the app dependency tree.
SINGLE_NAME_FACTORS: frozenset[str] = frozenset({"TSLA", "NVDA", "AAPL", "AMZN", "GOOGL"})


def factor_of(ticker: str) -> str:
    """The risk factor a symbol expresses. Unknown symbols are their own factor."""
    return FACTOR_OF.get(ticker.upper(), ticker.upper())


def underlier_has_earnings(ticker: str) -> bool:
    """Does this symbol's underlier report earnings?

    Used by the engine to decide whether a MISSING earnings lookup is benign (an
    index wrapper has no event to miss) or a fail-closed refusal (a single-name
    wrapper does). An unknown symbol is treated as single-name, the conservative
    reading: we would rather refuse an unfamiliar ticker than sell an event we
    could not see.
    """
    t = ticker.upper()
    f = FACTOR_OF.get(t)
    if f is None:
        return True  # unknown symbol: assume it can have earnings
    return f in SINGLE_NAME_FACTORS


def guard_factor_concentration(plan: OrderPlan, ctx: ExecContext, caps: Caps) -> GuardResult:
    """Cap total open collateral across every name sharing one risk factor.

    Closing orders consume no collateral and always pass. A missing factor map
    entry is not an error: the symbol becomes its own factor, which is the
    conservative reading (it cannot borrow another factor's headroom)."""
    need = plan.collateral()
    if need <= 0 or caps.per_factor is None:
        return ALLOW
    factor = ctx.factor if ctx.factor is not None else factor_of(ctx.ticker)
    already = ctx.open_collateral_by_factor.get(factor, Decimal(0))
    if already + need > caps.per_factor:
        return GuardResult(
            False,
            f"factor cap ({factor}): {already} + {need} > {caps.per_factor}; {ctx.ticker} shares this factor with the rest of the book",
        )
    return ALLOW


def guard_expiry_concentration(plan: OrderPlan, ctx: ExecContext, caps: Caps) -> GuardResult:
    """Cap total open collateral resolving on ONE expiration date.

    Closing orders consume no collateral and always pass. A plan with no
    expiration cannot be placed on the date axis and passes here (the OCC
    parser already refused it earlier if it is malformed). A spread counts its
    width, a cash-secured put its strike, the same collateral the other caps
    see; the only new thing is the grouping key."""
    need = plan.collateral()
    if need <= 0 or caps.per_expiry is None or plan.expiration is None:
        return ALLOW
    already = ctx.open_collateral_by_expiry.get(plan.expiration, Decimal(0))
    if already + need > caps.per_expiry:
        return GuardResult(
            False,
            f"expiry cap ({plan.expiration.isoformat()}): {already} + {need} > {caps.per_expiry}; "
            f"the book would resolve too much on one date",
        )
    return ALLOW


def guard_portfolio_concentration(plan: OrderPlan, ctx: ExecContext, caps: Caps) -> GuardResult:
    """Cap the whole book's open collateral, regardless of factor.

    The backstop for correlated exposure that FACTOR_OF deliberately does not
    group. Note this reads `open_collateral_total`, which the context has always
    carried and which no guard read until now."""
    need = plan.collateral()
    if need <= 0 or caps.per_portfolio is None:
        return ALLOW
    if ctx.open_collateral_total + need > caps.per_portfolio:
        return GuardResult(
            False,
            f"portfolio cap: {ctx.open_collateral_total} + {need} > {caps.per_portfolio}; "
            f"the book is long one factor's worth of beta across several tickers",
        )
    return ALLOW


# ── Earnings in window (Sep 6 2026) ──────────────────────────────────────────
# The threshold checker already ALERTS when a tracked position has earnings
# inside its remaining window, but nothing stopped the position being opened
# that way in the first place. An alert after the fact is not a control: by then
# the event risk is already sold. This moves the same check to the open path.
#
# Scope: opening orders only. Closing into an event is how you REDUCE event
# risk, so a BTC is always allowed through.
EARNINGS_BUFFER_DAYS = 0  # an event ON expiry day still counts as in-window


def guard_no_earnings_in_window(plan: OrderPlan, ctx: ExecContext, caps: Caps) -> GuardResult:
    """Refuse to OPEN a short whose expiry spans the underlier's next earnings.

    Fails CLOSED on a failed lookup: if we cannot tell whether an event lands in
    the window, we do not sell the window. An index/basket wrapper has no
    scheduled event, reports next_earnings=None with earnings_lookup_failed=False,
    and passes."""
    if plan.intent not in OPENING_INTENTS:
        return ALLOW
    if ctx.earnings_lookup_failed:
        return GuardResult(False, "earnings lookup failed; refusing to open into an unknown event window (fail closed)")
    if ctx.next_earnings is None or ctx.expiration is None:
        return ALLOW
    if ctx.next_earnings <= ctx.expiration + timedelta(days=EARNINGS_BUFFER_DAYS):
        return GuardResult(
            False,
            f"{ctx.ticker} reports earnings {ctx.next_earnings}, inside this contract's window (expiry {ctx.expiration})",
        )
    return ALLOW


# ── Vertical spreads (Phase 2, Sep 16 2026) ──────────────────────────────────
def guard_spread_structure(plan: OrderPlan, ctx: ExecContext, caps: Caps) -> GuardResult:
    """A spread plan must be a well-formed put credit spread: a wing strictly
    below the short on the SAME underlying and expiration, a net credit below
    the width, and (on open) a credit worth at least `min_spread_credit_frac` of
    the width. OrderPlan already raises on the structural half at construction;
    the guard makes the money-path check total (never raises) and adds the
    economic floor. Single-leg plans pass through."""
    if plan.intent not in SPREAD_INTENTS:
        return ALLOW
    if not plan.long_contract_symbol or plan.long_strike is None:
        return GuardResult(False, "spread plan has no long leg")
    width = plan.width()
    if width <= 0:
        return GuardResult(False, f"spread wing {plan.long_strike} is not below the short strike {plan.strike}")
    try:
        short_leg = occ_parts(plan.contract_symbol)
        wing_leg = occ_parts(plan.long_contract_symbol)
    except ValueError as exc:
        return GuardResult(False, f"spread leg is not an OCC symbol: {exc}")
    if (short_leg.kind, wing_leg.kind) != ("P", "P"):
        return GuardResult(False, "spread legs must both be puts")
    if short_leg.root != wing_leg.root or short_leg.expiration != wing_leg.expiration or short_leg.expiration != plan.expiration:
        return GuardResult(False, "spread legs differ in root/expiration (or from the plan's expiration); not a vertical")
    if short_leg.strike != plan.strike or wing_leg.strike != plan.long_strike:
        return GuardResult(False, "spread strike fields do not match the leg symbols")
    if plan.limit_price >= width:
        return GuardResult(False, f"spread net price {plan.limit_price} is not below its width {width}")
    if plan.intent == OrderIntent.PCS_OPEN and caps.min_spread_credit_frac is not None:
        floor = (width * caps.min_spread_credit_frac).quantize(Decimal("0.01"))
        if plan.limit_price < floor:
            return GuardResult(
                False, f"spread credit {plan.limit_price} is under {caps.min_spread_credit_frac:.0%} of its width {width} (floor {floor})"
            )
    return ALLOW


def guard_spread_concurrency(plan: OrderPlan, ctx: ExecContext, caps: Caps) -> GuardResult:
    """Cap OPEN spread contracts per strategy. The finding's binding risk control
    for verticals: the loss per spread is capped by the width, so what must be
    bounded is how many are on at once when a bear clusters the max losses.
    Closing a spread always passes."""
    if plan.intent != OrderIntent.PCS_OPEN or caps.max_open_spreads_per_strategy is None:
        return ALLOW
    if ctx.open_spreads_strategy + plan.quantity > caps.max_open_spreads_per_strategy:
        return GuardResult(
            False,
            f"spread concurrency cap ({plan.strategy}): {ctx.open_spreads_strategy} open + {plan.quantity} > {caps.max_open_spreads_per_strategy}",
        )
    return ALLOW


# Order matters: cheapest/most-fundamental checks first, so the first failure a
# human reads is the most basic reason the order cannot go.
_GUARDS = (
    guard_has_limit,
    guard_not_adjusted,
    guard_spread_structure,
    guard_one_in_flight,
    guard_unknown_blocks_opens,
    guard_no_earnings_in_window,
    guard_cash_secured,
    guard_caps,
    guard_factor_concentration,
    guard_expiry_concentration,
    guard_portfolio_concentration,
    guard_spread_concurrency,
)


def evaluate(plan: OrderPlan, ctx: ExecContext, caps: Caps) -> GuardResult:
    """Run every guard in order; return the first failure, or ALLOW if all pass.
    Total: any plan/context yields a decision, never an exception."""
    for g in _GUARDS:
        res = g(plan, ctx, caps)
        if not res.allow:
            return res
    return ALLOW
