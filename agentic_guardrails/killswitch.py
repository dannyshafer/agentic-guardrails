"""Layered halt controls + the dead-man gate (docs plan 4.6), pure.

Nothing here places or cancels; it DECIDES whether the engine may arm and place.
The I/O shell reads these decisions every cycle and refuses to place when any of
them says halt. Layers, any one of which halts new placements:

  1. executor-global flag OFF          (dark-shipped or manually killed)
  2. per-strategy daily loss stop       (this arm lost more than its stop today)
  3. global daily loss stop             (the whole book lost more than its stop)
  4. dead-man gate: the last reconcile OR the last morning self-test failed or is
     STALE. If we cannot prove state is clean and current, we do not trade.

Robinhood's own one-tap disconnect is the human's out-of-band last resort and is
not modelled here (it works even if our code is wedged).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal


@dataclass(frozen=True)
class HaltDecision:
    halted: bool
    reason: str


CLEAR = HaltDecision(False, "clear")


@dataclass(frozen=True)
class Freshness:
    """When a health signal (reconcile, self-test) last succeeded. None = never
    succeeded this session, which is itself stale (fail closed)."""

    last_ok_at: datetime | None


def _stale(fresh: Freshness, now: datetime, max_age: timedelta) -> bool:
    if fresh.last_ok_at is None:
        return True
    return (now - fresh.last_ok_at) > max_age


@dataclass(frozen=True)
class KillState:
    """Everything the layered check needs, all supplied by the I/O shell."""

    executor_enabled: bool
    strategy_pnl_today: Decimal
    strategy_daily_stop: Decimal | None  # positive dollars; loss beyond -stop halts the arm
    global_pnl_today: Decimal
    global_daily_stop: Decimal | None
    last_reconcile_ok: Freshness
    last_selftest_ok: Freshness


def deadman_gate(state: KillState, now: datetime, *, max_reconcile_age: timedelta, max_selftest_age: timedelta) -> HaltDecision:
    """Arm only if BOTH the last reconcile and the last self-test are recent and
    succeeded. A missing or stale signal halts (fail closed)."""
    if _stale(state.last_reconcile_ok, now, max_reconcile_age):
        return HaltDecision(True, "dead-man gate: last clean reconcile is missing or stale")
    if _stale(state.last_selftest_ok, now, max_selftest_age):
        return HaltDecision(True, "dead-man gate: last passing self-test is missing or stale")
    return CLEAR


def _loss_stop(name: str, pnl_today: Decimal, stop: Decimal | None) -> HaltDecision:
    # stop is a positive dollar amount; a realized loss at or beyond -stop halts.
    if stop is None or stop <= 0:
        return CLEAR
    if pnl_today <= -stop:
        return HaltDecision(True, f"{name} daily loss stop: pnl {pnl_today} <= -{stop}")
    return CLEAR


def evaluate_halt(
    state: KillState,
    now: datetime,
    *,
    max_reconcile_age: timedelta = timedelta(minutes=30),
    max_selftest_age: timedelta = timedelta(hours=20),
) -> HaltDecision:
    """The full layered check, first binding layer wins. Returns CLEAR only when
    every layer is clear."""
    if not state.executor_enabled:
        return HaltDecision(True, "executor is disabled (OPTIONS_EXECUTOR_ENABLED off)")
    for res in (
        _loss_stop("strategy", state.strategy_pnl_today, state.strategy_daily_stop),
        _loss_stop("global", state.global_pnl_today, state.global_daily_stop),
        deadman_gate(state, now, max_reconcile_age=max_reconcile_age, max_selftest_age=max_selftest_age),
    ):
        if res.halted:
            return res
    return CLEAR
