# The controls

What each control is, what it refuses, what it costs, and the incident that put it there. The
README tells the stories; this is the reference. Every item names the module and the function
so you can read the code it describes.

A rule of this package: a control that cannot run proves nothing. Every check below fails
CLOSED on an unreadable input (halts the open, reports the drift, acts rather than holds), and
the one class of action that is never halted is a protective close.

## Pre-trade guards (`guards.py`, run in order by `Engine.submit`)

| guard | refuses | why it exists |
|---|---|---|
| `guard_has_limit` | any order without a limit price | there are no market orders in an options book; a marketable close is a limit at the ask |
| `guard_not_adjusted` | a contract whose multiplier or root says it was adjusted (split, special dividend) | an adjusted contract's quote and collateral math are wrong in ways the venue does not flag |
| `guard_one_in_flight` | a second order on a contract that already has one unsettled | the same-tick duplicate; a resting close from another strategy is ignored only for a resting-close plan |
| `guard_unknown_blocks_opens` | a new open while any order is in the UNKNOWN state | "I sent it and cannot read the answer" means exposure is unknown; closes still run |
| `guard_cash_secured` | an open whose collateral exceeds free cash | the venue checks buying power, we check cash: cash already includes reserved collateral (incident 6) |
| `guard_caps` | collateral over the per-order, per-ticker, per-strategy, per-day cap | the fixed or proportional caps in `config.FixedCaps` / `ProportionalCaps` |
| `guard_factor_concentration` | collateral over the per-factor cap, factor from the `FactorMap` | several wrappers on one index are one bet; name the factor or they stack |
| `guard_expiry_concentration` | collateral over the per-expiry cap | one gap date hits every short on that expiry |
| `guard_portfolio_concentration` | collateral over the whole-book cap | the outer bound |
| `guard_no_earnings_in_window` | a sale across an earnings date, asking the provider about the UNDERLYING single name (NVDL asks NVDA) | the wrong-question defect of incident 1; an unmapped basket fails closed |
| `guard_spread_structure` | a two-leg plan that is not a valid vertical (wing on the wrong side, same strike, mismatched expiry) | raises before any cap sees it |
| `guard_spread_concurrency` | more open spreads per strategy than allowed, or a net credit under the fraction-of-width floor | a thin-credit spread is a free short wing |

Every guard takes an `ExecContext` the engine builds from the ledger in `Engine._context`,
including the unfilled remainder of in-flight opens (`ledger.inflight_open_remainders`, the
same-tick overshoot, incident 7). A guard whose input is missing from the context denies.

## Idempotency (`ids.py`, `state_machine.py`, `ledger.py`)

- `client_order_id(strategy, contract, intent, date)` is deterministic. Re-running a tick with
  the same signal produces the same id; the engine refuses a plan whose id is already tracked
  in any state but PLANNED. A re-run never double-places.
- `broker_ref_id(coid)` is the wire form (a UUID5 of the internal id). When a venue changes
  what it accepts as an idempotency key, the wire form changes and the internal key does not
  (incident 9).
- The state machine admits only the transitions a venue can actually produce. An order that
  reads as filled-0 and cancelled is terminal, and a later attempt needs a NEW id:
  `runtime.guard_loop.next_close_coid` mints `base_r1`, `base_r2`, ... after a terminal
  non-fill (incident 3) and stops at a bound.

## The dead-man gate (`killswitch.py`, `health.py`, `runtime/kill.py`)

- `deadman_gate(reconcile_age, selftest_age, windows)` halts NEW OPENS when the position
  reconcile is older than 30 minutes or the self-test older than 20 hours
  (`config.DeadmanWindows`). It reads real health rows; a missing or unreadable row is stale.
- `runtime.kill.open_kill_state` adds the daily loss stop (`runtime.vetting.realized_pnl_today`,
  which pairs each close with its open and FAILS CLOSED on a close it cannot attribute) and the
  enabled flag.
- `runtime.kill.guard_kill_state` is the protective-close state: always armed. Halting a close
  that reduces assignment risk is worse than any reason to halt.
- The cadence rule: the writer of a health row must run more often than its reader's window.
  `health.should_run` is pinned against `DeadmanWindows` in the tests so they cannot drift (the
  "daily writer, 30-minute reader" permanent halt of incident 1). The self-test also runs once at
  startup, not only post-close (incident 8).
- The self-test's canary contract rolls its own expiry (`brokers.robinhood.rolled_canary`); a
  dated canary expires and reads as a broker failure (incident 8).

## The reconciler (`reconciler.py`)

Compares the ledger's net positions with the broker's positions, not its order list (the venue
may not echo our id). OVER drift (broker holds more than the ledger knows) blocks opens; UNDER
drift is reported and never silently "healed" into a passing reconcile; an adjusted contract is
its own class and must be acknowledged by a human. The reconcile is the backstop for every
parse failure on the order path (incident 2).

## The assignment guard (`assignment_guard.py`, `runtime/guard_loop.py`)

Pure policy: by `hard_close_dte` (2) a short must be provably safe, else it is closed or rolled.
Before the cutoff it acts only on no-extrinsic / deep in the money, never on a near-strike touch
(`pre_cutoff_breach=False`; incident 4). Rolls are bounded (`max_roll_extension_days`,
nonzero bid, the roll leg's event guard checked BEFORE the buy-back) and a roll only after the
close has fully filled. The loop defers execution to market hours, cancels resting closes first
and re-reads the broker position before acting, pads a retry's limit, and reads spot from two
feeds (`runtime.spot.guard_spot`: the lower price when they disagree, which only makes the guard
act sooner; one dead feed falls back to the other; both dead is None and the loop acts rather
than holds).

## Open-side vetting (`runtime/vetting.py`)

`open_sanity_reason` stands an open down for: credit under $0.10, annualised yield on the LIVE
limit under 15%, DTE under 5, a same-day re-entry after a guard close, a new short on an
underlier whose short is within 3% of strike, and a second position in a factor or expiry
already held beyond the stacking fraction. `open_limit` clamps the recorded bid to the live ask
(the paper-conservative basis). `scan_quote_disagreement` refuses to trade a signal whose
recorded quote no longer matches the live one.

## Fill audit and the drill (`runtime/fill_audit.py`, `runtime/drill.py`)

- The fill audit re-reads every recent filled order from the broker and compares quantity and
  average price. It records a health row and never edits the ledger: bookkeeping drift is not
  exposure drift, the reconcile owns exposure.
- The drill runs each control against a fake that should trip it and reports any control that
  did not. A control that crashes reads as `noop`, because a check that cannot run proves
  nothing. It paid for itself on its first run: its own ledger check was a no-op because of an
  import the container did not have.

## What is deliberately not here

No stop-loss on short premium or on spreads (measured as net negative at every level: the
losses arrive as gaps, and a stop fills after the gap). No adaptive sizing as a response to
losing. No regime gate on the money path (a trend filter is advisory in the program this came
from; making it a hard gate is a reviewed change there, not a default here). No multi-leg
placement on Robinhood.
