"""Factor-concentration and earnings guards (Sep 6 2026 additions).

Both close holes that the existing per-ticker cap and post-hoc earnings ALERT
left open on the opening path.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from agentic_guardrails.guards import (
    Caps,
    ExecContext,
    evaluate,
    factor_of,
    guard_factor_concentration,
    guard_no_earnings_in_window,
    guard_portfolio_concentration,
    underlier_has_earnings,
)
from agentic_guardrails.ids import client_order_id
from agentic_guardrails.types import OrderIntent, OrderPlan, PriceBasis, QuoteSource

NOW = datetime(2026, 9, 6, 15, 0, tzinfo=timezone.utc)
EXP = date(2026, 9, 18)
SYM = "O:NVDL260918P00030000"


def _plan(intent=OrderIntent.STO, qty=1, strike="30") -> OrderPlan:
    return OrderPlan(
        client_order_id=client_order_id("dsl_skewrich_25d", SYM, intent, EXP),
        strategy="dsl_skewrich_25d",
        contract_symbol=SYM,
        intent=intent,
        quantity=qty,
        limit_price=Decimal("0.65"),
        strike=Decimal(strike),
        expiration=EXP,
        basis=PriceBasis("sto_bid", QuoteSource.ROBINHOOD, Decimal("0.65"), Decimal("0.70"), NOW),
        created_at=NOW,
    )


def _ctx(**kw) -> ExecContext:
    base = dict(
        free_cash=Decimal("1000000"),
        open_collateral_total=Decimal(0),
        open_collateral_ticker=Decimal(0),
        open_collateral_strategy=Decimal(0),
        committed_today=Decimal(0),
        ticker="NVDL",
        strategy_has_unknown_order=False,
    )
    base.update(kw)
    return ExecContext(**base)


# ── factor concentration ─────────────────────────────────────────────────────
def test_leveraged_wrappers_map_to_their_underlying_factor():
    assert factor_of("TQQQ") == factor_of("QLD") == factor_of("QQQ") == "NDX"
    assert factor_of("UPRO") == factor_of("SPY") == "SPX"
    assert factor_of("NVDL") == "NVDA"
    assert factor_of("nvdl") == "NVDA"  # case-insensitive
    # NVDX is a 2x NVDA wrapper; it must share the NVDA factor (not self-factor)
    # so it is (a) grouped with NVDL by the factor cap and (b) earnings-resolvable
    # to NVDA by the open loop's provider instead of failing closed forever.
    assert factor_of("NVDX") == "NVDA"


def test_unknown_ticker_is_its_own_factor_and_cannot_borrow_headroom():
    """A new ticker must never be silently pooled into someone else's budget."""
    assert factor_of("WEIRD") == "WEIRD"
    caps = Caps(per_factor=Decimal("10000"))
    ctx = _ctx(ticker="WEIRD", open_collateral_by_factor={"NDX": Decimal("9999")})
    assert guard_factor_concentration(_plan(), ctx, caps).allow


def test_the_tech_stack_defeats_per_factor_alone_and_needs_the_portfolio_cap():
    """The review finding on PR #450: NVDL, AMZU, TQQQ and UPRO map to FOUR
    different factors (NVDA, AMZN, NDX, SPX), so per_factor alone lets the
    correlated tech book stack to 4x that cap. per_portfolio is the honest bound,
    since every name in this universe is long US equity beta."""
    assert len({factor_of(t) for t in ("NVDL", "AMZU", "TQQQ", "UPRO")}) == 4
    caps = Caps(per_factor=Decimal("5000"), per_portfolio=Decimal("12000"))
    # Each factor is individually under its cap...
    spread = {"NVDA": Decimal("4000"), "AMZN": Decimal("4000"), "NDX": Decimal("4000")}
    ctx = _ctx(ticker="UPRO", open_collateral_by_factor=spread, open_collateral_total=Decimal("12000"))
    assert guard_factor_concentration(_plan(), ctx, caps).allow  # SPX is empty, so this passes
    # ...but the book as a whole is already at its ceiling.
    res = guard_portfolio_concentration(_plan(), ctx, caps)
    assert not res.allow
    assert "portfolio cap" in res.reason
    assert not evaluate(_plan(), ctx, caps).allow


def test_portfolio_cap_reads_the_previously_dead_total_field():
    """open_collateral_total was populated by the engine and read by NO guard."""
    caps = Caps(per_portfolio=Decimal("5000"))
    assert guard_portfolio_concentration(_plan(), _ctx(open_collateral_total=Decimal("1000")), caps).allow
    assert not guard_portfolio_concentration(_plan(), _ctx(open_collateral_total=Decimal("4500")), caps).allow


