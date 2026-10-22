# agentic-guardrails

Let an AI agent trade your brokerage account without it hurting you.

I pulled this out of the options income book my agent has been trading for real on Robinhood. There is no strategy in here and no signal. What is in here is every
control that has stood between one of the agent's orders and a loss that was not supposed to
happen:

- a deterministic order state machine and idempotent client order ids (a re-run never
  double-places, a terminal unfilled order never blocks the next attempt);
- eleven pre-trade guards: limit present, not an adjusted contract, one in flight per
  contract, unknown-state blocks opens, cash secured, collateral caps per order, ticker,
  strategy, day, expiry, factor and portfolio, no sale through an earnings report, spread
  structure and spread concurrency;
- a dead-man gate that halts new opens when the position reconcile or the broker self-test
  is stale, and never halts a protective close;
- a reconciler that classifies ledger-versus-broker drift as OVER (blocking) or UNDER;
- an assignment guard that decides, deterministically, when a short option must be closed or
  rolled so it is never assigned;
- a fault-injection `SimBroker` so every rule above is tested offline, plus the Robinhood MCP
  adapter that has survived the live API's typing quirks.

Status: v0.3.0. The core, the file-backed stores (`SqliteLedger`, `JsonFileHealthStore`), a
user-supplied `FactorMap`, the typed `GuardrailConfig`, the runtime layer
(`agentic_guardrails.runtime`: kill states for opens vs protective closes, the open-side vetting
floors, a two-feed spot, the broker-truth fill audit, the controls drill, the assignment-guard
loop), the broker adapters (`agentic_guardrails.brokers`) and the Robinhood read-only check are
in. See `examples/open_loop.py` for the order of operations end to end against no account,
`docs/controls.md` for what each control is and what it costs, and `docs/checklist.md` for the
three questions to ask of any new control before trusting it.

## Why this exists: nine times real money found a hole

I run a small options income book with an AI agent placing the orders. Every control in this
package is here because something went wrong with real money first. The full stories are in
[`docs/incidents.md`](docs/incidents.md), each as the hole and then the control that closed it.
The short version, grouped by who is likely to hit the same thing:

**If you run any agent that moves money, you will meet these:**

| incident | the control it produced |
|---|---|
| Eight controls that compiled, linted, passed their tests and did nothing | `docs/checklist.md`: wired, asked the right question, run on the reader's cadence; engine-level tests |
| A protective close that locked itself out behind its own idempotency key | fresh attempt ids after a terminal non-fill, bounded retries, market-hours deferral |
| Caps twice the size I thought: cash already included the collateral | equity is cash alone; a failed account read falls back to fixed caps |
| Two orders in one tick overshot a cap before the first had settled | in-flight remainders count in every cap scope |
| A self-test that only ran after the close, and a canary contract that expired | self-test at startup too; the canary rolls its own expiry |
| The venue changed what it accepts as an order id and every order was rejected for days | the wire id is derived at the boundary; a dead place path is a health row |

**If you sell options with any automation, you will meet these:**

| incident | the control it produced |
|---|---|
| A near-strike rule that was a stop-loss in disguise, and bought back healthy puts | the assignment guard acts at two days to expiry or on no extrinsic value, never on proximity; open-side sanity floors |
| A take-profit that was really a poll | resting GTC closes; the guard cancels resting closes before placing its own |

**If you build on Robinhood's agent endpoint, you will meet this on day one:**

| incident | the control it produced |
|---|---|
| The first fill the ledger never saw: a singular response where the parser expected a list | dict-valued keys parse as one record; UNKNOWN state; the reconciler reads positions, not orders |

The id-format change above also happened on this endpoint, and the order body's mixed typing is
handled in the adapter; the read-only check proves the whole surface before any order goes out.

The thing all nine have in common: a control is only real when it is wired to its real inputs,
asked the right question, and run often enough for whatever reads it. That is the checklist.

## Brokers

| adapter | status | idempotency recovery | multi-leg | venue review |
|---|---|---|---|---|
| `SimBroker` | fault injection for tests | by client id | yes | scripted |
| `DryRunBroker` | paper: fills by policy, never touches a venue | by client id | yes | local |
| `RobinhoodMCPBroker` | has run real money | `ref_id` (UUID5 of the client id) | the venue refuses multi-leg today | `review_option_order` |
| `SchwabBroker` | written to the Trader API, no live order yet | this process's memory only (Schwab has no client order id) | VERTICAL | none (local) |
| `TradierBroker` | written to the Brokerage API, no live order yet | exact, via `tag` | `class=multileg` | `preview=true` |
| `TastytradeBroker` | written to the Open API, no live order yet | this process's memory only | one order, two legs | `/orders/dry-run` |
| `IBKRBroker` | written to the Client Portal Web API, no live order yet | exact, via `cOID` | combo (`conidex`) | `/orders/whatif` |
| `AlpacaBroker` | written to the Trading API, no live order yet | exact, via `client_order_id` | `order_class=mleg` | none (local) |

