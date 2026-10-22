"""Classify a venue's reject text into a small set of classes with a policy each, pure.

Every venue we support hides the reason a place failed inside free text: Alpaca puts a dozen
different conditions behind one numeric code (40310000), Tradier returns HTTP 200 and parks the
reject on the order's `errors` field, tastytrade and Schwab put it in a status description, IBKR
in a numbered error. Code that branches on the HTTP status or the numeric code alone misreads
them (a buying-power reject read as "account blocked", a wash-trade reject retried into a second
live order). This module matches LOOSELY on the text, because the venues say the wording can
change, and attaches the one thing the caller needs: what to do next.

This classifies REJECT BODIES: text a venue returned while explicitly refusing the order, so
nothing rests. It is not for transport failures (timeouts, 5xx on a place), which are ambiguous
and belong to the engine's UNKNOWN path; "service unavailable" is deliberately NOT a warm-up
pattern for that reason.

Policies:
- RETRY_LATER: the venue is throttling or warming up and said so in an explicit rejection; the
  order did not rest. Back off, retry.
- STAND_DOWN_TICK: this order cannot be placed now (buying power, quantity held by another
  order, intent mismatch, market closed). Do not retry blindly; the next tick re-evaluates.
- CANCEL_RESTING_FIRST: an opposite-side or duplicate order on the same contract is in the way
  (wash-trade, "already have a closing order"). Cancel it, re-read the position, then re-plan.
- HALT_OPENS: the account's state forbids opening (PDT, closing-only, option level, restricted
  symbol). Sticky for the session: opens stay blocked until a human clears it.
- FIX_ORDER: the order itself is wrong (tick size, symbol format, unsupported structure, limit
  too far from the market). Retrying the same order is pointless; alert.
- UNKNOWN: unclassified. Treated as a terminal reject by the engine; surface the text.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass


class RejectClass(str, enum.Enum):
    RATE_LIMIT = "rate_limit"
    VENUE_WARMUP = "venue_warmup"
    BUYING_POWER = "buying_power"
    QTY_HELD = "qty_held"
    POSITION_INTENT = "position_intent"
    MARKET_CLOSED = "market_closed"
    TOO_CLOSE_TO_EXPIRY = "too_close_to_expiry"
    WASH_TRADE = "wash_trade"
    DUPLICATE_CLOSE = "duplicate_close"
    DUPLICATE_ORDER_ID = "duplicate_order_id"
    PDT = "pdt"
    CLOSING_ONLY = "closing_only"
    OPTION_LEVEL = "option_level"
    SYMBOL_RESTRICTED = "symbol_restricted"
    TICK_SIZE = "tick_size"
    SYMBOL_UNKNOWN = "symbol_unknown"
    STRUCTURE_UNSUPPORTED = "structure_unsupported"
    PRICE_AWAY = "price_away"
    LIMIT_VS_STRIKE = "limit_vs_strike"
    UNKNOWN = "unknown"


class Policy(str, enum.Enum):
    RETRY_LATER = "retry_later"
    STAND_DOWN_TICK = "stand_down_tick"
    CANCEL_RESTING_FIRST = "cancel_resting_first"
    HALT_OPENS = "halt_opens"
    FIX_ORDER = "fix_order"
    UNKNOWN = "unknown"


_POLICY: dict[RejectClass, Policy] = {
    RejectClass.RATE_LIMIT: Policy.RETRY_LATER,
    RejectClass.VENUE_WARMUP: Policy.RETRY_LATER,
    RejectClass.BUYING_POWER: Policy.STAND_DOWN_TICK,
    RejectClass.QTY_HELD: Policy.STAND_DOWN_TICK,
    RejectClass.POSITION_INTENT: Policy.STAND_DOWN_TICK,
    RejectClass.MARKET_CLOSED: Policy.STAND_DOWN_TICK,
    RejectClass.TOO_CLOSE_TO_EXPIRY: Policy.STAND_DOWN_TICK,
    RejectClass.WASH_TRADE: Policy.CANCEL_RESTING_FIRST,
    RejectClass.DUPLICATE_CLOSE: Policy.CANCEL_RESTING_FIRST,
    RejectClass.DUPLICATE_ORDER_ID: Policy.STAND_DOWN_TICK,
    RejectClass.PDT: Policy.HALT_OPENS,
    RejectClass.CLOSING_ONLY: Policy.HALT_OPENS,
    RejectClass.OPTION_LEVEL: Policy.HALT_OPENS,
    RejectClass.SYMBOL_RESTRICTED: Policy.HALT_OPENS,
    RejectClass.TICK_SIZE: Policy.FIX_ORDER,
    RejectClass.SYMBOL_UNKNOWN: Policy.FIX_ORDER,
    RejectClass.STRUCTURE_UNSUPPORTED: Policy.FIX_ORDER,
    RejectClass.PRICE_AWAY: Policy.FIX_ORDER,
    RejectClass.LIMIT_VS_STRIKE: Policy.FIX_ORDER,
    RejectClass.UNKNOWN: Policy.UNKNOWN,
}

# Order matters: the first pattern that matches wins, so the specific ones (PDT, wash trade)
# come before the broad ones (buying power). Patterns are case-insensitive and deliberately
# short; a venue rewording its message should still hit on the operative noun.
_PATTERNS: tuple[tuple[RejectClass, str], ...] = (
    (RejectClass.RATE_LIMIT, r"quota violation|rate limit|too many requests"),
    (RejectClass.VENUE_WARMUP, r"order manager not ready|not ready, please retry"),
    (RejectClass.PDT, r"pattern day|day trad(e|ing) (equity|buying power|protection)|TooSmallEquityForDayTrading|DayTraderPattern"),
    (RejectClass.CLOSING_ONLY, r"closing only|closing-only|no opening trades|only closing"),
    (
        RejectClass.OPTION_LEVEL,
        r"option(s)? level|not (approved|authorized|eligible) (for|to trade) (selling )?(uncovered|options|spreads|level)|OptionLevelRestriction|risk_check_failed",
    ),
    (RejectClass.SYMBOL_RESTRICTED, r"no trades are currently allowed|restricted|not tradable|non-tradable|halted"),
    (RejectClass.WASH_TRADE, r"wash trade|WashTradeAttempt|same security and same price in different directions"),
    (RejectClass.DUPLICATE_CLOSE, r"already have a closing order|duplicate (close|closing)"),
    (RejectClass.DUPLICATE_ORDER_ID, r"duplicate (order id|client_order_id|order)|client_order_id must be unique"),
    (RejectClass.QTY_HELD, r"insufficient qty|held_for_orders|qty available|not enough (shares|contracts) available"),
    (RejectClass.POSITION_INTENT, r"position intent mismatch|cannot sell to open against a long|would result in a long"),
    (
        RejectClass.TOO_CLOSE_TO_EXPIRY,
        r"expires soon|too_close_to_expiration|prior to market close on day of expiration|unable to open new positions",
    ),
    (
        RejectClass.MARKET_CLOSED,
        r"market(s)? (is |are )?closed|only allowed during market hours|extended hours is not supported|outside (of )?(regular )?trading hours",
    ),
    (RejectClass.TICK_SIZE, r"min(imum)? price (variation|increment)|rounded to nearest|tick size|price increment|penny pilot"),
    (
        RejectClass.SYMBOL_UNKNOWN,
        r"symbol does not exist|SecurityUndefined|no security definition|unknown symbol|invalid (option )?symbol|no instrument",
    ),
    (
        RejectClass.STRUCTURE_UNSUPPORTED,
        r"mleg|multi.?leg|complex orders? (not supported|are disabled)|use single leg|unsupported instrument|not supported for options",
    ),
    (RejectClass.PRICE_AWAY, r"away from the current market|too far (from|through) the market|price check|precaution"),
    (RejectClass.LIMIT_VS_STRIKE, r"limit price exceeds strike|exceeds strike price"),
    (
        RejectClass.BUYING_POWER,
        r"buying power|InitialMargin|MaintenanceMargin|margin_check_failed|insufficient (funds|cash|equity|margin)|not enough (funds|cash)",
    ),
)

# The classes a venue only ever states about THIS ORDER. A 403 whose body names one of these is a
# reject, not an auth failure; anything else on a 403 (a restricted key, an unknown symbol, a
# throttle) keeps the auth path so re-authentication and the health row still happen.
ORDER_LEVEL_REJECTS: frozenset[RejectClass] = frozenset(
    {
        RejectClass.BUYING_POWER,
        RejectClass.QTY_HELD,
        RejectClass.POSITION_INTENT,
        RejectClass.MARKET_CLOSED,
        RejectClass.TOO_CLOSE_TO_EXPIRY,
        RejectClass.WASH_TRADE,
        RejectClass.DUPLICATE_CLOSE,
        RejectClass.PDT,
        RejectClass.CLOSING_ONLY,
        RejectClass.OPTION_LEVEL,
        RejectClass.STRUCTURE_UNSUPPORTED,
        RejectClass.LIMIT_VS_STRIKE,
    }
)

_COMPILED = tuple((cls, re.compile(pat, re.IGNORECASE)) for cls, pat in _PATTERNS)


@dataclass(frozen=True)
class VenueReject:
    cls: RejectClass
    policy: Policy
    matched: str  # the fragment that decided it, for the audit log
    text: str

    @property
    def sticky(self) -> bool:
        """True when the condition outlives this order (the account, not the order, is blocked)."""
        return self.policy is Policy.HALT_OPENS


def classify_reject(text: str | None) -> VenueReject:
    """Map a venue's reject text to a class and a policy. Never raises; unknown text is UNKNOWN."""
    s = (text or "").strip()
    for cls, rx in _COMPILED:
        m = rx.search(s)
        if m:
            return VenueReject(cls, _POLICY[cls], m.group(0), s)
    return VenueReject(RejectClass.UNKNOWN, Policy.UNKNOWN, "", s)


