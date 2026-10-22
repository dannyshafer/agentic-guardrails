# The nine incidents

Every control in this package is here because something went wrong with real money first. These
are the nine, in the order they happened, each with the hole and the control that closed it. No
dollar figures: they were all small, which is the point, and the controls are what kept them small.

Each one carries a tag for who is likely to hit it:

- **any agent that moves money**: not specific to options or to any broker;
- **any short-premium book**: specific to selling options, on any venue;
- **Robinhood's agent endpoint**: specific to that venue's API, and exactly what you hit if you
  build on it.

## 1. Eight controls that did nothing (any agent that moves money)

One review session found eight defects. All eight compiled, passed lint, and passed their own
unit tests. The dead-man gate refused to arm without a fresh self-test, and nothing ever ran the
self-test, so all it could do was halt. Two new guards were wired into the guard list, but the
engine never filled in their inputs, so they took their defaults and let every order through.
Their unit tests stayed green the whole time. The earnings guard asked the data provider about a
leveraged ETF, which has no earnings, instead of the company it tracks. It got None back, read
that as "no event coming", and would have sold a put straight through the company's report. That
is the one name it exists to protect. The reconcile recorded a pass on a position mismatch because
the code called it "safe to heal" and nothing healed it.

What I learned is in `checklist.md`. Every guard test in this package now runs at the engine
level, the fake earnings provider checks which symbol it was asked about, and every health
writer's cadence is pinned against the window its reader expects.

## 2. The fill the ledger never saw (Robinhood's agent endpoint, but the reconcile lesson is general)

The first live order filled. The ledger did not record it. The broker returns a placed order as
one object, `{"data": {"order": {...}}}`, and my parser only unwrapped lists. It handed back the
wrapper, found no id and no state in it, and left the row sitting at "reviewed" with no broker
order id while the broker was holding a filled short put.

Now `broker._records` treats a dict-valued key as a single record. There is an UNKNOWN order
state that means exactly "I sent it and could not read the answer". And the reconciler reads the
broker's positions, not its order list, so a fill that never made it into the ledger shows up as
OVER drift and blocks the next open instead of sitting there as an invisible short.

## 3. The protective close that locked itself out (any agent that moves money)

The assignment guard built its buy-to-close with a client order id that was fixed for the
calendar day. The cutoff fired just after midnight UTC, which is evening in New York, after
hours, thin book. The close did not fill inside its poll window and got cancelled with zero
filled. Every tick after that produced the same id, hit the engine's duplicate check ("already
tracked in state canceled"), and gave up. So the guard whose whole job is to make sure a short
never gets assigned did nothing for the rest of the day, including through a full session where
the close would have filled easily.

Now `runtime.guard_loop.next_close_coid` walks a sequence of attempt ids, mints a fresh one after
a terminal non-fill, skips while one is still working, and raises a hand for a human after a
bounded number of cancels. And the loop waits for market hours before it tries, because a
marketable close after the bell is not going to fill.

## 4. The stop-loss I did not mean to write (any short-premium book)

A rule said: if spot gets within 1% of the strike, close the short. It bought back two
out-of-the-money puts with about a week to run, each for well over the credit I had collected.
Then both rolls failed, one because the guard's engine had no earnings provider and the roll leg
failed closed, one on a per-day cap. Pure stop-outs. A put gets assigned at expiry or when it is
deep in the money, not when spot wanders near the strike with a week left. And a stop-loss on
short premium is a rule I had already measured and found to lose money.

So `GuardConfig.pre_cutoff_breach` is now False by default: the guard acts at two days to expiry
or when there is no extrinsic value left. Rolls are bounded. The roll leg's earnings check runs
before the buy-back, not after. And `runtime.vetting.open_sanity_reason` floors any new open on
credit, yield, days to expiry, no same-day re-entry after a guard close, and no stacking onto a
short that is already near its strike.

## 5. The take-profit that was really a poll (any short-premium book)

My first close-at-half-the-credit rule only bought if a periodic tick happened to catch the ask
below target. Now the take-profit is a resting GTC order keyed on the position's expiry.
`OrderPlan` refuses GTC on an opening order. The engine's one-in-flight check ignores another
strategy's resting close only when the plan is itself a resting close. And the assignment guard
cancels resting closes first, re-reads the broker position, and only then places its own,
because otherwise both can fill and you end up long a put you never wanted.

## 6. Caps twice the size I thought (any agent that moves money)

The broker's cash figure already includes the collateral tied up in open cash-secured puts. Only
buying power subtracts it. I was computing equity as cash plus open collateral, so every
proportional cap was about double the real account, and the only thing actually stopping me was
the venue's "not enough buying power" rejections.

Now `runtime.vetting` and `ProportionalCaps.caps_for` treat cash alone as equity, and if the
account read fails they fall back to the fixed caps, never to a flattering guess.

## 7. Two orders, one tick, cap overshot (any agent that moves money)

Two orders went out seconds apart in the same tick. The second was evaluated before the first had
settled to filled in the ledger, so the per-factor cap read low and the factor ended up over its
cap by about one order. Cash still bounded it, so no loss, but a cap you can overshoot is not a
cap.

Now `ledger.inflight_open_remainders` counts the unfilled remainder of every live opening order
in every cap scope inside `Engine._context`, and the test for it runs at the engine level: an
unsettled open blocks the next one at the cap.

## 8. The self-test that only ran after the close, and the canary that expired (any agent that moves money)

The dead-man gate wants a self-test less than 20 hours old. The self-test only ran after market
close. So a morning restart ran the open loop against a gate about to go stale. Then the canary
contract the self-test quotes, which I had hardcoded with a date, expired. The broker said "no
instrument", every self-test failed, and the gate was about to halt every live open for a reason
that had nothing to do with the broker. Halting is the safe direction, so this one is a false
alarm story, not a loss story.

Now `health.should_run` also fires at startup, and `brokers.robinhood.rolled_canary` keeps the
root, type and strike but rolls the expiry to the standard monthly two months out. Never pin a
health check to a dated contract.

## 9. The id the venue stopped accepting (Robinhood's agent endpoint, but the boundary lesson is general)

The broker started rejecting my client order id as the `ref_id` ("must be a UUID"). Every open
got rejected. Worse, the assignment guard's protective close goes through the same place path, so
if a short had gone against me there was no way to buy it back. Nothing caught it for days,
because it showed up as repeated reject messages, not as a health row.

Now `ids.broker_ref_id` derives a UUID5 from the internal id right at the wire, so the internal
idempotency key never has to change when a venue changes its format. And the fill audit and the
controls drill exist so that "the place path is dead" is a health row, not something you infer
from a pile of alerts.

## The pattern

All nine are the same thing: a control is only real when it is wired to its real inputs, asked
the right question, and run often enough for whatever reads it. That is `checklist.md`.
