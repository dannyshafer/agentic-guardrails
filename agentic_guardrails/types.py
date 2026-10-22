"""Core value types for the executor's decision core (stdlib only).

Every monetary value is a Decimal and every contract symbol is an OCC string;
normalization to those forms is the adapter's job, so by the time a value
reaches the decision core it is already clean. The types here are immutable
(frozen dataclasses) so a plan or a fill can be passed through guards and the
state machine without any function being able to mutate it underfoot.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal


class OrderState(str, enum.Enum):
    """Lifecycle of one order. UNKNOWN is first-class: we placed and then lost
    connectivity before reading a terminal status, and must fail closed until
    the reconciler resolves it (never guess it filled or didn't)."""

    PLANNED = "planned"  # a plan exists, nothing sent
    REVIEWED = "reviewed"  # broker review/simulate passed (if supported)
    PLACED = "placed"  # accepted by broker, no fill yet
    PARTIAL = "partial"  # some quantity filled, order still working
    FILLED = "filled"  # fully filled (terminal)
    CANCELED = "canceled"  # canceled with no remaining quantity (terminal)
    REJECTED = "rejected"  # broker refused (terminal)
    EXPIRED = "expired"  # day order expired unfilled (terminal)
    UNKNOWN = "unknown"  # placed, terminal status unread (NOT terminal; fail closed)


TERMINAL_STATES: frozenset[OrderState] = frozenset({OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED, OrderState.EXPIRED})


class OrderIntent(str, enum.Enum):
    """What the order is for. STO/CC OPEN risk (and consume collateral); BTC
    CLOSES an existing short and frees it."""

    STO = "sto"  # sell-to-open a cash-secured put
    BTC = "btc"  # buy-to-close the short put (the 50% target)
    CC = "cc"  # sell-to-open a covered call (the assigned-wheel leg)
    # Phase 2: vertical put credit spreads, the capital lever. ONE order with
    # TWO legs: sell the short put, buy the lower-strike wing, for a net credit.
    PCS_OPEN = "pcs_open"  # sell-to-open the vertical (net credit)
    PCS_CLOSE = "pcs_close"  # buy-to-close the vertical (net debit)


OPENING_INTENTS: frozenset[OrderIntent] = frozenset({OrderIntent.STO, OrderIntent.CC, OrderIntent.PCS_OPEN})
# Intents whose plan carries a second (long) leg. A spread plan without its wing
# is malformed and raises at construction (see OrderPlan.__post_init__).
SPREAD_INTENTS: frozenset[OrderIntent] = frozenset({OrderIntent.PCS_OPEN, OrderIntent.PCS_CLOSE})
CLOSING_INTENTS: frozenset[OrderIntent] = frozenset({OrderIntent.BTC, OrderIntent.PCS_CLOSE})

TIME_IN_FORCE: frozenset[str] = frozenset({"gfd", "gtc"})

# basis.rule of a RESTING take-profit close (GTC buy-to-close at the close50
# target). Orders carrying it are meant to sit working at the broker for days,
# so the engine must not cancel them after its settle poll, and one arm's
# resting close must not block ANOTHER arm's resting close on the same contract
# (two arms can hold the same put at different credits).
RESTING_TP_RULE = "btc_close50_rest"


class QuoteSource(str, enum.Enum):
    ROBINHOOD = "robinhood"
    MASSIVE = "massive"


@dataclass(frozen=True)
class OccParts:
    root: str
    expiration: date
    kind: str  # "P" | "C"
    strike: Decimal


def occ_parts(contract_symbol: str) -> OccParts:
    """Strict OCC parse: ROOT + YYMMDD + C/P + strike*1000 in 8 digits, with or
    without the 'O:' prefix. Raises ValueError on anything else. The one parser
    the decision core trusts for leg identity (the plan builder, the plan's own
    validation and the structure guard all read it), so a symbol/field mismatch
    cannot slip through one path that another would have caught."""
    s = contract_symbol.removeprefix("O:").strip().upper()
    i = 0
    while i < len(s) and s[i].isalpha():
        i += 1
    root, tail = s[:i], s[i:]
    if not root or len(tail) != 15 or tail[6] not in ("C", "P") or not (tail[:6] + tail[7:]).isdigit():
        raise ValueError(f"not an OCC symbol: {contract_symbol!r}")
    exp = date(2000 + int(tail[0:2]), int(tail[2:4]), int(tail[4:6]))
    return OccParts(root=root, expiration=exp, kind=tail[6], strike=Decimal(tail[7:]) / Decimal(1000))


STANDARD_MULTIPLIER = 100


def occ_root(contract_symbol: str) -> str:
    """The OCC root, read from the RIGHT (the 15-char date/type/strike tail is
    fixed width), so an ADJUSTED root that carries a digit ('NVDL1') parses as
    'NVDL1' instead of failing. Raises ValueError on anything not OCC-shaped."""
    s = contract_symbol.removeprefix("O:").strip().upper()
    root, tail = s[:-15], s[-15:]
    if not root or not root.isalnum() or not root[0].isalpha() or len(tail) != 15 or tail[6] not in ("C", "P"):
        raise ValueError(f"not an OCC symbol: {contract_symbol!r}")
    if not (tail[:6] + tail[7:]).isdigit():
        raise ValueError(f"not an OCC symbol: {contract_symbol!r}")
    return root


def is_adjusted_symbol(contract_symbol: str) -> bool:
    """True for an OCC-ADJUSTED contract (reverse split / corporate action): OCC
    re-issues the series under a new root that carries a digit ('NVDL1') and a
    non-standard deliverable. The executor never trades one. An unparseable
    symbol is NOT reported as adjusted here (the strict parsers already refuse
    it); this only answers the adjusted-root question."""
    try:
        root = occ_root(contract_symbol)
    except ValueError:
        return False
    return any(ch.isdigit() for ch in root)


def adjusted_base_key(contract_symbol: str) -> tuple[str, str, str] | None:
    """(base root with trailing digits stripped, YYMMDD, C/P) for pairing an
    adjusted broker contract with the ledger's pre-adjustment symbol. The strike
    is deliberately NOT part of the key: an adjustment may restate it. None when
    the symbol does not parse."""
    try:
        root = occ_root(contract_symbol)
    except ValueError:
        return None
    s = contract_symbol.removeprefix("O:").strip().upper()
    tail = s[-15:]
    return root.rstrip("0123456789"), tail[:6], tail[6]


@dataclass(frozen=True)
class Quote:
    """One contract's NBBO from one source. mid/half_spread are derived so a
    caller can never pass an inconsistent trio."""

    contract_symbol: str
    bid: Decimal
    ask: Decimal
    source: QuoteSource
    as_of: datetime
    # Optional greeks/mark, populated when the source carries them (Robinhood
    # does). The assignment guard needs delta (P(ITM) proxy) and mark (to derive
    # extrinsic vs intrinsic); None when unavailable so pricing paths that only
    # need bid/ask are unaffected.
    delta: Decimal | None = None
    mark: Decimal | None = None

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal(2)

    @property
    def half_spread(self) -> Decimal:
        return (self.ask - self.bid) / Decimal(2)


@dataclass(frozen=True)
class PriceBasis:
    """The audit trail for WHY a limit price is what it is: which quote, from
    which source, at what time, under which rule. Every order carries one so a
    fill can always be explained after the fact."""

    rule: str  # e.g. "sto_bid" | "sto_mid_minus_k_halfspread"
    quote_source: QuoteSource
    bid: Decimal
    ask: Decimal
    as_of: datetime


@dataclass(frozen=True)
class OrderPlan:
    """An immutable intent to trade, BEFORE anything is sent to the broker. The
    client_order_id is deterministic (see ids.py) so a restart re-derives the
    same id and the reconciler re-adopts an in-flight order instead of
    double-placing it."""

    client_order_id: str
    strategy: str
    contract_symbol: str  # OCC
    intent: OrderIntent
    quantity: int  # contracts, > 0
    limit_price: Decimal  # per share; NO market orders, ever
    strike: Decimal
    expiration: date
    basis: PriceBasis
    created_at: datetime
    # Spread legs (PCS_OPEN / PCS_CLOSE only). `contract_symbol`/`strike` stay the
    # SHORT leg so every single-leg reader (ticker, earnings, factor, one-in-flight)
    # keeps working unchanged; the wing rides here. `limit_price` is then the NET
    # price per share (credit on open, debit on close), never a per-leg price.
    long_contract_symbol: str | None = None
    long_strike: Decimal | None = None
    # "gfd" (day) for every marketable order. "gtc" only for a RESTING take-profit
    # close, which must survive the session close to fill whenever the target trades.
    time_in_force: str = "gfd"

    def __post_init__(self) -> None:
        # Structural invariants that must hold for ANY plan the core will touch.
        # A malformed plan is a programming error, not a stand-down, so it raises.
        if self.time_in_force not in TIME_IN_FORCE:
            raise ValueError(f"OrderPlan.time_in_force must be one of {sorted(TIME_IN_FORCE)}, got {self.time_in_force!r}")
        if self.time_in_force == "gtc" and self.intent not in CLOSING_INTENTS:
            raise ValueError("only a closing order may rest GTC; an opening order is a day order")
        if self.quantity <= 0:
            raise ValueError(f"OrderPlan.quantity must be > 0, got {self.quantity}")
        if self.limit_price <= 0:
            raise ValueError("OrderPlan.limit_price must be > 0 (no market orders)")
        if self.strike <= 0:
            raise ValueError("OrderPlan.strike must be > 0")
        if self.intent in SPREAD_INTENTS:
            self._check_spread()
        elif self.long_contract_symbol is not None or self.long_strike is not None:
            raise ValueError(f"intent {self.intent.value} is single-leg; it cannot carry a long leg")

    def _check_spread(self) -> None:
        if not self.long_contract_symbol or self.long_strike is None:
            raise ValueError("a spread plan needs its long leg (long_contract_symbol + long_strike)")
        if self.long_strike <= 0 or self.long_strike >= self.strike:
            raise ValueError("a put credit spread's wing must sit strictly BELOW the short strike")
        if self.long_contract_symbol.strip().upper() == self.contract_symbol.strip().upper():
            raise ValueError("a spread's two legs must be different contracts")
        if self.limit_price >= self.width():
            raise ValueError("a spread's net price must be below its width (a credit at or above the width is not a spread)")
        # The symbols are what the broker trades; the strike/expiration
        # fields are what the caps and guards read. They must be the SAME
        # contracts: two puts, one root, one expiration equal to the plan's,
        # strikes equal to the plan's. Otherwise a plan could sell calls
        # under PCS_OPEN or fabricate a $1 width that every collateral cap
        # then trusts (Devin, PR #529).
        short_leg = occ_parts(self.contract_symbol)
        wing_leg = occ_parts(self.long_contract_symbol)
        if (short_leg.kind, wing_leg.kind) != ("P", "P"):
            raise ValueError("a put credit spread needs two PUT legs")
        if short_leg.root != wing_leg.root:
            raise ValueError(f"spread legs are on different roots ({short_leg.root} vs {wing_leg.root})")
        if short_leg.expiration != wing_leg.expiration or short_leg.expiration != self.expiration:
            raise ValueError("spread legs must share the plan's expiration (a different wing expiry is a diagonal, not a vertical)")
        if short_leg.strike != self.strike or wing_leg.strike != self.long_strike:
            raise ValueError("spread strike fields do not match the leg symbols")

    @property
    def is_spread(self) -> bool:
        return self.intent in SPREAD_INTENTS

    def width(self) -> Decimal:
        """Strike distance between the short and the wing (0 for a single leg)."""
        if self.long_strike is None:
            return Decimal(0)
        return self.strike - self.long_strike

    def collateral(self) -> Decimal:
        """Cash this plan ties up. A cash-secured short: strike x 100 x qty. A
        vertical: its WIDTH x 100 x qty, the defined max loss, gross of the credit
        (the broker nets the credit against the requirement; we deliberately do
        not, so the caps read a few percent conservative rather than optimistic).
        A closing intent frees collateral rather than consuming it, so it is 0
        here; callers that net collateral treat closing intents separately."""
        if self.intent not in OPENING_INTENTS:
            return Decimal(0)
        per = self.width() if self.is_spread else self.strike
        return per * Decimal(100) * Decimal(self.quantity)

    def legs(self) -> tuple[str, ...]:
        """Every contract this plan touches (short first)."""
        return (self.contract_symbol, self.long_contract_symbol) if self.long_contract_symbol else (self.contract_symbol,)


@dataclass(frozen=True)
class BrokerOrder:
    """The broker's view of an order, as last read. filled_qty and avg_fill_price
    are the ONLY sources of booked size and P&L (never a quote or an intended
    size). raw is the verbatim payload for the audit log."""

    broker_order_id: str | None  # None only for a not-yet-placed / rejected-pre-id plan
    state: OrderState
    filled_qty: int
    avg_fill_price: Decimal | None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Position:
    """A held options position. Negative quantity = short (a sold put/call)."""

    contract_symbol: str
    quantity: int  # signed: short puts are negative
    avg_price: Decimal
    # Contract multiplier as the broker reports it (trade_value_multiplier).
    # Anything other than 100 means a non-standard deliverable (an OCC
    # adjustment), even when the root carries no digit.
    multiplier: int = STANDARD_MULTIPLIER

    @property
    def adjusted(self) -> bool:
        """An OCC-adjusted contract: digit root or non-100 multiplier. The
        executor and the assignment guard never trade one automatically."""
        return self.multiplier != STANDARD_MULTIPLIER or is_adjusted_symbol(self.contract_symbol)


@dataclass(frozen=True)
class Account:
    """Account cash state, broker-read. free_cash is settled cash available to
    secure new shorts; buying_power is the broker's own figure, cross-checked
    against our ledger by the cash-secured guard."""

    free_cash: Decimal
    buying_power: Decimal
