"""Assignment-avoidance policy tests (docs: never-assigned requirement, Sep 10 2026).

The guarantee is deterministic; EV only chooses roll-vs-close inside the safe
envelope. These pin both: the hard triggers always act, and an OTM put near the
cutoff is left to expire worthless (the EV win, and still no assignment)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from agentic_guardrails.assignment_guard import (
    GuardAction,
    GuardConfig,
    PositionSnapshot,
    RollCandidate,
    decide,
)

TODAY = date(2026, 9, 10)
CFG = GuardConfig()


def _short_put(**kw):
    base = dict(
        contract_symbol="O:TNA260918P00060000",
        strike=Decimal("60"),
        expiration=date(2026, 9, 18),
        quantity=-1,
        underlying_spot=Decimal("64"),
        put_delta=Decimal("-0.24"),
        close_ask=Decimal("1.10"),
        extrinsic=Decimal("1.06"),
    )
    base.update(kw)
    return PositionSnapshot(**base)


def _roll(strike="58", exp=date(2026, 10, 16), credit="1.20", delta="-0.20"):
    return RollCandidate("O:TNA261016P000" + str(strike) + "000", Decimal(strike), exp, Decimal(credit), Decimal(delta))


def test_long_position_out_of_scope():
    assert decide(_short_put(quantity=1), [], TODAY, CFG).action == GuardAction.HOLD


def test_safe_otm_far_from_expiry_holds():
    d = decide(_short_put(underlying_spot=Decimal("64")), [], TODAY, CFG)
    assert d.action == GuardAction.HOLD


def test_near_strike_before_cutoff_holds_by_default():
    # Sep 24 2026: an 8-DTE put 1% above its strike is NOT an assignment risk (it
    # can only be assigned at expiry or deep ITM). Closing it is a stop-loss; the
    # live guard did exactly that on NVDX $19.5P for a -$50 loss. Default holds.
    d = decide(_short_put(underlying_spot=Decimal("60.4"), put_delta=Decimal("-0.45")), [_roll()], TODAY, CFG)
    assert d.action == GuardAction.HOLD


def test_shallow_itm_before_cutoff_holds_by_default():
    # ITM but with real extrinsic left: no early-assignment risk yet, wait for the cutoff.
    d = decide(_short_put(underlying_spot=Decimal("59.5"), put_delta=Decimal("-0.55"), extrinsic=Decimal("0.90")), [], TODAY, CFG)
    assert d.action == GuardAction.HOLD


def test_pre_cutoff_breach_trigger_is_opt_in():
    cfg = GuardConfig(pre_cutoff_breach=True)
    d = decide(_short_put(underlying_spot=Decimal("59.5"), put_delta=Decimal("-0.55")), [], TODAY, cfg)
    assert d.action == GuardAction.CLOSE


def test_cutoff_itm_rolls_when_qualifying_candidate_exists():
    d = decide(
        _short_put(expiration=date(2026, 9, 11), underlying_spot=Decimal("59.5"), put_delta=Decimal("-0.55")),
        [_roll(exp=date(2026, 9, 25))],
        TODAY,
        CFG,
    )
    assert d.action == GuardAction.ROLL and d.roll_to is not None and d.roll_to.strike == Decimal("58")


def test_roll_never_stretches_far_past_the_original_expiry():
    # A candidate 5 weeks further out is not a roll of a 7-10 DTE position; go flat instead.
    d = decide(
        _short_put(expiration=date(2026, 9, 11), underlying_spot=Decimal("59.5"), put_delta=Decimal("-0.55")),
        [_roll(exp=date(2026, 10, 16))],
        TODAY,
        CFG,
    )
    assert d.action == GuardAction.CLOSE


def test_roll_ignores_a_zero_bid_candidate():
    d = decide(
        _short_put(expiration=date(2026, 9, 11), underlying_spot=Decimal("59.5"), put_delta=Decimal("-0.55"), close_ask=Decimal("0.10")),
        [_roll(exp=date(2026, 9, 25), credit="0")],
        TODAY,
        CFG,
    )
    assert d.action == GuardAction.CLOSE


def test_deep_itm_zero_extrinsic_triggers_early_regardless_of_dte():
    # 8 DTE (before cutoff) but deep ITM with tiny extrinsic = early-assignment risk.
    d = decide(_short_put(underlying_spot=Decimal("50"), extrinsic=Decimal("0.10"), put_delta=Decimal("-0.95")), [], TODAY, CFG)
    assert d.action in (GuardAction.CLOSE, GuardAction.ROLL)


def test_cutoff_comfortably_otm_lets_expire():
    d = decide(_short_put(expiration=date(2026, 9, 11), underlying_spot=Decimal("64"), put_delta=Decimal("-0.05")), [], TODAY, CFG)
    assert d.action == GuardAction.LET_EXPIRE


def test_cutoff_not_otm_must_act():
    d = decide(_short_put(expiration=date(2026, 9, 11), underlying_spot=Decimal("60.5"), put_delta=Decimal("-0.45")), [], TODAY, CFG)
    assert d.action in (GuardAction.CLOSE, GuardAction.ROLL)


def test_cutoff_spot_unreadable_fails_safe_to_act():
    d = decide(_short_put(expiration=date(2026, 9, 11), underlying_spot=None), [], TODAY, CFG)
    assert d.action in (GuardAction.CLOSE, GuardAction.ROLL)


def test_roll_into_large_debit_is_rejected_for_close():
    # A candidate that collects almost nothing while buy-to-close is expensive:
    # rolling would pay up to stay short, so the policy closes instead.
    d = decide(
        _short_put(expiration=date(2026, 9, 11), underlying_spot=Decimal("59.5"), put_delta=Decimal("-0.55"), close_ask=Decimal("3.00")),
        [_roll(exp=date(2026, 9, 25), credit="0.10")],
        TODAY,
        CFG,
    )
    assert d.action == GuardAction.CLOSE


def test_cutoff_reason_names_the_delta_leg_when_price_cushion_passes():
    # 6% OTM (price cushion OK) but |delta| 0.20 > 0.10: the reason must say DELTA, not "not comfortably OTM".
    d = decide(_short_put(expiration=date(2026, 9, 12), underlying_spot=Decimal("63.6"), put_delta=Decimal("-0.20")), [], TODAY, CFG)
    assert d.action == GuardAction.CLOSE
    assert "delta" in d.reason and "price cushion OK" in d.reason


def test_cutoff_reason_names_the_price_leg_when_spot_is_near_strike():
    d = decide(_short_put(expiration=date(2026, 9, 12), underlying_spot=Decimal("60.5"), put_delta=Decimal("-0.05")), [], TODAY, CFG)
    assert d.action == GuardAction.CLOSE
    assert "price cushion" in d.reason and "|delta|" not in d.reason