# IBKR numbered errors (TWS API and the Client Portal gateway reuse the table). The three
# questions a bot has to answer about an IBKR error: is it noise, is the order dead, and does
# it mean the data is stale. Codes not listed are treated as a reject of the order they name.
IBKR_NOISE_CODES = frozenset({2104, 2106, 2158, 2107, 2108, 2119, 2100, 2137, 1102})  # farm OK / status chatter
IBKR_DATA_STALE_CODES = frozenset({2103, 2105, 2157, 1100, 1101, 10197, 354, 10089, 10167})
IBKR_ORDER_DEAD_CODES = frozenset({201, 202, 10147, 10148, 103, 110, 200})
IBKR_PACING_CODES = frozenset({100, 162, 420})


class IbkrErrorKind(str, enum.Enum):
    NOISE = "noise"
    DATA_STALE = "data_stale"
    PACING = "pacing"
    ORDER_DEAD = "order_dead"
    CANCEL_RACED_FILL = "cancel_raced_fill"
    SESSION = "session"
    OTHER = "other"


def classify_ibkr(code: int | None, text: str = "") -> IbkrErrorKind:
    """Classify an IBKR error. 10147/10148 on a cancel usually mean the order already filled or
    was never ours (a different clientId); the caller must re-read the order before assuming
    the cancel worked. 202 is "cancelled" and can be the venue's own price precaution, so it is
    ORDER_DEAD, not noise, but not a reject of the account either."""
    if code is None:
        return IbkrErrorKind.OTHER
    if code in IBKR_NOISE_CODES:
        return IbkrErrorKind.NOISE
    if code in IBKR_PACING_CODES:
        return IbkrErrorKind.PACING
    if code in IBKR_DATA_STALE_CODES:
        return IbkrErrorKind.DATA_STALE
    if code in (10147, 10148):
        return IbkrErrorKind.CANCEL_RACED_FILL
    if code in IBKR_ORDER_DEAD_CODES:
        return IbkrErrorKind.ORDER_DEAD
    if code in (1300, 504, 502) or re.search(r"not connected|authenticated\W*false|session", text, re.IGNORECASE):
        return IbkrErrorKind.SESSION
    return IbkrErrorKind.OTHER
