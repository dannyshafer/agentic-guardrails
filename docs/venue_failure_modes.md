# Venue failure modes, from the field

What people running automated options trading actually hit, collected from GitHub issue
trackers, broker developer forums, Elite Trader, QuantConnect, Option Alpha and Alpaca
community threads, tradingqna and first-person post-mortems. Each row names the guard in this
package that addresses it. Nothing here is from our own book; the war stories in
[incidents.md](incidents.md) are. Wording of venue messages changes without notice, which is
why `venue_errors.classify_reject` matches loosely on the operative noun rather than exact text.

Legend for the "scope" column: **G** general to any venue, **O** options-specific, **V** one
venue's quirk.

## Authentication and sessions

| scope | what happens | guard | sources |
|---|---|---|---|
| V | Schwab's refresh token hard-expires after seven days; refreshing the access token does not extend it. The token endpoint then answers `invalid_client`, which reads like a bad app secret. A browser login is the only fix. | `runtime/auth_lease.py`: `Lease` with the documented lifetime, warnings a day and hours ahead, and `open_blocked_reason` refusing a new short the credential cannot guard to the next open | [schwab-py auth docs](https://schwab-py.readthedocs.io/en/latest/auth.html), [schwab-py #263](https://github.com/alexgolec/schwab-py/pull/263) |
| V | Schwab access tokens live thirty minutes; early client libraries failed the refresh at exactly that mark. A token file shared by two processes lets each invalidate the other's refresh. | `auth_lease`, `runtime/instance.py` lock (one process per token file) | [schwab-py #100](https://github.com/alexgolec/schwab-py/issues/100), [schwab-py token issues](https://github.com/alexgolec/schwab-py/issues?q=is%3Aissue+token) |
| V | tastytrade access tokens live fifteen minutes; a 401 with an HTML body means a missing `User-Agent`, not an expired token. The legacy session login was decommissioned and old code silently stopped. | `auth_lease` (unattended-renewable lease: never blocks, surfaces as `BrokerAuthExpired`) | [tastytrade FAQ](https://developer.tastytrade.com/docs/faq/), [tastyware #269](https://github.com/tastyware/tastytrade/issues/269) |
| V | tastytrade blocks the IP for about eight hours after repeated failed logins; every endpoint then times out with no error. | `brokers/rest.py` maps the timeout to `BrokerTimeout`; after a place that is UNKNOWN, never a retry | [tastytrade FAQ](https://developer.tastytrade.com/docs/faq/) |
| V | IBKR Client Portal sessions lapse without a keep-alive every minute, and a login elsewhere (phone app, TWS) kills the API session mid-day; the gateway restarts nightly and needs a human 2FA weekly. | `auth_lease` (`ibkr_cp_session`), `venue_errors.classify_ibkr` SESSION kind | [IBKR campus: authenticating the gateway](https://www.interactivebrokers.com/campus/trading-lessons/launching-and-authenticating-the-gateway/), [ibc #194](https://github.com/ibcalpha/ibc/issues/194) |
| G | Sandbox credentials against the production host (or the reverse) return an opaque 401; a production key with the bot believing it is paper trades real money. | `runtime/instance.py` `mode_mismatch_reason` refuses to start on a disagreement | [Tradier trading docs](https://docs.tradier.com/docs/trading), [tastytrade sandbox](https://developer.tastytrade.com/docs/sandbox/) |

## Placing orders

| scope | what happens | guard | sources |
|---|---|---|---|
| G | A place times out after the venue accepted it; the retry doubles the position. One author's live bot did this six times in a session on a 3x ETF. No venue we support offers a server-enforced idempotency key. | Engine: a timed-out place is UNKNOWN and opens are blocked until it is resolved. `runtime/adopt.py` finds the order by client tag where the venue echoes one, else by shape inside a window; two matches stay UNKNOWN for a human | [dev.to post-mortem](https://dev.to/azender1/my-trading-bot-tried-to-execute-the-same-trade-twice-that-became-safeagent-ffl), [tastytrade idempotency guide](https://developer.tastytrade.com/docs/guides/idempotency-and-retries/) |
| V | Schwab returns 201 with an empty body; the order id is only in the `Location` header, which a proxy may strip. | Schwab adapter raises `BrokerMalformed` with "order may have landed" (the UNKNOWN path), then `adopt` by shape | [schwab-py util docs](https://schwab-py.readthedocs.io/en/latest/util.html) |
| V | Tradier returns HTTP 200 for a request that downstream later rejects; the reject appears on the order's `errors` field. | Tradier adapter polls status after place; `classify_reject` on the `errors` text | [Tradier error responses](https://docs.tradier.com/docs/error-responses) |
| V | Alpaca puts a dozen different conditions behind one code (`40310000`): buying power, option level, wash trade, quantity held, expiring today. Branching on the code misclassifies them. | `venue_errors.classify_reject` on the message text; `rest.raise_for` routes a 403 with reject text to `BrokerRejected` rather than `BrokerAuthExpired` | [Alpaca options overview](https://docs.alpaca.markets/us/docs/options-trading-overview), [Alpaca forum: wash trade](https://forum.alpaca.markets/t/apierror-potential-wash-trade-detected-use-complex-orders/13441) |
| G | Self-match protection: a close is rejected while an opposite-side order rests on the same contract (a resting stop, a stale close from yesterday). Scripts crashed or retried into the reject. | `classify_reject` policy `CANCEL_RESTING_FIRST`; `close_sizing.resting_opposite_in_the_way`; the engine's `cancel_resting` before a protective close | [Alpaca user protection](https://docs.alpaca.markets/us/docs/user-protection), [Tradier `WashTradeAttempt`](https://docs.tradier.com/docs/error-responses) |
| G | "insufficient qty available" because a resting order already reserves the position; a second sell races it. Duplicate lots in a store sent exits for more than was held and opened a short. | `runtime/close_sizing.py` sizes every close from the broker's position minus resting reservations and never flips the sign | [Alpaca forum](https://forum.alpaca.markets/t/alpaca-py-apierror-insufficient-qty-available-for-order/17712), [robinhood-options-mobile #234](https://github.com/CIInc/robinhood-options-mobile/issues/234) |
| V | tastytrade relabels a sell-to-close as sell-to-open when its position ledger lags the fill by seconds, then rejects it on naked margin or option level. | `classify_reject` POSITION_INTENT / OPTION_LEVEL policies; the engine's one-in-flight rule serialises a close behind the open's settle | [Lean.Brokerages.Tastytrade #30](https://github.com/QuantConnect/Lean.Brokerages.Tastytrade/issues/30) |
| O | Pattern-day-trade and closing-only restrictions arrive as reject text, sometimes after the balance that triggered them has changed. | `classify_reject` policy `HALT_OPENS` (sticky for the session) | [Option Alpha community](https://app.optionalpha.com/community/posts/pattern-day-trading-202602079523), [Alpaca forum](https://forum.alpaca.markets/t/trade-denied-due-to-pattern-day-trading-protection-not-sure-why-i-get-this-error/2318) |
| O | Index options (SPX, RUT, NDX, VIX) trade on a nickel grid at every price; a penny-priced order is held to the close and then rejected. | `brokers/_common.tick_for` | [alpalo-income-pnl #15](https://github.com/jcvilap/alpalo-income-pnl/pull/15), [WealthLab thread](https://www.wealth-lab.com/Discussion/Trouble-exiting-options-positions-in-Schwab-12835) |
| O | A one-cent resting bid is accepted by some venues and displayed to nobody; it fills only when someone market-sells into it. | `brokers/_common.sub_display_min_reason` | [Elite Trader](https://www.elitetrader.com/et/threads/buying-selling-at-0-01-when-min-bid-for-display-to-market-is-0-05.311214/) |
| O | Multi-leg structures are rejected in venue-specific ways: one refuses closing a spread in one order, one bans time spreads on index options, one needs a negative limit for a credit. | `classify_reject` STRUCTURE_UNSUPPORTED policy `FIX_ORDER`; the engine's spread guard | [Alpaca forum](https://forum.alpaca.markets/t/closing-an-existing-multi-leg-options-position-in-a-single-order-fails/16802), [schwab-py order builder](https://schwab-py.readthedocs.io/en/latest/order-builder.html) |

## Rate limits and transport

| scope | what happens | guard | sources |
|---|---|---|---|
| V | Schwab allows 120 requests a minute and a separate order budget set at app registration; orders over it are rejected, not queued, and an immediate retry prolongs the throttle. | `runtime/ratelimit.py` token bucket set under the published limit; `retry_after_seconds` waits a full window when no header says otherwise | [schwab-py getting started](https://schwab-py.readthedocs.io/en/latest/getting-started.html), [LumiBot Schwab notes](https://lumibot.lumiwealth.com/brokers.schwab.html) |
| V | Tradier reports a quota violation as HTTP 400 plain text, not 429. A naive client reads it as a terminal reject. | `rest.raise_for` maps the body to `BrokerTransient` | [Tradier rate limiting](https://docs.tradier.com/docs/rate-limiting) |
| V | tastytrade publishes no limits and sends a bare 429 with no headers. | `ratelimit` conservative default budget | [tastytrade backoff guide](https://developer.tastytrade.com/docs/guides/rate-limits-and-backoff/) |
| V | IBKR pacing: fifty messages a second shared by every client on one gateway; a burst disconnects the client. | `classify_ibkr` PACING kind; budget per venue | [groups.io thread](https://groups.io/g/insync/topic/max_rate_of_messages/6390099) |

## Order state after placing

| scope | what happens | guard | sources |
|---|---|---|---|
| V | IBKR: cancel answered with 10147/10148 ("not found" / "cannot be cancelled, state: Filled"), sometimes even when the cancel went through; an order placed by a dead process is un-cancellable from any other client id and still fills. | `classify_ibkr` CANCEL_RACED_FILL: re-read the order before trusting the cancel; the adapter docs say keep one stable client id | [ib_insync #690](https://github.com/erdewit/ib_insync/issues/690), [ib_async #240](https://github.com/ib-api-reloaded/ib_async/issues/240) |
| V | IBKR 202 "Order Canceled" can be the venue's own price precaution; 201 "rejected" sometimes arrives with no status event. | `classify_ibkr` ORDER_DEAD, treated as terminal and re-read | [pysystemtrade discussion](https://github.com/pst-group/pysystemtrade/discussions/1576), [ib_insync #245](https://github.com/erdewit/ib_insync/issues/245) |
| V | IBKR farm-connection chatter (2104/2106/2158) logs as warnings on every connect; the broken variants (2103/2105) mean the data is stale. | `classify_ibkr` NOISE vs DATA_STALE | [IBKR message codes](https://interactivebrokers.github.io/tws-api/message_codes.html) |
| V | Tradier orders sit `pending` for minutes before a reject, and fills at the venue sometimes never reach the client. | Engine `poll_working` re-reads every non-terminal order; `runtime/fill_audit.py` compares ledger fills to the broker | [WealthLab discussion](https://www.wealth-lab.com/Discussion/11301) |
| V | Alpaca's trade-update stream does not replay events missed during a drop; the stream and REST give the same fill different ids. | Position reconcile every cycle is the source of truth, not the stream | [Alpaca activity SSE](https://docs.alpaca.markets/us/docs/activity-sse) |
| V | tastytrade marks an order `Filled` before all leg fills are attached; a vertical can part-fill and leave a naked short. | `runtime/fill_audit.py`; the spread guard's structure check on reconcile | [tastytrade order lifecycle](https://developer.tastytrade.com/docs/concepts/order-lifecycle/) |

## Data feeds

| scope | what happens | guard | sources |
|---|---|---|---|
| G | A venue's options quote feed froze for days of trading while equities kept ticking; a limit sell sat against a stale book as last sale ran past it. | `runtime/freshness.py` `quote_lag_reason` per order and `FrozenFeedDetector` across symbols | [Alpaca forum: options chain frozen](https://forum.alpaca.markets/t/options-chain-frozen/19462) |
| G | A websocket reported "connected" for over half an hour with no ticks; a signal filled that stale. Nothing measured seconds since the last tick. | `FrozenFeedDetector.feed_frozen_reason`; the existing two-feed spot read | [tradingqna post-mortem](https://tradingqna.com/t/my-live-algo-showed-connected-while-its-data-feed-was-silently-dead-for-33-minutes-post-mortem-what-im-building-so-it-never-happens-silently-again/197554) |
| G | A bar request without an explicit start returned the same window for over an hour; the bot logged a "fresh decision" on a price that never moved. | `calendar.bar_is_current`; `FrozenFeedDetector` | [dev.to](https://dev.to/tatsuyawwp/my-bot-logged-a-fresh-decision-every-5-minutes-for-over-an-hour-the-price-it-was-deciding-on-never-40ch) |
| V | IBKR options quotes need an OPRA subscription on the API user; paper accounts do not inherit it; one connector never sent the delayed-data call and tickers just never ticked. Another venue's paper fills ignore liquidity entirely. | `classify_ibkr` DATA_STALE (10089/354/10167); README says paper fills are an upper bound | [IBKR error resolution](https://www.interactivebrokers.com/docs/tws-api/doc/error-handling/common-error-resolution/), [Elite Trader paper fills](https://www.elitetrader.com/et/threads/ibkr-paper-account-fill-issue-via-api.385898/) |

## Calendar and clock

| scope | what happens | guard | sources |
|---|---|---|---|
| G | A bot ran on an exchange holiday against stale candles; the venue queued its orders and filled them at the next open, bypassing every signal check. | `runtime/calendar.py`: `is_trading_day`, `market_open`, `bar_is_current`; a table that ends halts rather than guesses | [CentralizedAlgoSystem #55](https://github.com/ssamirr123/CentralizedAlgoSystem/pull/55) |
| G | An end-of-day routine keyed on "hour >= 16" in server UTC fired hours early; tests used fixed timestamps so it never surfaced. | `calendar.assert_aware`, every rule in ET via zoneinfo | [dev.to](https://dev.to/botandbull/the-bug-that-passed-every-test-and-still-lost-me-money-1gl5) |
| G | A fixed UTC offset for the broker ran the session window an hour off every summer. | `calendar.ET` zoneinfo, no fixed offsets anywhere | [CX_xauusd_ai_bot_v2 #4](https://github.com/Tejas-040303/CX_xauusd_ai_bot_v2/pull/4) |
| G | Pre-market market orders filled at the post-open spike; extended-hours quotes can be zero or stale. | `calendar.market_open` is the regular session only; the engine never sends a market order | [Alpaca forum](https://forum.alpaca.markets/t/frustration-with-the-execution-of-pre-market-orders/13787) |

## Expiry, assignment and the risk desk

| scope | what happens | guard | sources |
|---|---|---|---|
| O | A covered call closed out of the money, the underlying dipped after hours, the venue assigned it late evening; the bot had booked "expired worthless", kept phantom shares and sold stock it no longer had the next morning. | `runtime/expiry.py` `assignment_window_open` and `expiry_settled_at`: nothing is booked or traded on the underlying until the exercise cutoff has passed; `runtime/book_check.py` flags unexpected equity before the open | [QuantConnect thread](https://www.quantconnect.com/forum/discussion/15605/incorrect-tracking-of-covered-call-assignments-please-help/) |
| O | A short leg was assigned early, the venue marked the resulting equity non-tradable, the bot's close went nowhere, margin calls followed. | `book_check` NON_TRADABLE finding escalates to a human; the reconciler's ASSIGNMENT class | [QuantConnect thread](https://www.quantconnect.com/forum/discussion/6173/unable-to-liquidate-underlying-after-option-assignment/) |
| O | Venues close in-the-money legs themselves from early afternoon on expiry day with no stated cutoff; one venue refuses opening orders on expiring contracts from mid-afternoon. | `expiry.pin_risk_reason` closes a pinned short before the risk desk does; `expiry.no_open_reason` refuses late opens ahead of every venue's own cutoff | [Elite Trader](https://www.elitetrader.com/et/threads/wondering-about-forced-liquidation-of-long-options-just-before-expiration.350496/), [Alpaca options overview](https://docs.alpaca.markets/us/docs/options-trading-overview) |
| O | After-hours exercise cutoffs are venue-specific and undocumented; nobody on a long thread could name one. | `expiry.ASSIGNMENT_WINDOW_ENDS` is set past every reported cutoff | [Elite Trader](https://www.elitetrader.com/et/threads/after-hours-options-assignment-cut-off.357058/) |
| O | A short call on a hard-to-borrow name was assigned overnight and auto-liquidated at the open with a large borrow charge. | `expiry.ex_dividend_assignment_reason` flags the window; hard-to-borrow is a data input the package does not have, so the README names it as a user check | [Elite Trader](https://www.elitetrader.com/et/threads/bynd-short-call-assigned-on-hard-to-borrow-on-interactive-broker.334742/) |
| G | A venue's risk desk liquidates with minutes of warning, sometimes none, on a single bad print. | The caps are the bot's own cushion, set well inside the venue's; `book_check` surfaces positions the bot did not open | [Elite Trader](https://www.elitetrader.com/et/threads/your-experiences-with-being-auto-liquidated-by-ibkrs-strict-risk-management.310659/) |
| O | Adjusted contracts (a digit root, a non-standard deliverable) list beside standard ones on the same chain and price "wrong". | The engine's `guard_not_adjusted`; the reconciler's ADJUSTED class | [investing.com](https://www.investing.com/analysis/reverse-splits-and-unreliable-options-chains-200170735) |

## Process and state

| scope | what happens | guard | sources |
|---|---|---|---|
| G | Two instances of the bot (an old process never stopped, the same bot on a laptop and a server): every trade exactly twice. | `runtime/instance.py` `InstanceLock` | [noonootrading](https://noonootrading.com/blog/en/duplicate-order-idempotency.html) |
| G | A restart rebuilt positions at their original size after partial exits, over-closed and re-fired targets; another restart silently disabled a trailing stop behind a "safe default" of zero. | `close_sizing` from the broker's position; the ledger is the restart source of truth; health rows fail loudly on a missing read | [ai-trading-bot #742](https://github.com/bumpy-croc/ai-trading-bot/issues/742), [dev.to](https://dev.to/xuks124/a-restart-silently-disabled-our-trailing-stop-and-the-crash-fix-is-what-hid-it-cgo) |
| G | A live algorithm "forgot" positions it still held at the venue; sells recorded internally never happened. | The reconciler (OVER/UNDER) every cycle; `book_check` ORPHAN_OPTION | [QuantConnect thread](https://www.quantconnect.com/forum/discussion/18613/algorithm-holdings-not-matching-brokerage-holdings/) |
| G | A dashboard said "placed" for days while the venue's ack count was zero; an exception was swallowed without incrementing the error counter. | Health rows record only venue-confirmed outcomes; a check that cannot run reads `noop`, never ok | [dev.to](https://dev.to/jugeni/60-of-my-921-wasnt-strategy-the-other-40-wasnt-even-visible-4m30) |

## What is not covered

- Hard-to-borrow status and dividend calendars: inputs the package does not fetch. Pass an
  ex-dividend date to `ex_dividend_assignment_reason` from your own data.
- Fidelity: no public trading API exists.
- Venue maintenance windows: one venue publishes a weekly window, the others do not. Treat a
  503 as transient and let the dead-man gate decide whether the bot is still healthy.
- Streaming (websocket) reconnect and replay: the package is REST-first by design; reconcile
  from REST after any stream gap.
