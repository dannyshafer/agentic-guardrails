"""Guards built from field-reported venue failures (docs/venue_failure_modes.md)."""

from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agentic_guardrails.broker import BrokerAuthExpired, BrokerRejected, BrokerTransient
from agentic_guardrails.brokers._common import sub_display_min_reason, tick_for
from agentic_guardrails.brokers.rest import BrokerThrottled, RestResponse, raise_for
from agentic_guardrails.runtime import adopt, auth_lease, book_check, calendar, close_sizing, expiry, freshness, instance, ratelimit
from agentic_guardrails.venue_errors import IbkrErrorKind, Policy, RejectClass, classify_ibkr, classify_reject

UTC = timezone.utc


def et(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=calendar.ET)


# ---- venue_errors -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,cls,policy",
    [
        ("insufficient options buying power (required: 5000, available: 100)", RejectClass.BUYING_POWER, Policy.STAND_DOWN_TICK),
        ("You do not have enough buying power to open this position", RejectClass.BUYING_POWER, Policy.STAND_DOWN_TICK),
        ("potential wash trade detected. use complex orders", RejectClass.WASH_TRADE, Policy.CANCEL_RESTING_FIRST),
        ("WashTradeAttempt: same security and same price in different directions", RejectClass.WASH_TRADE, Policy.CANCEL_RESTING_FIRST),
        ("trade denied due to pattern day trading protection", RejectClass.PDT, Policy.HALT_OPENS),
        ("Order rejected by Schwab. Day Trade Equity Restricted, no opening trades allowed", RejectClass.PDT, Policy.HALT_OPENS),
        ("Account is set to closing only and this order has an opening leg.", RejectClass.CLOSING_ONLY, Policy.HALT_OPENS),
        ("Your account is not approved for selling uncovered options", RejectClass.OPTION_LEVEL, Policy.HALT_OPENS),
        ("account not eligible for level2 options trading", RejectClass.OPTION_LEVEL, Policy.HALT_OPENS),
        (
            "You already have a closing order for XYZ which must be canceled before this order can be routed",
            RejectClass.DUPLICATE_CLOSE,
            Policy.CANCEL_RESTING_FIRST,
        ),
        ("insufficient qty available for order (requested: 1, available: 0)", RejectClass.QTY_HELD, Policy.STAND_DOWN_TICK),
        ("position intent mismatch, inferred: sell_to_close, specified: sell_to_open", RejectClass.POSITION_INTENT, Policy.STAND_DOWN_TICK),
        ("Quota Violation", RejectClass.RATE_LIMIT, Policy.RETRY_LATER),
        ("order manager not ready, please retry", RejectClass.VENUE_WARMUP, Policy.RETRY_LATER),
        ("Option order price rounded to nearest 0.05 (not a penny pilot)", RejectClass.TICK_SIZE, Policy.FIX_ORDER),
        ("mleg uncovered short contracts not allowed, please use single leg order", RejectClass.STRUCTURE_UNSUPPORTED, Policy.FIX_ORDER),
        ("contract expires soon, unable to open new positions", RejectClass.TOO_CLOSE_TO_EXPIRY, Policy.STAND_DOWN_TICK),
        (
            "Sell to open orders cannot be sent 30 minutes prior to market close on day of expiration",
            RejectClass.TOO_CLOSE_TO_EXPIRY,
            Policy.STAND_DOWN_TICK,
        ),
        ("options market orders are only allowed during market hours", RejectClass.MARKET_CLOSED, Policy.STAND_DOWN_TICK),
        ("put sell limit price exceeds strike price", RejectClass.LIMIT_VS_STRIKE, Policy.FIX_ORDER),
        ("Symbol does not exist", RejectClass.SYMBOL_UNKNOWN, Policy.FIX_ORDER),
        ("limit price is significantly away from the current market price", RejectClass.PRICE_AWAY, Policy.FIX_ORDER),
        ("No trades are currently allowed.", RejectClass.SYMBOL_RESTRICTED, Policy.HALT_OPENS),
        ("", RejectClass.UNKNOWN, Policy.UNKNOWN),
        (None, RejectClass.UNKNOWN, Policy.UNKNOWN),
    ],
)
def test_classify_reject(text, cls, policy):
    r = classify_reject(text)
    assert (r.cls, r.policy) == (cls, policy)


