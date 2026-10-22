# The checklist: three questions for any new control

In one review session of this code's ancestor, eight defects shipped that compiled, passed
lint, and passed their own unit tests. Every one was the same class: code that reads as
protection while being a no-op. The list below is what catches that class. Run it on every
guard, gate, tripwire or check before trusting it, and write the test that answers each
question at the level where the answer lives.

## 1. Is it connected?

Trace the input from its real producer, not from the test. A guard wired into the guard list
whose input the engine never populates takes its dataclass default and allows everything,
while its unit test passes.

- Write the test at the engine, loop or job level, never only at the pure-function level.
- Assert that the context the guard receives carries the field, with a value that trips it.
- If the control reads a health row, assert that something WRITES that row on a cadence, and
  that a missing row reads as stale.

## 2. Is it asked the right question?

Assert the argument, not just the outcome. A test double that ignores its input cannot catch a
wrong-symbol or wrong-unit bug: the earnings guard asked about the ETF instead of the company it
holds, and its test's provider answered the same for any symbol.

- The double must answer differently for the right and the wrong question.
- Units: per-share versus per-contract, cash versus buying power, UTC date versus session date.
  Each of these has produced a silent miss.
- Symbols: the underlier of a wrapper, the OCC form of a venue's instrument id, the wire form of
  an internal id.

## 3. Does its cadence match its consumer's freshness bar?

A daily writer feeding a 30-minute staleness check is a permanent halt. A post-close-only
self-test feeding a 20-hour window is a morning halt after a restart.

- Pin the writer's interval against the reader's constant in a test so they cannot drift.
- Decide, per control, whether stale means halt (opens) or means act (protective closes), and
  test both branches.

## Corollaries

- "Compiles, lints, unit tests pass" says nothing about whether the SQL runs or the venue
  accepts the shape. Run new I/O code against the real thing in read-only mode before calling
  it done (`python -m agentic_guardrails.brokers.robinhood`).
- Read the real row after deploying, not just the logs. A never-passing check that reads as
  "passed in 1970" is visible only in the table.
- A symmetric time window plus "nearest" is a look-ahead. An execution-quality basis must be
  at or before the decision.
- Never pin a health check to a dated contract, strike or date. Compute it or roll it.
- When a venue changes an identifier's format, fix the wire boundary; never change the internal
  idempotency key.
- A repeated per-order reject page with no health row is the fingerprint of a dead place path.
  Make it a health row.
- Before fixing an apparently superfluous alert, look for the record of why it was kept.
