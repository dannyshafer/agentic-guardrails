# Changelog

All notable changes to this package. The format follows Keep a Changelog; versions follow SemVer.
Until 1.0 a minor version may change an interface, and the entry here says so when it does.

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