def test_sticky_only_for_account_level_blocks():
    assert classify_reject("pattern day trading protection").sticky
    assert not classify_reject("insufficient buying power").sticky


def test_digits_in_a_reject_do_not_read_as_a_rate_limit():
    assert classify_reject("insufficient buying power for order at 429.50").cls is RejectClass.BUYING_POWER


def test_pdt_wins_over_buying_power_wording():
    # "day trading buying power" must read as PDT, not a transient cash shortfall.
    assert classify_reject("not enough day trading buying power").cls is RejectClass.PDT


@pytest.mark.parametrize(
    "code,kind",
    [
        (2104, IbkrErrorKind.NOISE),
        (2106, IbkrErrorKind.NOISE),
        (2103, IbkrErrorKind.DATA_STALE),
        (10197, IbkrErrorKind.DATA_STALE),
        (100, IbkrErrorKind.PACING),
        (10147, IbkrErrorKind.CANCEL_RACED_FILL),
        (10148, IbkrErrorKind.CANCEL_RACED_FILL),
        (201, IbkrErrorKind.ORDER_DEAD),
        (202, IbkrErrorKind.ORDER_DEAD),
        (103, IbkrErrorKind.ORDER_DEAD),
        (1300, IbkrErrorKind.SESSION),
        (None, IbkrErrorKind.OTHER),
        (9999, IbkrErrorKind.OTHER),
    ],
)
def test_classify_ibkr(code, kind):
    assert classify_ibkr(code) is kind


def test_ibkr_session_by_text():
    assert classify_ibkr(5000, "authenticated: false") is IbkrErrorKind.SESSION


# ---- rest.py routing -----------------------------------------------------------------------


def test_403_with_a_known_reject_text_is_a_reject_not_auth():
    with pytest.raises(BrokerRejected):
        raise_for(RestResponse(403, {"code": 40310000, "message": "insufficient options buying power"}), "place", placing=True)


def test_403_with_a_non_order_class_stays_auth():
    # "restricted" matches SYMBOL_RESTRICTED, but an API-key restriction is an auth problem.
    with pytest.raises(BrokerAuthExpired):
        raise_for(RestResponse(403, {"message": "API key restricted"}), "place", placing=True)


def test_403_without_reject_text_stays_auth():
    with pytest.raises(BrokerAuthExpired):
        raise_for(RestResponse(403, "forbidden"), "place", placing=True)
    with pytest.raises(BrokerAuthExpired):
        raise_for(RestResponse(403, {"message": "insufficient options buying power"}), "positions")


def test_quota_violation_400_is_transient_with_retry_hint():
    with pytest.raises(BrokerTransient, match="retry after 9s") as ei:
        raise_for(RestResponse(400, "Quota Violation", {"Retry-After": "9"}), "place", placing=True)
    assert isinstance(ei.value, BrokerThrottled) and ei.value.retry_after == 9.0


def test_5xx_on_a_place_is_ambiguous_not_throttled():
    with pytest.raises(BrokerTransient, match="may have landed") as ei:
        raise_for(RestResponse(503, "Service Unavailable", {"Retry-After": "9"}), "place", placing=True)
    assert not isinstance(ei.value, BrokerThrottled)
    with pytest.raises(BrokerThrottled):  # a 5xx on a READ rested nothing: retry hint allowed
        raise_for(RestResponse(503, "Service Unavailable", {"Retry-After": "9"}), "positions")


def test_service_unavailable_text_is_not_a_retryable_reject():
    assert classify_reject("Service Unavailable").cls is RejectClass.UNKNOWN


def test_other_400_on_place_is_still_a_reject():
    with pytest.raises(BrokerRejected):
        raise_for(RestResponse(400, {"error": "bad"}), "place", placing=True)


# ---- ratelimit -----------------------------------------------------------------------------


def test_token_bucket_blocks_past_budget_and_refills():
    b = ratelimit.TokenBucket(per_minute=60)
    waits = [b.take(1.0) for _ in range(61)]
    assert waits[:60] == [0.0] * 60
    assert waits[60] == pytest.approx(1.0)
    assert b.take(1.0 + 3.0) == 0.0  # three seconds refills the one owed plus two more


