"""The controls fire drill: ask each control, on purpose, the three questions a decorative
control fails. CONNECTED: did its real producer write recently? RIGHT QUESTION: handed a
known-bad input, does it refuse? CADENCE: does the producer run inside its consumer's
freshness bar? A check returns ok, noop (the control would not protect: page a human), warn
(unverifiable right now, e.g. the market is closed) or skip (not applicable). Read-only.

These are the pure checks; a deployment composes them with its own reads and adds any
behavioral checks against its real modules (the shipped example drill shows the shape).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

RECONCILE_BAR = timedelta(minutes=30)
SELFTEST_BAR = timedelta(hours=20)


@dataclass(frozen=True)
class DrillResult:
    name: str
    status: str  # ok | noop | warn | skip
    detail: str


def check_reconcile_cadence(interval_sec: float, bar: timedelta = RECONCILE_BAR) -> DrillResult:
    """A reconcile writer slower than the gate's bar means the gate can only ever halt."""
    if interval_sec < bar.total_seconds():
        return DrillResult("reconcile cadence", "ok", f"runs every {interval_sec / 60:.0f}m inside the {bar} bar")
    return DrillResult("reconcile cadence", "noop", f"runs every {interval_sec / 60:.0f}m, slower than the {bar} gate bar")


def check_health_row(name: str, rec, now: datetime, bar: timedelta, *, market_open: bool, must_exist: bool = True) -> DrillResult:
    if rec is None:
        return DrillResult(name, "noop" if must_exist else "skip", "never written by its producer")
    if rec.last_result == "fail":
        return DrillResult(name, "noop", f"failing: {rec.detail or 'no detail'}")
    if rec.last_ok_at is None:
        return DrillResult(name, "noop", "has never passed")
    age = now - rec.last_ok_at
    if age > bar:
        return DrillResult(name, "noop" if market_open else "warn", f"last pass {age.total_seconds() / 3600:.1f}h ago, bar {bar}")
    return DrillResult(name, "ok", f"passed {age.total_seconds() / 60:.0f}m ago")


def check_resting_closes(open_lots: list[tuple[str, str]], resting: set[tuple[str, str]], *, market_open: bool) -> DrillResult:
    """Every open lot should carry its strategy's resting take-profit close."""
    if not open_lots:
        return DrillResult("resting closes", "skip", "no open lot")
    bare = [f"{a} {c}" for a, c in open_lots if (a, c) not in resting]
    if not bare:
        return DrillResult("resting closes", "ok", f"{len(open_lots)} open lot(s) all carry a resting close")
    return DrillResult("resting closes", "noop" if market_open else "warn", "no resting close on " + ", ".join(bare))


def summarize_drill(results: list[DrillResult]) -> tuple[bool, str]:
    bad = [r for r in results if r.status == "noop"]
    return (not bad), "\n".join(f"{r.status.upper():<5} {r.name}: {r.detail}" for r in results)