Fidelity publishes no trading API and cannot be supported. The five REST adapters are tested
offline against recorded shapes over a scripted transport; the first live order through each one
is its acceptance test, so keep it probe-sized and watch the ledger row. A venue with no client
order id (Schwab, tastytrade) relies on the engine's one-in-flight guard and the position
reconcile after a restart, which is exactly what those controls are for.

All adapters share `brokers.rest.HttpxTransport` (install the `rest` extra) and one error
mapping: a timeout after a place is the UNKNOWN class, a 4xx on a place is a terminal reject, a
4xx on a read is malformed (never papered over), 401/403 is an expired session, 429 and 5xx are
transient. Nothing retries a place. Nothing in here starts on import; orders go out
only through a `BrokerAdapter` you construct, and only with `EngineConfig.shadow=False`.

### Robinhood: the read-only check

Before the first order, prove the adapter end to end without trading:

```
export AG_RH_TOKEN_PATH=/path/to/robinhood_token.json   # the OAuth bundle exported from the desktop `claude mcp` flow
export AG_RH_ACCOUNT_ID=<the agentic-enabled account number>
python -m agentic_guardrails.brokers.robinhood
```

It lists the live tool surface and confirms every configured tool name is on it, reads the
account and positions, quotes a canary contract that rolls its own expiry, reads the tick size,
and scores the result exactly as the dead-man gate's self-test would (exit 0 on a pass). It never
places, reviews or cancels, and by default it never refreshes the OAuth token: a second process
rotating the refresh token under a running executor is how a live bot loses its session, so a
401 is reported, not repaired (pass `--allow-refresh` when nothing else holds the token). The
check has run green against a funded account: every tool we use present on the surface, account
and positions read, canary quoted and ticked.

In code, `brokers.robinhood.build_broker(RobinhoodConfig(...))` gives you the adapter the engine
takes, with `DEFAULT_TOOLS` (the verified tool-name set) unless you pass `tools=`. To honour the
environment the way the CLI does, pass both: `build_broker(config_from_env(),
tools=tools_from_env())`, where `tools_from_env` overrides one verb at a time
(`AG_RH_TOOL_QUOTE`, ...). Build it once per process: it holds the MCP session and the
instrument caches.

## Install

```
pip install agentic-guardrails              # the core: stdlib only
pip install "agentic-guardrails[robinhood]" # adds httpx for the Robinhood MCP adapter
pip install "agentic-guardrails[rest]"      # adds httpx for the Schwab, Tradier, tastytrade, IBKR and Alpaca adapters
```

Python 3.12 or newer. Nothing runs on import, nothing phones home, and no order leaves the
process unless you construct a broker adapter and set `EngineConfig.shadow=False`.

## Minimal use

```python
from agentic_guardrails import Engine, GuardrailConfig, JsonFileHealthStore, SimBroker, SqliteLedger

cfg = GuardrailConfig()                      # enabled=False, shadow=True: nothing is sent
caps = cfg.caps_for(cash)                    # proportional to cash; fixed caps when cash is None
engine = Engine(SimBroker(), SqliteLedger("orders.db"), cfg.engine_config(caps))
```

The health store feeds the dead-man gate; a store that cannot be read halts new opens and
never halts a protective close. Describe your own universe with `FactorMap`; the shipped
`POLYSENTINEL_FACTORS` is an example, not a recommendation.

## Non-goals

No strategy, no signal, no sizing advice, no promise of profit. No multi-leg placement on
Robinhood (the MCP refuses it today; the `review_option_order` probe that would detect support
is how you find out, not an assumption in the code). No hosted guard loop for other people's
accounts. The package will refuse an order it cannot prove safe; it will not tell you which
order to send.

## The audit file

The Robinhood adapter emits every MCP request and response as a record on a dedicated Python
logger (headers redacted, bodies kept). The bodies are quotes, positions, balances and order
acknowledgements: that is the point of an order audit trail, and it means those records hold your
account figures. The package creates no file for them; wherever you route that logger's handler,
treat it as sensitive and keep it out of any shared log shipper. The OAuth token file, by contrast,
is written by the package with mode 0600.

## Running the tests

```
pip install -e ".[dev]"
pytest
```