def test_token_bucket_hands_pending_callers_distinct_slots():
    # Ten callers hitting an empty bucket must NOT all be told "one second": each reserves the next slot.
    b = ratelimit.TokenBucket(per_minute=60)
    for _ in range(60):
        b.take(1.0)
    waits = [b.take(1.0) for _ in range(10)]
    assert waits == pytest.approx([float(i) for i in range(1, 11)])


def test_retry_after_parsing():
    assert ratelimit.retry_after_seconds({"Retry-After": "7"}) == 7.0
    assert ratelimit.retry_after_seconds({"retry-after": "junk"}) == 60.0
    assert ratelimit.retry_after_seconds({}) == 60.0
    assert ratelimit.retry_after_seconds({"Retry-After": "nan"}) == 60.0
    assert ratelimit.retry_after_seconds({"Retry-After": "inf"}) == 60.0
    assert ratelimit.retry_after_seconds({"Retry-After": "-5"}) == 60.0
    assert ratelimit.retry_after_seconds({"Retry-After": "nan", "X-Ratelimit-Reset-After": "4"}) == 4.0
    assert ratelimit.retry_after_seconds({"Retry-After": "600"}) == 600.0  # the venue's own ask is never shortened


def test_backoff_is_bounded():
    for a in range(1, 12):
        assert 0 < ratelimit.backoff_seconds(a) <= 60 * 1.25


# ---- calendar ------------------------------------------------------------------------------


def test_holidays_and_weekends():
    assert not calendar.is_trading_day(date(2026, 11, 26))  # Thanksgiving
    assert not calendar.is_trading_day(date(2026, 10, 24))  # Saturday
    assert calendar.is_trading_day(date(2026, 10, 22))


def test_market_open_uses_et_not_utc():
    # 15:30 UTC on a trading day is 11:30 ET: open. A bot keyed on UTC "hour >= 16" is wrong here.
    assert calendar.market_open(datetime(2026, 10, 22, 15, 30, tzinfo=UTC))
    assert not calendar.market_open(datetime(2026, 10, 22, 20, 30, tzinfo=UTC))  # 16:30 ET


def test_early_close_day():
    assert calendar.market_open(et(2026, 11, 27, 12, 59))
    assert not calendar.market_open(et(2026, 11, 27, 13, 1))


def test_naive_datetime_refused():
    with pytest.raises(ValueError):
        calendar.market_open(datetime(2026, 10, 22, 12, 0))


def test_calendar_table_end_halts():
    with pytest.raises(ValueError):
        calendar.is_trading_day(date(2028, 1, 3))


def test_next_session_open_skips_holiday_weekend():
    nxt = calendar.next_session_open(et(2026, 11, 25, 17, 0))  # Wed before Thanksgiving
    assert nxt == et(2026, 11, 27, 9, 30)


def test_bar_is_current():
    now = et(2026, 10, 22, 10, 0)
    assert calendar.bar_is_current(et(2026, 10, 22, 9, 57), now)
    assert calendar.bar_is_current(et(2026, 10, 22, 9, 50), now)  # a completed 5-min bar stamped at its open
    assert not calendar.bar_is_current(et(2026, 10, 22, 9, 35), now)  # today's date but frozen since the open
    assert calendar.bar_is_current(et(2026, 10, 22, 9, 35), now, period=timedelta(minutes=15))  # fine for 15-min bars
    assert not calendar.bar_is_current(et(2026, 10, 22, 9, 50), now, max_age=timedelta(minutes=5))  # explicit bound wins
    assert not calendar.bar_is_current(et(2026, 10, 22, 10, 1), now)  # stamped in the future
    assert not calendar.bar_is_current(et(2026, 10, 21, 16, 0), now)
    pre = et(2026, 10, 22, 8, 0)
    assert calendar.bar_is_current(et(2026, 10, 21, 15, 59), pre)  # before the open the last session's bar is right


# ---- freshness -----------------------------------------------------------------------------


def test_quote_lag_reason():
    now = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)
    assert freshness.quote_lag_reason(now - timedelta(seconds=5), now) is None
    assert "old" in freshness.quote_lag_reason(now - timedelta(seconds=45), now)
    assert "future" in freshness.quote_lag_reason(now + timedelta(seconds=45), now)
    assert "no timestamp" in freshness.quote_lag_reason(None, now)
    assert "naive" in freshness.quote_lag_reason(datetime(2026, 10, 22, 15, 0), now)


