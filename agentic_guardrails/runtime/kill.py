"""The two kill states: one for OPENING risk, one for REDUCING it.

A protective close must never be blocked by the layers that exist to stop NEW risk. The
dead-man gate (stale reconcile or self-test) and the daily loss stops are about not opening
into an unknown or bad state; halting a close would leave the assignment risk in place, the
opposite of the guarantee. So the guard closes under a state that is always armed, while an
open reads the REAL health-store freshness and today's REAL realized P&L. A sentinel on the
open side would be the "control that looks like protection and does nothing" defect.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from agentic_guardrails import killswitch
from agentic_guardrails.config import GuardrailConfig


def guard_kill_state(now: datetime | None = None) -> killswitch.KillState:
    """Always-armed: closing is always allowed. The new STO leg of a ROLL still goes through
    every pre-trade guard inside `Engine.submit`, so a roll cannot create unbounded risk."""
    fresh = killswitch.Freshness(last_ok_at=now or datetime.now(timezone.utc))
    return killswitch.KillState(
        executor_enabled=True,
        strategy_pnl_today=Decimal(0),
        strategy_daily_stop=None,
        global_pnl_today=Decimal(0),
        global_daily_stop=None,
        last_reconcile_ok=fresh,
        last_selftest_ok=fresh,
    )


def open_kill_state(
    cfg: GuardrailConfig,
    reconcile_fresh: killswitch.Freshness,
    selftest_fresh: killswitch.Freshness,
    strategy_pnl_today: Decimal,
    global_pnl_today: Decimal,
) -> killswitch.KillState:
    """For an OPENING order: the dead-man gate reads the health store's freshness (pass the
    result of `health.kill_state_freshness`), the loss stop reads `vetting.realized_pnl_today`."""
    return killswitch.KillState(
        executor_enabled=cfg.enabled,
        strategy_pnl_today=strategy_pnl_today,
        strategy_daily_stop=cfg.daily_loss_stop,
        global_pnl_today=global_pnl_today,
        global_daily_stop=cfg.daily_loss_stop,
        last_reconcile_ok=reconcile_fresh,
        last_selftest_ok=selftest_fresh,
    )
