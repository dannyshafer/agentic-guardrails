# Changelog

All notable changes to this package. The format follows Keep a Changelog; versions follow SemVer.
Until 1.0 a minor version may change an interface, and the entry here says so when it does.

## 0.4.0 (2026-10-22)

Guards built from field-reported venue failures (see `docs/venue_failure_modes.md`, with sources).
Nothing in the engine's decision path changed; every addition is a pure helper a loop opts into.

- `venue_errors.classify_reject`: cross-venue reject text to a class and a policy;
  `classify_ibkr` for IBKR numbered errors.
- `brokers/rest.raise_for`: a 403 carrying reject text is `BrokerRejected`, not auth; a quota
  violation sent as HTTP 400 is `BrokerTransient`; transient errors carry the retry delay.
- `brokers/_common.tick_for` (index nickel grid) and `sub_display_min_reason`.
- `runtime/calendar`: ET sessions, holidays and early closes through the end of next year,
  naive datetimes refused, `bar_is_current`.
- `runtime/freshness`: `quote_lag_reason` and `FrozenFeedDetector`.
- `runtime/expiry`: `no_open_reason`, `pin_risk_reason`, `assignment_window_open`,
  `expiry_settled_at`, `ex_dividend_assignment_reason`.
- `runtime/auth_lease`: `Lease` with documented venue lifetimes, tiered warnings,
  `open_blocked_reason` against a guard horizon.
- `runtime/instance`: `InstanceLock`, `mode_mismatch_reason`.
- `runtime/close_sizing`: `closable_qty`, `resting_opposite_in_the_way`.
- `runtime/adopt`: `match_unidentified` for venues without a client order id.
- `runtime/ratelimit`: per-venue budgets, `TokenBucket`, `retry_after_seconds`, `backoff_seconds`.
- `runtime/book_check`: unexpected equity, orphan options, non-tradable holdings.

## 0.3.0 (2026-10-22)

- `EngineConfig.factors` takes a `FactorModel` (`factor_of`, `earnings_underlier`,
  `underlier_has_earnings`). With none set the engine reads the module table in `guards`, exactly
  as before. `ExecContext.factor` carries the plan's own factor so the concentration guard and the
  engine agree on the key.
- `FactorMap.earnings_underliers`: name the company a pooled wrapper tracks when its risk group is
  not that company. A missing earnings date for a symbol with an entry is a refusal.
- `GuardrailConfig.engine_config()` passes the config's factor map through.
- README: the nine incidents rewritten in first person.

## 0.2.0 (2026-10-22)

- `brokers.robinhood`: build the Robinhood MCP adapter from `RobinhoodConfig` or the `AG_RH_*`
  environment; `rolled_canary` keeps the self-test contract ahead of expiry; a read-only check
  (`python -m agentic_guardrails.brokers.robinhood`) that never refreshes the OAuth token.
- `docs/controls.md` and `docs/checklist.md`.

## 0.1.0 (2026-10-22)

- The execution core extracted from a live options income program: order state machine and
  idempotent ids, eleven pre-trade guards, the dead-man gate, the reconciler, the assignment
  guard, `SimBroker`.
- File-backed stores (`SqliteLedger`, `JsonFileHealthStore`), `FactorMap`, `GuardrailConfig`.
- `runtime/`: kill states, open-side vetting, two-feed spot, fill audit, controls drill, the
  assignment-guard loop.
- `brokers/`: `DryRunBroker` and offline-tested adapters for Schwab, Tradier, tastytrade, IBKR
  and Alpaca over a shared REST transport.
