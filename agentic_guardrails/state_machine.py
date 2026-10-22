"""The order state machine (docs plan 3.2): an explicit, total transition table
and the fill-bookkeeping invariants promoted from real Polymarket losses.

Pure and I/O-free. The engine's I/O shell calls `apply_broker_status` after every
broker read and `assert_legal` before any state write; illegal transitions raise
rather than silently correcting, because a surprising transition is a bug in our
model of the broker, and hiding it is how phantom positions were born.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from agentic_guardrails.types import TERMINAL_STATES, BrokerOrder, OrderState

# name -> the set of states it may move to next. Terminal states move nowhere.
# UNKNOWN can resolve to ANY real state once the reconciler reads a true status
# (it is the "we don't know yet" node, not a dead end).
LEGAL_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.PLANNED: frozenset({OrderState.REVIEWED, OrderState.PLACED, OrderState.REJECTED}),
    OrderState.REVIEWED: frozenset({OrderState.PLACED, OrderState.REJECTED}),
    OrderState.PLACED: frozenset(
        {
            OrderState.PARTIAL,
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
            OrderState.UNKNOWN,
        }
    ),
    OrderState.PARTIAL: frozenset({OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCELED, OrderState.EXPIRED, OrderState.UNKNOWN}),
    OrderState.UNKNOWN: frozenset(
        {
            OrderState.PLACED,
            OrderState.PARTIAL,
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        }
    ),
    # Terminal: no outgoing transitions.
    OrderState.FILLED: frozenset(),
    OrderState.CANCELED: frozenset(),
    OrderState.REJECTED: frozenset(),
    OrderState.EXPIRED: frozenset(),
}


class IllegalTransition(Exception):
    """A state write that the transition table forbids. Raised, never swallowed."""


def is_terminal(state: OrderState) -> bool:
    return state in TERMINAL_STATES


def is_legal(current: OrderState, target: OrderState) -> bool:
    # A re-read returning the SAME state is always a legal no-op (polling a
    # still-working order, or re-reading a settled one, is never an error). Real
    # MOVES are governed by the transition table; an impossible move (FILLED ->
    # PLACED) is what this catches.
    if current == target:
        return True
    return target in LEGAL_TRANSITIONS[current]


def assert_legal(current: OrderState, target: OrderState) -> None:
    if not is_legal(current, target):
        raise IllegalTransition(f"{current.value} -> {target.value} is not a legal order transition")


@dataclass(frozen=True)
class OrderView:
    """Our tracked view of one live order: its current state, the largest filled
    quantity we have EVER seen, and the fill price at that high-water mark. The
    high-water mark is the whole point of this type (see below)."""

    current: OrderState
    filled_qty: int
    avg_fill_price: Decimal | None

    @classmethod
    def planned(cls) -> OrderView:
        return cls(current=OrderState.PLANNED, filled_qty=0, avg_fill_price=None)


def apply_broker_status(view: OrderView, read: BrokerOrder) -> OrderView:
    """Fold a broker status read into our view, honoring the invariants (3.2):

    - filled_qty only ever INCREASES: keep the largest quantity ever seen. A
      cancel does not stop fills already in flight, so a read that reports a
      smaller filled_qty than we already booked is stale and must not shrink our
      size (the slow-fill-after-cancel class). The fill price is kept
      from the read whose filled_qty set the high-water mark.
    - a cancel is NOT terminal while quantity is still filling: if the broker
      reports CANCELED but its filled_qty exceeds what we had, we advance the
      fill first; CANCELED with a partial fill books the partial (state CANCELED,
      filled_qty > 0), it does not discard it.
    - the resulting transition must be legal, else raise: a broker status we
      cannot legally reach from here is a modelling bug, surfaced loudly.
    """
    new_filled = max(view.filled_qty, read.filled_qty)
    # The fill price travels with the high-water mark: adopt the read's price
    # only when this read is the one that advanced (or set) filled_qty.
    if read.filled_qty >= view.filled_qty and read.avg_fill_price is not None:
        new_price = read.avg_fill_price
    else:
        new_price = view.avg_fill_price

    target = read.state
    # A broker that reports FILLED must report the full quantity as filled; if it
    # says FILLED but we have booked MORE than it reports, we still trust the
    # high-water mark for size (never shrink), but the state can advance to FILLED.
    assert_legal(view.current, target)
    return replace(view, current=target, filled_qty=new_filled, avg_fill_price=new_price)


def to_unknown(view: OrderView) -> OrderView:
    """Move a working order to UNKNOWN after a read failure/timeout post-place.
    Fail closed: UNKNOWN blocks new entries for the strategy (enforced in guards)
    until the reconciler reads a true status. Only legal from PLACED/PARTIAL; a
    terminal order stays terminal (a timeout reading an already-final order does
    not un-finalize it)."""
    if is_terminal(view.current):
        return view
    assert_legal(view.current, OrderState.UNKNOWN)
    return replace(view, current=OrderState.UNKNOWN)