def test_frozen_feed_detector_distinguishes_quiet_contract_from_dead_feed():
    d = freshness.FrozenFeedDetector(window=timedelta(minutes=5))
    t0 = et(2026, 10, 22, 11, 0)
    for sym in ("A", "B", "C"):
        d.observe(sym, Decimal("1.00"), Decimal("1.10"), t0)
    t1 = t0 + timedelta(minutes=6)
    d.observe("A", Decimal("1.05"), Decimal("1.15"), t1)  # A moved
    d.observe("B", Decimal("1.00"), Decimal("1.10"), t1)
    d.observe("C", Decimal("1.00"), Decimal("1.10"), t1)
    assert d.frozen_symbols(t1) == ["B", "C"]
    assert d.feed_frozen_reason(t1) is None  # something moved: the market, not the feed
    t2 = t1 + timedelta(minutes=6)
    for sym in ("A", "B", "C"):
        d.observe(sym, *(d._last[sym].bid, d._last[sym].ask), t2)
    assert d.feed_frozen_reason(t2) is not None


def test_frozen_feed_needs_enough_symbols():
    d = freshness.FrozenFeedDetector(window=timedelta(minutes=1))
    t0 = et(2026, 10, 22, 11, 0)
    d.observe("A", Decimal(1), Decimal(2), t0)
    assert d.feed_frozen_reason(t0 + timedelta(minutes=5)) is None


def test_frozen_feed_is_quiet_outside_market_hours_and_for_unobserved_symbols():
    d = freshness.FrozenFeedDetector(window=timedelta(minutes=5))
    t0 = et(2026, 10, 22, 11, 0)
    for sym in ("A", "B", "C"):
        d.observe(sym, Decimal(1), Decimal(2), t0)
    # Same quotes re-seen 6 min later, in session: frozen.
    t1 = t0 + timedelta(minutes=6)
    for sym in ("A", "B", "C"):
        d.observe(sym, Decimal(1), Decimal(2), t1)
    assert d.feed_frozen_reason(t1) is not None
    # After the close nothing moves and that is not a frozen feed.
    assert d.feed_frozen_reason(et(2026, 10, 22, 17, 0)) is None
    assert not d.frozen("A", et(2026, 10, 22, 17, 0))
    # A symbol not observed within the window (an expired contract) is unknown, not frozen.
    assert not d.frozen("A", t1 + timedelta(minutes=10))
    d.forget("A")
    assert "A" not in d.frozen_symbols(t1)


# ---- expiry ---------------------------------------------------------------------------------


def test_no_open_after_cutoff_on_expiry_day():
    today = date(2026, 10, 22)
    assert expiry.no_open_reason(today, et(2026, 10, 22, 14, 59)) is None
    assert expiry.no_open_reason(today, et(2026, 10, 22, 15, 0)) is not None
    assert expiry.no_open_reason(date(2026, 10, 21), et(2026, 10, 22, 10, 0)) == "contract has expired"
    assert expiry.no_open_reason(date(2026, 10, 23), et(2026, 10, 22, 15, 30)) is None


def test_pin_risk_only_in_band_after_cutoff_on_expiry_day():
    today = date(2026, 10, 22)
    k = Decimal(100)
    assert expiry.pin_risk_reason(today, k, "P", Decimal("101"), et(2026, 10, 22, 12, 59)) is None
    assert expiry.pin_risk_reason(today, k, "P", Decimal("101"), et(2026, 10, 22, 13, 1)) is not None
    assert expiry.pin_risk_reason(today, k, "P", Decimal("110"), et(2026, 10, 22, 13, 1)) is None
    assert expiry.pin_risk_reason(date(2026, 10, 23), k, "P", Decimal("100"), et(2026, 10, 22, 13, 1)) is None


def test_assignment_window():
    today = date(2026, 10, 22)
    assert not expiry.assignment_window_open(today, et(2026, 10, 22, 15, 59))
    assert expiry.assignment_window_open(today, et(2026, 10, 22, 16, 30))
    assert not expiry.assignment_window_open(today, et(2026, 10, 22, 17, 46))
    assert expiry.expiry_settled_at(today) == et(2026, 10, 22, 17, 45)


