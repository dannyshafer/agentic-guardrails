"""How many contracts a close may actually send, pure.

The over-close stories: a venue rejects a sell with "insufficient qty available (available: 0)"
because a resting stop already reserves the whole position; a bot with duplicated lots sent
exits for more than it held and opened a short; a restart rebuilt a position at its ORIGINAL
size after partial exits and closed too much. One rule covers them: a close is sized from the
BROKER's position, minus what resting closes already reserve, never from the bot's own lot
table, and a close that would flip the sign is cut to flat.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CloseSize:
    send: int  # contracts to send now (0 = nothing to do)
    reason: str | None  # why it differs from what was asked, or None


def closable_qty(requested: int, broker_qty: int, resting_close_qty: int) -> CloseSize:
    """`broker_qty` is signed (short negative); `resting_close_qty` is contracts already reserved by
    working orders on the opposite side. A request beyond what remains is cut, and the cut is
    reported so the caller can log why the position is not flat yet."""
    if requested <= 0:
        return CloseSize(0, "nothing requested")
    held = abs(broker_qty)
    if held == 0:
        return CloseSize(0, "broker shows no position; a close would open the opposite side")
    free = held - max(0, resting_close_qty)
    if free <= 0:
        return CloseSize(0, f"all {held} contracts are already reserved by resting closes; cancel one before sending another")
    if requested > free:
        return CloseSize(free, f"cut from {requested} to {free}: {held} held, {resting_close_qty} reserved by resting closes")
    return CloseSize(requested, None)


def resting_opposite_in_the_way(resting_sides: list[str], close_side: str) -> bool:
    """A venue that enforces self-match protection rejects a close while an opposite-side order
    rests on the same contract (the wash-trade reject). Cancel those first, then re-read."""
    return any(s != close_side for s in resting_sides)
