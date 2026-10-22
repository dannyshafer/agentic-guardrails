"""Configuration dataclasses: everything the runtime layer reads, with the production defaults.

The core takes its limits as `guards.Caps` (dollars) and `EngineConfig`; the extracting project's
scanner computed those from ~25 environment settings each tick. These two dataclasses are the
package's replacement for that settings object, so the runtime layer (caps, open vetting, the
guard loop) has one typed input instead of a reference to someone else's config module.

Defaults are the values the production book ran on at extraction, stated here so a
reader sees what a real deployment chose, not because they suit any other account. Every number
is a money-path value in the originating project: changed only through a reviewed promotion
gate, never by a tick of the runtime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from agentic_guardrails import guards
from agentic_guardrails.assignment_guard import GuardConfig
from agentic_guardrails.factors import POLYSENTINEL_FACTORS, FactorMap

if TYPE_CHECKING:
    from agentic_guardrails.engine import EngineConfig


def _d(x: float | int | str | Decimal) -> Decimal:
    return Decimal(str(x))


@dataclass(frozen=True)
class FixedCaps:
    """Dollar ceilings used when the account cannot be read, or when proportional caps are off."""

    per_order: Decimal = _d(8000)
    per_ticker: Decimal = _d(8000)
    per_strategy: Decimal = _d(8000)
    per_day: Decimal = _d(8000)
    per_factor: Decimal = _d(8000)
    per_expiry: Decimal = _d(8000)
    portfolio: Decimal = _d(16000)

    def as_caps(self, *, max_open_spreads_per_strategy: int | None = None, min_spread_credit_frac: Decimal | None = None) -> guards.Caps:
        return guards.Caps(
            max_open_spreads_per_strategy=max_open_spreads_per_strategy,
            min_spread_credit_frac=min_spread_credit_frac,
            per_order=self.per_order,
            per_ticker=self.per_ticker,
            per_strategy=self.per_strategy,
            per_day=self.per_day,
            per_factor=self.per_factor,
            per_expiry=self.per_expiry,
            per_portfolio=self.portfolio,
        )


@dataclass(frozen=True)
class ProportionalCaps:
    """Caps as fractions of live account equity, re-read once per open-loop tick.

    They scale in BOTH directions and have no floor: a low read tightens. A failed read falls
    back to `FixedCaps` (the known-good default) rather than to "no cap". `stack` bounds any
    SECOND position in a factor or expiry already held, so one position may use idle cash while
    a correlated second one cannot."""

    enabled: bool = True
    portfolio: Decimal = _d("1.0")
    day: Decimal = _d("1.0")
    order: Decimal = _d("0.75")
    expiry: Decimal = _d("0.75")
    stack: Decimal = _d("0.40")

    def caps_for(
        self,
        equity: Decimal | None,
        fallback: FixedCaps,
        *,
        max_open_spreads_per_strategy: int | None = None,
        min_spread_credit_frac: Decimal | None = None,
    ) -> guards.Caps:
        """Dollar caps for this tick. `equity` is CASH (collateral already reserved is inside
        it; adding open collateral back double-counts, incident 6), None when the
        account read failed."""
        spread = dict(max_open_spreads_per_strategy=max_open_spreads_per_strategy, min_spread_credit_frac=min_spread_credit_frac)
        if not self.enabled or equity is None or equity <= 0:
            return fallback.as_caps(**spread)
        per_order = (equity * self.order).quantize(Decimal("0.01"))
        return guards.Caps(
            **spread,
            per_order=per_order,
            per_ticker=per_order,
            per_strategy=(equity * self.portfolio).quantize(Decimal("0.01")),
            per_day=(equity * self.day).quantize(Decimal("0.01")),
            per_factor=per_order,
            per_expiry=(equity * self.expiry).quantize(Decimal("0.01")),
            per_portfolio=(equity * self.portfolio).quantize(Decimal("0.01")),
        )


@dataclass(frozen=True)
class OpenSanity:
    """Floors an opening order must clear before it is worth sending (incident 4)."""

    min_credit: Decimal = _d("0.10")  # per share
    min_annual_yield: Decimal = _d("0.15")  # on the live limit, not the recorded quote
    min_dte: int = 5  # an open inside the assignment guard's cutoff reach just churns
    threat_cushion_pct: Decimal = _d("0.03")  # no new short on an underlier whose short is this close to strike
    no_same_day_reentry_after_guard_close: bool = True


@dataclass(frozen=True)
class DeadmanWindows:
    """How stale the reconcile and the self-test may be before new opens halt. Protective
    closes are never halted by these; the guard loop reads a separate, always-armed state."""

    max_reconcile_age: timedelta = timedelta(minutes=30)
    max_selftest_age: timedelta = timedelta(hours=20)


@dataclass(frozen=True)
class GuardrailConfig:
    enabled: bool = False  # layer 1 of the kill switch: off means the open loop no-ops
    shadow: bool = True  # record the exact order, send nothing
    guard_enabled: bool = True
    guard_shadow: bool = True
    fixed_caps: FixedCaps = FixedCaps()
    proportional_caps: ProportionalCaps = ProportionalCaps()
    open_sanity: OpenSanity = OpenSanity()
    deadman: DeadmanWindows = DeadmanWindows()
    daily_loss_stop: Decimal = _d(1500)  # realized today; a close with no matching open stands the tick down
    max_opens_per_run: int = 12  # anti-runaway backstop only; the caps are the control
    max_open_spreads_per_strategy: int = 4
    min_spread_credit_frac: Decimal = _d("0.10")
    close_fraction: Decimal = _d("0.5")  # resting take-profit at this fraction of the open credit
    factors: FactorMap = POLYSENTINEL_FACTORS
    assignment_guard: GuardConfig = field(default_factory=GuardConfig)

    def caps_for(self, equity: Decimal | None) -> guards.Caps:
        """The dollar caps for this tick, spread limits included. `equity` is cash, None when the
        account read failed (fixed caps then). This is the one builder a runtime should call; the
        two cap dataclasses' own builders take the spread limits as arguments so nothing is
        dropped when they are used directly."""
        return self.proportional_caps.caps_for(
            equity,
            self.fixed_caps,
            max_open_spreads_per_strategy=self.max_open_spreads_per_strategy,
            min_spread_credit_frac=self.min_spread_credit_frac,
        )

    def engine_config(self, caps: guards.Caps | None = None, **overrides) -> EngineConfig:
        """An `EngineConfig` in this config's shadow mode carrying this config's `factors`; caps
        default to the fixed caps with the spread limits (pass `self.caps_for(cash)` for the
        proportional ones)."""
        from agentic_guardrails.engine import EngineConfig

        return EngineConfig(
            **{"caps": caps if caps is not None else self.caps_for(None), "shadow": self.shadow, "factors": self.factors, **overrides}
        )


@dataclass(frozen=True)
class RobinhoodConfig:
    """The Robinhood Agentic MCP adapter's inputs. Nothing here is a secret: the OAuth token lives
    in `token_path` (written mode 0600 by the client), never in config."""

    endpoint: str = "https://agent.robinhood.com/mcp/trading"
    token_path: str = "~/.agentic_guardrails/robinhood_token.json"
    account_id: str | None = None
    protocol_version: str = "2025-06-18"
    timeout_s: float = 30.0