def test_ex_dividend_window():
    now = et(2026, 10, 22, 10, 0)
    assert expiry.ex_dividend_assignment_reason(date(2026, 11, 20), date(2026, 10, 23), now) is not None
    assert expiry.ex_dividend_assignment_reason(date(2026, 11, 20), date(2026, 11, 2), now) is None
    assert expiry.ex_dividend_assignment_reason(date(2026, 10, 22), date(2026, 10, 23), now) is None  # after expiry
    assert expiry.ex_dividend_assignment_reason(date(2026, 11, 20), None, now) is None


# ---- auth_lease ------------------------------------------------------------------------------


def test_lease_warns_on_tiers_and_blocks_opens_inside_guard_horizon():
    now = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)
    lease = auth_lease.Lease("schwab_refresh", now - timedelta(days=6, hours=20), timedelta(days=7), renewable_unattended=False)
    assert "4.0h" in lease.warning(now)
    assert auth_lease.open_blocked_reason(lease, now, timedelta(hours=18)) is not None
    assert auth_lease.open_blocked_reason(lease, now, timedelta(hours=2)) is None
    fresh = auth_lease.Lease("schwab_refresh", now, timedelta(days=7), renewable_unattended=False)
    assert fresh.warning(now) is None


def test_expired_lease_blocks_everything():
    now = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)
    lease = auth_lease.Lease("x", now - timedelta(days=8), timedelta(days=7), renewable_unattended=False)
    assert lease.expired(now)
    assert "expired" in auth_lease.open_blocked_reason(lease, now, timedelta(hours=1))
    assert "EXPIRED" in lease.warning(now)


def test_unattended_renewable_lease_never_blocks_or_warns():
    now = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)
    lease = auth_lease.Lease("tt_access", now - timedelta(minutes=14), timedelta(minutes=15), renewable_unattended=True)
    assert lease.warning(now) is None
    assert auth_lease.open_blocked_reason(lease, now, timedelta(hours=18)) is None


def test_no_clock_lease_never_blocks():
    now = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)
    lease = auth_lease.Lease("tradier", now - timedelta(days=400), None, renewable_unattended=False)
    assert lease.remaining(now) is None
    assert auth_lease.open_blocked_reason(lease, now, timedelta(hours=18)) is None


# ---- instance --------------------------------------------------------------------------------


def test_instance_lock_refuses_second_holder():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "bot.lock")
        with instance.InstanceLock(path):
            with pytest.raises(RuntimeError, match="another instance"):
                with instance.InstanceLock(path):
                    pass
        with instance.InstanceLock(path):  # released
            pass


@pytest.mark.parametrize(
    "url,live,bad",
    [
        ("https://sandbox.tradier.com/v1", True, True),
        ("https://api.tradier.com/v1", True, False),
        ("https://api.tradier.com/v1", False, True),
        ("https://paper-api.alpaca.markets", False, False),
        ("https://api.cert.tastyworks.com", True, True),
    ],
)
def test_mode_mismatch(url, live, bad):
    assert (instance.mode_mismatch_reason(url, live=live) is not None) is bad


def test_loopback_gateway_is_judged_by_account_id():
    gw = "https://localhost:5000/v1/api"
    assert instance.mode_mismatch_reason(gw, live=True) is not None  # cannot decide without an account id
    assert instance.mode_mismatch_reason(gw, live=True, account_id="U1234567") is None
    assert instance.mode_mismatch_reason(gw, live=True, account_id="DU1234567") is not None
    assert instance.mode_mismatch_reason(gw, live=False, account_id="DU1234567") is None
    assert instance.mode_mismatch_reason(gw, live=False, account_id="U1234567") is not None
    assert instance.mode_mismatch_reason(gw, live=False, account_id=" du1234567 ") is None


# ---- close_sizing ------------------------------------------------------------------------------


def test_closable_qty_never_flips_sign_or_double_reserves():
    assert close_sizing.closable_qty(1, -1, 0).send == 1
    assert close_sizing.closable_qty(2, -1, 0).send == 1
    assert close_sizing.closable_qty(1, 0, 0).send == 0
    assert close_sizing.closable_qty(1, -2, 2).send == 0
    assert close_sizing.closable_qty(2, -3, 1) == close_sizing.CloseSize(2, None)
    assert close_sizing.closable_qty(0, -3, 0).send == 0


def test_resting_opposite_detection():
    assert close_sizing.resting_opposite_in_the_way(["sell"], "buy")
    assert not close_sizing.resting_opposite_in_the_way(["buy"], "buy")
    assert not close_sizing.resting_opposite_in_the_way([], "buy")


