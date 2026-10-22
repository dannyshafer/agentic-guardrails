"""The runtime layer: the generic half of the loops that feed the engine.

The core decides whether ONE order may go out. These modules are the glue a real deployment
needs around it: the kill state for opens vs protective closes, the open-side vetting floors,
a two-feed underlying spot, the broker-truth fill audit, the controls fire drill and the
assignment-guard loop, plus the guards built from other people's reported venue failures
(docs/venue_failure_modes.md): session calendar, quote freshness, expiry-day rules, credential
leases, the single-instance lock, close sizing, order adoption and rate budgets. Everything here is I/O-free except the two loops, which take the broker,
ledger and engine as arguments and never construct them.
"""

from agentic_guardrails.runtime.adopt import Adoption, Candidate, match_unidentified
from agentic_guardrails.runtime.auth_lease import VENUE_HARD_LIFETIMES, Lease, open_blocked_reason as lease_open_blocked_reason
from agentic_guardrails.runtime.book_check import BookFinding, BookIssue, check_book, opens_blocked_tickers
from agentic_guardrails.runtime.calendar import (
    ET,
    assert_aware,
    bar_is_current,
    is_trading_day,
    market_open,
    minutes_to_close,
    next_session_open,
)
from agentic_guardrails.runtime.close_sizing import CloseSize, closable_qty, resting_opposite_in_the_way
from agentic_guardrails.runtime.drill import DrillResult, check_health_row, check_reconcile_cadence, check_resting_closes, summarize_drill
from agentic_guardrails.runtime.expiry import (
    assignment_window_open,
    ex_dividend_assignment_reason,
    expiry_settled_at,
    no_open_reason,
    pin_risk_reason,
)
from agentic_guardrails.runtime.fill_audit import KIND_FILL_AUDIT, FillDrift, audit_candidates, compare_fill, run_fill_audit
from agentic_guardrails.runtime.freshness import FrozenFeedDetector, quote_lag_reason
from agentic_guardrails.runtime.guard_loop import AssignmentGuardLoop, GuardLoopHooks, close_limit, next_close_coid
from agentic_guardrails.runtime.instance import InstanceLock, mode_mismatch_reason
from agentic_guardrails.runtime.kill import guard_kill_state, open_kill_state
from agentic_guardrails.runtime.ratelimit import VENUE_BUDGETS_PER_MIN, TokenBucket, backoff_seconds, retry_after_seconds
from agentic_guardrails.runtime.spot import guard_spot, readable_spot
from agentic_guardrails.runtime.vetting import (
    limit_below_live_bid_reason,
    open_limit,
    open_sanity_reason,
    realized_pnl_today,
    scan_quote_disagreement,
)

__all__ = [
    "Adoption",
    "Candidate",
    "match_unidentified",
    "VENUE_HARD_LIFETIMES",
    "Lease",
    "lease_open_blocked_reason",
    "BookFinding",
    "BookIssue",
    "check_book",
    "opens_blocked_tickers",
    "ET",
    "assert_aware",
    "bar_is_current",
    "is_trading_day",
    "market_open",
    "minutes_to_close",
    "next_session_open",
    "CloseSize",
    "closable_qty",
    "resting_opposite_in_the_way",
    "assignment_window_open",
    "ex_dividend_assignment_reason",
    "expiry_settled_at",
    "no_open_reason",
    "pin_risk_reason",
    "FrozenFeedDetector",
    "quote_lag_reason",
    "InstanceLock",
    "mode_mismatch_reason",
    "VENUE_BUDGETS_PER_MIN",
    "TokenBucket",
    "backoff_seconds",
    "retry_after_seconds",
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
