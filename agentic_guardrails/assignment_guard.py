"""Assignment-avoidance policy for live short puts (Dan, Sep 10 2026).

HARD REQUIREMENT: a live short option must NEVER be assigned. Dan accepts taking
a loss to close/roll rather than ever managing an assigned equity position.

The guarantee is DETERMINISTIC, never an EV bet: an EV model can be wrong, and
"usually flat" is not "never assigned". So this module is layered:

  1. A hard backstop that guarantees we are flat-or-safe before assignment can
     occur — the two ways a short put is assigned:
       (a) EXPIRATION: any ITM short put at expiry is assigned. So by a cutoff
           DTE we must be safe: either close/roll, OR (the EV win) let it expire
           worthless when it is comfortably OTM — an OTM put assigns nobody and
           costs nothing to leave alone.
       (b) EARLY: an American put can be assigned early once it is deep ITM and
           its extrinsic value ~ 0 (nothing left to gain by waiting). So if that
           state is reached at ANY time, act now.
  2. Within that safe envelope, EV decides the CHOICE: when we must act, roll
     (keep premium working) vs close (go flat); and we never pay to close a put
     that will safely expire worthless.

This module is PURE and stdlib-only (Decimal/date/dataclasses), like the rest of
the executor core, so the policy is unit-testable with no broker, DB, or clock.
The runtime loop supplies the snapshot and executes the decision; it does NOT
decide policy here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum


class GuardAction(str, Enum):
    HOLD = "hold"  # safe, do nothing this tick
    LET_EXPIRE = "let_expire"  # comfortably OTM near cutoff: allow worthless expiry (no cost, no assignment)
    CLOSE = "close"  # buy-to-close now (go flat)
    ROLL = "roll"  # buy-to-close + sell the chosen further/lower candidate


@dataclass(frozen=True)
class RollCandidate:
    """A put we could roll INTO (further-dated and at/below the current strike, so
    it reduces — never increases — assignment risk). credit_bid is what selling it
    would collect; delta is its (negative) put delta as a P(ITM) proxy."""

    contract_symbol: str
    strike: Decimal
    expiration: date
    credit_bid: Decimal
    delta: Decimal


@dataclass(frozen=True)
class PositionSnapshot:
    """Everything the policy needs about one open short put, as last read. A
    non-short or non-put position is out of scope (returns HOLD). Any field the
    loop could not read is None, and the policy FAILS SAFE on a missing field:
    when it cannot prove the position is safe, it acts rather than holds."""

    contract_symbol: str
    strike: Decimal
    expiration: date
    quantity: int  # signed; a short put is negative
    underlying_spot: Decimal | None
    put_delta: Decimal | None  # negative for a put; |delta| ~ P(finish ITM)
    close_ask: Decimal | None  # buy-to-close cost per share (the short put's ask)
    extrinsic: Decimal | None  # mark - intrinsic, per share; ~0 deep ITM = early-assignment risk


@dataclass(frozen=True)
class GuardConfig:
    # The deterministic backstop: by this many days to expiry we must be safe.
    hard_close_dte: int = 2
    # Near the cutoff, "comfortably OTM" = spot at least this fraction above the
    # strike AND |delta| below otm_delta_max: then we let it expire worthless.
    safe_otm_pct: Decimal = Decimal("0.03")
    otm_delta_max: Decimal = Decimal("0.10")
    # ITM-breach trigger before the cutoff: act once spot is within this fraction
    # ABOVE the strike (0 = only once actually at/through the strike; a small
    # positive value acts a touch early for buffer).
    itm_buffer_pct: Decimal = Decimal("0.01")
    # Whether that near-strike trigger fires BEFORE the cutoff at all. OFF by
    # default (Sep 24 2026): a short put can only be assigned at expiry or when
    # deep ITM with no extrinsic left, and both are covered above/below. Acting
    # on a 1%-from-strike touch with a week to go is a STOP-LOSS, not assignment
    # avoidance; live it bought back dip200_7dte's NVDX $19.5P at 0.80 (credit
    # 0.30) on a still-OTM, 8-DTE put. Stops on short premium are a measured
    # closed vein here (copy-book spot-stop, spread stop study): the recovering
    # majority costs more than the tail saves. The cutoff still forces action.
    pre_cutoff_breach: bool = False
    # ITM inside this many days to expiry: act now (Oct 13 2026, Dan). A put that
    # is actually IN the money this close to expiry is an assignment risk, not a
    # stop-loss decision; waiting for the DTE-2 cutoff left an ITM NVDL $37P
    # (delta -0.67) on "hold" at 3 DTE. OTM puts are unaffected.
    itm_act_dte: int = 5
    # A roll must not quietly stretch the position far past its original life:
    # only candidates expiring within this many days after the current expiry
    # qualify. Picking the max-credit candidate across the whole chain chose a
    # contract 5 weeks further out (Oct 2 -> Nov 6) on a 7-10 DTE arm.
    max_roll_extension_days: int = 14
    # Early-assignment trigger: when ITM and extrinsic has decayed to at most this
    # fraction of intrinsic (or is essentially zero), act now regardless of DTE.
    deep_itm_extrinsic_frac: Decimal = Decimal("0.05")
    # Roll only if it is ~cash-neutral-or-credit: the new premium must recover the
    # buy-to-close cost to within this per-share debit. A roll that pays UP to stay
    # short (collects little premium for a fresh obligation) is not worth it vs
    # simply going flat, so we close instead. This is what stops "cash always
    # favors rolling" from rolling into a bad new short.
    max_roll_debit: Decimal = Decimal("0.20")


@dataclass(frozen=True)
class GuardDecision:
    action: GuardAction
    reason: str
    roll_to: RollCandidate | None = None


def _dte(expiration: date, today: date) -> int:
    return (expiration - today).days


def is_short_put(pos: PositionSnapshot) -> bool:
    return pos.quantity < 0


def choose_roll_or_close(pos: PositionSnapshot, candidates: list[RollCandidate], cfg: GuardConfig = GuardConfig()) -> GuardDecision:
    """The EV determination, within the safe envelope: roll if a qualifying
    candidate exists whose net cost is no worse than closing; else close.

    A qualifying roll is further-dated AND struck at or below the current strike
    (so assignment risk strictly decreases) AND lower delta than the current
    position's (a safer P(ITM)). Among those, prefer the one whose NET (roll
    credit minus buy-to-close cost) is greatest — least debit / most credit.
    Rolling is chosen only when that net is not materially worse than the pure
    close cost, i.e. rolling does not pay UP to stay short. Documented heuristic,
    not a false-precise EV engine; a richer model can replace this body without
    touching the deterministic backstop below."""
    if pos.close_ask is None:
        # Cannot price the buy-to-close: fail safe to CLOSE (the loop will still
        # need a price to execute, but the policy's intent is unambiguous).
        return GuardDecision(GuardAction.CLOSE, "close: buy-to-close price unreadable, failing safe to close")
    cur_delta = abs(pos.put_delta) if pos.put_delta is not None else None
    qualifying = [
        c
        for c in candidates
        if c.expiration > pos.expiration
        and (c.expiration - pos.expiration).days <= cfg.max_roll_extension_days
        and c.strike <= pos.strike
        and c.credit_bid > 0
        and (cur_delta is None or abs(c.delta) <= cur_delta)
    ]
    if not qualifying:
        return GuardDecision(GuardAction.CLOSE, f"close: no qualifying roll (lower/further, safer delta); buy-to-close @ {pos.close_ask}")
    # Net cash of the roll = credit collected on the new short minus the cost to
    # buy back the current one. Higher is better (a credit roll is ideal).
    best = max(qualifying, key=lambda c: c.credit_bid - pos.close_ask)
    net = best.credit_bid - pos.close_ask  # roll cash: new premium minus buy-to-close
    # Rolling keeps us SHORT a fresh obligation, so cash alone (which always
    # favors rolling when credit_bid > 0) is not the test. Roll only when the new
    # premium recovers the buyback to within max_roll_debit; otherwise the roll
    # collects too little to justify a new short and we simply close.
    if net < -cfg.max_roll_debit:
        return GuardDecision(GuardAction.CLOSE, f"close: best roll net {net} beyond -{cfg.max_roll_debit} debit tolerance; going flat")
    return GuardDecision(
        GuardAction.ROLL, f"roll: {best.contract_symbol} net {net} (credit {best.credit_bid} - close {pos.close_ask})", roll_to=best
    )


def decide(pos: PositionSnapshot, candidates: list[RollCandidate], today: date, cfg: GuardConfig = GuardConfig()) -> GuardDecision:
    """The layered policy. Returns the action for THIS tick. Deterministic on the
    safety guarantee; EV only chooses roll-vs-close when action is required."""
    if not is_short_put(pos):
        return GuardDecision(GuardAction.HOLD, "hold: not a short put, out of scope")

    dte = _dte(pos.expiration, today)
    spot = pos.underlying_spot
    strike = pos.strike

    # Early-assignment trigger (any DTE): deep ITM with extrinsic ~ 0.
    if spot is not None and spot < strike:
        intrinsic = strike - spot
        if pos.extrinsic is not None and intrinsic > 0 and pos.extrinsic <= cfg.deep_itm_extrinsic_frac * intrinsic:
            return _act(
                pos,
                candidates,
                f"deep-ITM early-assignment risk (spot {spot} < strike {strike}, extrinsic {pos.extrinsic} <= {cfg.deep_itm_extrinsic_frac} of intrinsic {intrinsic})",
                cfg,
            )

    # Hard backstop: by the cutoff DTE we must be provably safe.
    if dte <= cfg.hard_close_dte:
        if spot is None:
            return _act(pos, candidates, f"cutoff reached (dte {dte}) and spot unreadable: failing safe to act", cfg)
        comfortably_otm = spot >= strike * (Decimal(1) + cfg.safe_otm_pct)
        low_delta = pos.put_delta is None or abs(pos.put_delta) <= cfg.otm_delta_max
        if comfortably_otm and low_delta:
            return GuardDecision(
                GuardAction.LET_EXPIRE,
                f"let expire: dte {dte}, spot {spot} >= strike {strike} x (1+{cfg.safe_otm_pct}), delta safe — worthless expiry, no assignment",
            )
        # Name the leg that actually failed (Sep 16 2026): on a high-IV wrapper
        # a 6%-OTM put can carry |delta| 0.20, so the PRICE cushion passes and
        # only the delta gate forces action; the old "not comfortably OTM" text
        # misread that as a price failure. Both legs are deliberately required
        # for LET_EXPIRE: |delta| is ~P(finish ITM), and 20% with two days left
        # is not "never assigned", so acting there is the guarantee, not a bug.
        if not comfortably_otm:
            why = f"spot {spot} < strike {strike} x (1+{cfg.safe_otm_pct}) price cushion"
        else:
            why = f"price cushion OK (spot {spot} vs strike {strike}) but |delta| {abs(pos.put_delta)} > {cfg.otm_delta_max} = P(ITM) too high"
        return _act(pos, candidates, f"cutoff reached (dte {dte}), not safe to let expire: {why}; must be flat before expiry", cfg)

    # In the money close to expiry: act now, do not wait for the cutoff.
    if spot is not None and spot < strike and dte <= cfg.itm_act_dte:
        return _act(pos, candidates, f"in the money near expiry (dte {dte} <= {cfg.itm_act_dte}, spot {spot} < strike {strike})", cfg)

    # ITM-breach trigger before the cutoff.
    if cfg.pre_cutoff_breach and spot is not None and spot <= strike * (Decimal(1) + cfg.itm_buffer_pct):
        return _act(pos, candidates, f"ITM/near-strike breach (spot {spot} <= strike {strike} x (1+{cfg.itm_buffer_pct}))", cfg)

    return GuardDecision(GuardAction.HOLD, f"hold: safe (dte {dte}, spot {spot}, strike {strike})")


def _act(pos: PositionSnapshot, candidates: list[RollCandidate], why: str, cfg: GuardConfig = GuardConfig()) -> GuardDecision:
    d = choose_roll_or_close(pos, candidates, cfg)
    return GuardDecision(d.action, f"{why} -> {d.reason}", roll_to=d.roll_to)
