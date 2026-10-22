"""Order pricing policy (docs plan 3.3), pure and I/O-free.

Sell-to-open limit = max(bid, mid - k*half_spread). k is how many half-spreads
BELOW the mid we sit, so k=1 lands exactly on the bid (mid - 1*half_spread ==
bid) and is the conservative default: the paper book's credit basis is the bid,
so a live fill at the bid makes the paper assumptions hold exactly. Lower k
shades UP toward the mid to chase a thin book (k=0 is the mid); k>=1 clamps to
the bid, never below. No
market orders anywhere: a plan without a positive limit never gets built here,
and the guard rejects one if it somehow appears. Every price is cross-checked
against Massive's NBBO before placement; a disagreement beyond tolerance stands
the order down (two independent sources must agree before money moves).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from agentic_guardrails.types import OrderIntent, Quote

# Options quote in penny increments; round the SELL limit DOWN (never ask for
# more than the tick supports = never fail to fill for a sub-penny) and a BUY
# limit UP, both toward a more-fillable price, never a less-fillable one.
_TICK = Decimal("0.01")


def sto_limit(quote: Quote, k: Decimal = Decimal(1)) -> Decimal:
    """Sell-to-open limit for a short. max(bid, mid - k*half_spread), penny-floored.
    k=1 (default) -> the bid, the paper-consistent basis. 0<=k<1 shades UP toward
    the mid to chase a thin book; k>=1 clamps to the bid, never below."""
    if k < 0:
        raise ValueError("k must be >= 0")
    shaded = quote.mid - k * quote.half_spread
    limit = max(quote.bid, shaded)
    q = limit.quantize(_TICK, rounding=ROUND_DOWN)
    # Never round a sell limit to zero or below the bid's floor.
    return max(q, quote.bid.quantize(_TICK, rounding=ROUND_DOWN))


def btc_limit(quote: Quote, target_credit: Decimal, fraction: Decimal = Decimal("0.5")) -> Decimal:
    """Buy-to-close limit at the profit target: the fraction of the credit we
    keep as a buyback price (0.5 = close at 50% of credit). Penny-ceiled so the
    resting buy is at-or-through the target, never a penny short of it. Bounded
    below by the tick and above by the current ask (never overpay past the
    market to close)."""
    if not (Decimal(0) < fraction < Decimal(1)):
        raise ValueError("fraction must be in (0, 1)")
    target = (target_credit * fraction).quantize(_TICK, rounding=ROUND_UP)
    target = max(target, _TICK)
    return min(target, quote.ask.quantize(_TICK, rounding=ROUND_UP))


def limit_for(intent: OrderIntent, quote: Quote, *, k: Decimal = Decimal(1), target_credit: Decimal | None = None) -> Decimal:
    """Dispatch the right pricing rule for an intent."""
    if intent in (OrderIntent.STO, OrderIntent.CC):
        return sto_limit(quote, k)
    if intent == OrderIntent.BTC:
        if target_credit is None:
            raise ValueError("BTC pricing needs the entry target_credit")
        return btc_limit(quote, target_credit)
    raise ValueError(f"no pricing rule for intent {intent}")


# ── Vertical put credit spreads (Phase 2) ─────────────────────────────────────
# The paper book records a spread at BID-CROSS-ASK (short.bid - long.ask, both
# legs at the worst executable side; options_chain.select_put_spread). k=1
# lands the net limit exactly there, so a live fill at the limit makes the paper
# credit hold; 0<=k<1 shades up toward the mid-to-mid net to chase a thin book.
def pcs_open_limit(short: Quote, long: Quote, k: Decimal = Decimal(1)) -> Decimal:
    """Net CREDIT limit for a vertical: max(bid-cross-ask, net_mid - k*net_half_spread),
    penny-floored, never below bid-cross-ask. Raises when the structure earns no
    positive credit at the bid-cross-ask (an unbuildable spread is a stand-down at
    selection, never priced here)."""
    if k < 0:
        raise ValueError("k must be >= 0")
    cross = short.bid - long.ask
    if cross <= 0:
        raise ValueError(f"spread earns no net credit at bid-cross-ask ({short.bid} - {long.ask})")
    net_mid = short.mid - long.mid
    net_half = short.half_spread + long.half_spread
    limit = max(cross, net_mid - k * net_half)
    q = limit.quantize(_TICK, rounding=ROUND_DOWN)
    return max(q, cross.quantize(_TICK, rounding=ROUND_DOWN))


def pcs_close_limit(short: Quote, long: Quote, target_credit: Decimal, fraction: Decimal = Decimal("0.5")) -> Decimal:
    """Net DEBIT limit to buy the vertical back at a fraction of the entry credit
    (0.5 = close at 50%). Penny-ceiled so the resting buy is at-or-through the
    target; bounded above by ASK-CROSS-BID (short.ask - long.bid, the worst
    executable net) so a close never pays past the market. NB the spreads
    finding says HOLD is the right management basis for a vertical (close50
    forfeits most of its P&L); this exists for risk-reducing closes, not as the
    default exit."""
    if not (Decimal(0) < fraction < Decimal(1)):
        raise ValueError("fraction must be in (0, 1)")
    target = (target_credit * fraction).quantize(_TICK, rounding=ROUND_UP)
    target = max(target, _TICK)
    worst = (short.ask - long.bid).quantize(_TICK, rounding=ROUND_UP)
    return max(_TICK, min(target, worst))


@dataclass(frozen=True)
class NbboCheck:
    agree: bool
    reason: str
    delta: Decimal


def nbbo_agreement(
    broker: Quote,
    massive: Quote,
    *,
    tol_abs: Decimal = Decimal("0.05"),
    tol_frac: Decimal = Decimal("0.10"),
) -> NbboCheck:
    """Do the broker's and Massive's mids agree closely enough to trade on? The
    two sources must agree within max(tol_abs, tol_frac * massive_mid) — an
    absolute floor so a cheap contract is not held to an impossibly tight cent,
    and a fractional band so an expensive one is not waved through on a large
    absolute gap. Disagreement stands the order down (never averages the two)."""
    ref = abs(massive.mid)
    tol = max(tol_abs, tol_frac * ref)
    delta = abs(broker.mid - massive.mid)
    if delta <= tol:
        return NbboCheck(True, "ok", delta)
    return NbboCheck(False, f"broker mid {broker.mid} vs massive {massive.mid} differ by {delta} > tol {tol}", delta)
