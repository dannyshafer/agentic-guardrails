"""Vertical put credit spreads: the pure plan builder (Phase 2).

Why spreads: the capital-year replay of the validated entries. The same
entries earn ~32% ROC per capital-year as verticals vs ~8% as cash-secured puts,
and on a small pool no index CSP ever fits while a TQQQ 10%-wing vertical
does ($660 of collateral). The tail is fat and CLUSTERED (max losses arrive in
bunches in a bear), so the binding control is CONCURRENCY (3-4 open at once),
enforced by `guards.guard_spread_concurrency`, not the entry gate.

This module turns two quotes and two OCC symbols into an `OrderPlan` the engine
already knows how to guard, review, place, settle, ledger and reconcile. It is
stdlib-only and I/O-free like the rest of the decision core; the caller (a pilot
script, later a scanner loop) fetches the quotes and picks the legs, the scanner's
`select_put_spread` being the selection rule of record.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from agentic_guardrails.ids import client_order_id
from agentic_guardrails.pricing import pcs_close_limit, pcs_open_limit
from agentic_guardrails.types import OrderIntent, OrderPlan, PriceBasis, Quote, occ_parts


def _occ(contract_symbol: str) -> tuple[str, date, str, Decimal]:
    """(root, expiration, 'P'|'C', strike) via the core's strict OCC parser."""
    o = occ_parts(contract_symbol)
    return o.root, o.expiration, o.kind, o.strike


def build_pcs_open(
    *,
    strategy: str,
    short_quote: Quote,
    long_quote: Quote,
    quantity: int,
    now: datetime,
    k: Decimal = Decimal(1),
) -> OrderPlan:
    """A sell-to-open vertical put credit spread plan: short `short_quote`'s
    contract, long `long_quote`'s, net credit limit from `pricing.pcs_open_limit`
    (k=1 = the paper book's bid-cross-ask basis). Both legs must be PUTS on the
    same root and expiration with the wing strictly below the short; anything
    else raises here (before any guard runs) so a wrong-leg plan can never reach
    the broker."""
    s_root, s_exp, s_cp, s_strike = _occ(short_quote.contract_symbol)
    l_root, l_exp, l_cp, l_strike = _occ(long_quote.contract_symbol)
    if (s_cp, l_cp) != ("P", "P"):
        raise ValueError("a put credit spread needs two PUT legs")
    if s_root != l_root or s_exp != l_exp:
        raise ValueError(f"spread legs must share root and expiration ({s_root} {s_exp} vs {l_root} {l_exp})")
    if l_strike >= s_strike:
        raise ValueError(f"the wing {l_strike} must be strictly below the short strike {s_strike}")
    limit = pcs_open_limit(short_quote, long_quote, k)
    basis = PriceBasis(
        rule="pcs_open_bid_cross_ask" if k >= 1 else f"pcs_open_mid_minus_{k}_halfspread",
        quote_source=short_quote.source,
        bid=short_quote.bid - long_quote.ask,  # the net at the worst executable side
        ask=short_quote.ask - long_quote.bid,
        as_of=short_quote.as_of,
    )
    return OrderPlan(
        client_order_id=client_order_id(
            strategy, short_quote.contract_symbol, OrderIntent.PCS_OPEN, now.date(), long_quote.contract_symbol
        ),
        strategy=strategy,
        contract_symbol=short_quote.contract_symbol,
        intent=OrderIntent.PCS_OPEN,
        quantity=quantity,
        limit_price=limit,
        strike=s_strike,
        expiration=s_exp,
        basis=basis,
        created_at=now,
        long_contract_symbol=long_quote.contract_symbol,
        long_strike=l_strike,
    )


def build_pcs_close(
    *,
    strategy: str,
    short_quote: Quote,
    long_quote: Quote,
    quantity: int,
    entry_credit: Decimal,
    now: datetime,
    fraction: Decimal = Decimal("0.5"),
) -> OrderPlan:
    """A buy-to-close plan for an open vertical at `fraction` of the entry credit
    (net debit limit, capped at ask-cross-bid). Hold is the finding's default
    management for spreads; this is the risk-reducing exit."""
    s_root, s_exp, s_cp, s_strike = _occ(short_quote.contract_symbol)
    l_root, l_exp, l_cp, l_strike = _occ(long_quote.contract_symbol)
    if (s_cp, l_cp) != ("P", "P") or s_root != l_root or s_exp != l_exp or l_strike >= s_strike:
        raise ValueError("close legs must be the vertical's own two put legs")
    limit = pcs_close_limit(short_quote, long_quote, entry_credit, fraction)
    basis = PriceBasis(
        rule=f"pcs_close_{fraction}_of_credit",
        quote_source=short_quote.source,
        bid=short_quote.bid - long_quote.ask,
        ask=short_quote.ask - long_quote.bid,
        as_of=short_quote.as_of,
    )
    return OrderPlan(
        client_order_id=client_order_id(
            strategy, short_quote.contract_symbol, OrderIntent.PCS_CLOSE, now.date(), long_quote.contract_symbol
        ),
        strategy=strategy,
        contract_symbol=short_quote.contract_symbol,
        intent=OrderIntent.PCS_CLOSE,
        quantity=quantity,
        limit_price=limit,
        strike=s_strike,
        expiration=s_exp,
        basis=basis,
        created_at=now,
        long_contract_symbol=long_quote.contract_symbol,
        long_strike=l_strike,
    )
