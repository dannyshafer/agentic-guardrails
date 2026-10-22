"""The runtime layer: the generic half of the loops that feed the engine.

The core decides whether ONE order may go out. These modules are the glue a real deployment
needs around it: the kill state for opens vs protective closes, the open-side vetting floors,
a two-feed underlying spot, the broker-truth fill audit, the controls fire drill and the
assignment-guard loop. Everything here is I/O-free except the two loops, which take the broker,
ledger and engine as arguments and never construct them.
"""

from agentic_guardrails.runtime.drill import DrillResult, check_health_row, check_reconcile_cadence, check_resting_closes, summarize_drill
from agentic_guardrails.runtime.fill_audit import KIND_FILL_AUDIT, FillDrift, audit_candidates, compare_fill, run_fill_audit
from agentic_guardrails.runtime.guard_loop import AssignmentGuardLoop, GuardLoopHooks, close_limit, next_close_coid
from agentic_guardrails.runtime.kill import guard_kill_state, open_kill_state
from agentic_guardrails.runtime.spot import guard_spot, readable_spot
from agentic_guardrails.runtime.vetting import (
    limit_below_live_bid_reason,
    open_limit,
    open_sanity_reason,
    realized_pnl_today,
    scan_quote_disagreement,
)

__all__ = [
    "KIND_FILL_AUDIT",
    "AssignmentGuardLoop",
    "DrillResult",
    "FillDrift",
    "GuardLoopHooks",
    "audit_candidates",
    "check_health_row",
    "check_reconcile_cadence",
    "check_resting_closes",
    "close_limit",
    "compare_fill",
    "guard_kill_state",
    "guard_spot",
    "limit_below_live_bid_reason",
    "next_close_coid",
    "open_kill_state",
    "open_limit",
    "open_sanity_reason",
    "readable_spot",
    "realized_pnl_today",
    "run_fill_audit",
    "scan_quote_disagreement",
    "summarize_drill",
]