# ---- adopt ----------------------------------------------------------------------------------------


def _cand(oid, sym="O:X", side="sell", qty=1, px="0.50", at=None, tag=None):
    return adopt.Candidate(oid, sym, side, qty, Decimal(px), at, tag)


def test_adopt_by_tag_then_by_shape_and_ambiguity():
    t = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)
    kw = dict(client_order_id="c1", contract_symbol="O:X", side="sell", quantity=1, limit_price=Decimal("0.50"), attempted_at=t)
    assert adopt.match_unidentified([_cand("a", tag="c1"), _cand("b")], **kw).broker_order_id == "a"
    assert adopt.match_unidentified([_cand("b", at=t)], **kw).verdict == "adopted"
    assert adopt.match_unidentified([_cand("b", at=t), _cand("c", at=t)], **kw).verdict == "ambiguous"
    assert adopt.match_unidentified([_cand("b", at=t - timedelta(hours=1))], **kw).verdict == "none"
    assert adopt.match_unidentified([_cand("b", qty=2, at=t)], **kw).verdict == "none"
    assert adopt.match_unidentified([_cand("a", tag="c1"), _cand("b", tag="c1")], **kw).verdict == "ambiguous"
    # An identical order tagged with ANOTHER client id is someone else's: never adopted by shape.
    assert adopt.match_unidentified([_cand("b", at=t, tag="c2")], **kw).verdict == "none"
    # No timestamp, a naive timestamp, or no limit: never a shape match (yesterday's resting close).
    assert adopt.match_unidentified([_cand("b", at=None)], **kw).verdict == "none"
    assert adopt.match_unidentified([_cand("a", at=None, tag="c1")], **kw).verdict == "adopted"  # the tag path needs no timestamp
    assert adopt.match_unidentified([_cand("b", at=datetime(2026, 10, 22, 15, 0))], **kw).verdict == "none"
    assert adopt.match_unidentified([adopt.Candidate("b", "O:X", "sell", 1, None, t)], **kw).verdict == "none"


# ---- book_check ------------------------------------------------------------------------------------


def test_book_check_flags_equity_orphans_and_non_tradable():
    f = book_check.check_book(
        {"TQQQ": 100, "QQQ": 0}, {"O:TQQQ261120P00050000": -1, "O:QQQ261120P00500000": -1}, {"O:QQQ261120P00500000"}, {"TQQQ"}
    )
    issues = [(x.issue, x.symbol) for x in f]
    assert (book_check.BookIssue.UNEXPECTED_EQUITY, "TQQQ") in issues
    assert (book_check.BookIssue.ORPHAN_OPTION, "O:TQQQ261120P00050000") in issues
    assert (book_check.BookIssue.NON_TRADABLE, "TQQQ") in issues
    assert (book_check.BookIssue.UNEXPECTED_EQUITY, "QQQ") not in issues
    assert book_check.opens_blocked_tickers(f) == {"TQQQ"}


def test_book_check_clean():
    assert book_check.check_book({}, {"O:QQQ261120P00500000": -1}, {"O:QQQ261120P00500000"}) == []


# ---- ticks ------------------------------------------------------------------------------------------


def test_index_tick_is_always_a_nickel():
    assert tick_for("O:SPX261120P05000000", Decimal("7.52")) == Decimal("0.05")
    assert tick_for("SPX", Decimal("7.52")) == Decimal("0.05")
    assert tick_for("SPXW", Decimal("0.30")) == Decimal("0.05")
    assert tick_for("TQQQ", Decimal("7.52")) == Decimal("0.10")
    assert tick_for("TQQQ", Decimal("0.30")) == Decimal("0.05")


@pytest.mark.parametrize("mod", ["schwab", "tradier", "tastytrade", "ibkr", "alpaca"])
def test_every_rest_adapter_min_tick_uses_the_index_grid(mod):
    import importlib
    import inspect

    src = inspect.getsource(importlib.import_module(f"agentic_guardrails.brokers.{mod}"))
    assert "c.tick_for(contract_symbol, price)" in src


def test_sub_display_minimum_bid():
    assert sub_display_min_reason(Decimal("0.01"), True) is not None
    assert sub_display_min_reason(Decimal("0.05"), True) is None
    assert sub_display_min_reason(Decimal("0.01"), False) is None