def test_portfolio_cap_ignores_closes_and_is_optional():
    caps = Caps(per_portfolio=Decimal("1"))
    assert guard_portfolio_concentration(_plan(intent=OrderIntent.BTC), _ctx(open_collateral_total=Decimal("9e9")), caps).allow
    assert guard_portfolio_concentration(_plan(), _ctx(open_collateral_total=Decimal("9e9")), Caps()).allow


def test_single_name_wrappers_are_known_to_have_earnings():
    """Drives the engine's fail-closed decision when no provider is configured."""
    for t in ("NVDL", "NVDX", "AMZU", "TSLL", "AAPU", "GGLL"):
        assert underlier_has_earnings(t), t
    for t in ("TQQQ", "QQQ", "UPRO", "SPY", "TNA"):
        assert not underlier_has_earnings(t), t
    assert underlier_has_earnings("WEIRD")  # unknown: assume it can, the safe reading


def test_four_tickers_on_one_factor_are_capped_together():
    """Two tickers on ONE factor (TQQQ and QLD are both the Nasdaq) share a
    budget that per-ticker caps would treat as independent."""
    caps = Caps(per_ticker=Decimal("100000"), per_factor=Decimal("5000"))
    ctx = _ctx(ticker="TQQQ", open_collateral_by_factor={"NDX": Decimal("4500")})
    res = guard_factor_concentration(_plan(qty=1, strike="30"), ctx, caps)  # needs 3000
    assert not res.allow
    assert "factor cap (NDX)" in res.reason


def test_factor_cap_allows_within_budget():
    caps = Caps(per_factor=Decimal("10000"))
    ctx = _ctx(ticker="TQQQ", open_collateral_by_factor={"NDX": Decimal("4500")})
    assert guard_factor_concentration(_plan(), ctx, caps).allow


def test_closing_orders_are_never_factor_capped():
    caps = Caps(per_factor=Decimal("1"))
    ctx = _ctx(open_collateral_by_factor={"NVDA": Decimal("999999")})
    assert guard_factor_concentration(_plan(intent=OrderIntent.BTC), ctx, caps).allow


def test_no_factor_cap_configured_is_a_no_op():
    ctx = _ctx(open_collateral_by_factor={"NVDA": Decimal("999999")})
    assert guard_factor_concentration(_plan(), ctx, Caps()).allow


# ── earnings in window ───────────────────────────────────────────────────────
def test_opening_into_an_earnings_window_is_refused():
    ctx = _ctx(next_earnings=date(2026, 9, 10), expiration=date(2026, 9, 18))
    res = guard_no_earnings_in_window(_plan(), ctx, Caps())
    assert not res.allow
    assert "reports earnings 2026-09-10" in res.reason


def test_earnings_after_expiry_is_fine():
    ctx = _ctx(next_earnings=date(2026, 10, 20), expiration=date(2026, 9, 18))
    assert guard_no_earnings_in_window(_plan(), ctx, Caps()).allow


def test_earnings_on_expiry_day_still_counts_as_in_window():
    ctx = _ctx(next_earnings=date(2026, 9, 18), expiration=date(2026, 9, 18))
    assert not guard_no_earnings_in_window(_plan(), ctx, Caps()).allow


def test_index_wrapper_with_no_scheduled_event_passes():
    ctx = _ctx(ticker="TQQQ", next_earnings=None, expiration=date(2026, 9, 18))
    assert guard_no_earnings_in_window(_plan(), ctx, Caps()).allow


def test_a_failed_earnings_lookup_fails_closed():
    """Absent and unreadable must not look the same: unreadable refuses."""
    ctx = _ctx(next_earnings=None, earnings_lookup_failed=True, expiration=date(2026, 9, 18))
    res = guard_no_earnings_in_window(_plan(), ctx, Caps())
    assert not res.allow
    assert "fail closed" in res.reason


def test_closing_into_an_event_is_always_allowed():
    """Buying back before earnings REDUCES event risk; never block that."""
    ctx = _ctx(next_earnings=date(2026, 9, 10), expiration=date(2026, 9, 18), earnings_lookup_failed=True)
    assert guard_no_earnings_in_window(_plan(intent=OrderIntent.BTC), ctx, Caps()).allow


# ── wiring ───────────────────────────────────────────────────────────────────
def test_both_new_guards_are_in_the_evaluated_chain():
    earn = _ctx(next_earnings=date(2026, 9, 10), expiration=date(2026, 9, 18))
    assert not evaluate(_plan(), earn, Caps()).allow
    conc = _ctx(ticker="TQQQ", open_collateral_by_factor={"NDX": Decimal("4500")})
    assert not evaluate(_plan(), conc, Caps(per_factor=Decimal("5000"))).allow


def test_default_context_is_backward_compatible():
    """Existing callers that pass none of the new fields keep passing."""
    assert evaluate(_plan(), _ctx(), Caps()).allow
