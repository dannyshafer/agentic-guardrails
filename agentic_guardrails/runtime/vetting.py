"""Open-side vetting: the realized-P&L-today pairing, the open limit, the NBBO cross-check and
the sanity floors. All pure. Each one exists because a trade the guards allowed was one no
human would have placed (incident 4: a five-cent credit, a re-entry on a name the guard had just
bought back, a second short stacked onto a threatened one).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from agentic_guardrails.config import GuardrailConfig
from agentic_guardrails.engine import ticker_of
from agentic_guardrails.ledger import inflight_open_remainders, net_positions, open_collateral, open_collateral_by_expiry
from agentic_guardrails.pricing import nbbo_agreement
from agentic_guardrails.types import OPENING_INTENTS, OrderIntent, Quote, QuoteSource

GUARD_STRATEGY = "assign_guard"
NBBO_SCAN_SOURCE = "nbbo"


def _dec(x) -> Decimal | None:
    if x is None:
        return None
    try:
        d = Decimal(str(x))
    except Exception:  # noqa: BLE001 - unreadable reads as None, callers fail closed
        return None
    return d if d.is_finite() else None


def _opening_lots(records) -> dict[tuple[str, str], list]:
    opens: dict[tuple[str, str], list] = {}
    for r in records:
        if r.shadow or r.filled_qty <= 0 or r.avg_fill_price is None or r.intent not in OPENING_INTENTS:
            continue
        opens.setdefault((r.strategy, r.contract_symbol), []).append((r.created_at, r.avg_fill_price, r.filled_qty))
    for lot in opens.values():
        lot.sort(key=lambda t: t[0])
    return opens


def _attribute_close(r, opens):
    """The guard closes under its own strategy tag, never the arm that opened the contract, so
    attribute a close to the single arm with an open on the same contract; an ambiguous one
    (two arms, one contract) stays unattributable and fails closed."""
    owner = r.strategy
    lot = opens.get((owner, r.contract_symbol))
    if not lot:
        owners = {k[0] for k in opens if k[1] == r.contract_symbol}
        if len(owners) == 1:
            owner = owners.pop()
            lot = opens[(owner, r.contract_symbol)]
    return owner, lot


def realized_pnl_today(records, today: date) -> tuple[dict[str, Decimal], Decimal, bool]:
    """(per-strategy, total, ok) realized today in UTC from the ledger alone.

    Pairs each filled CLOSE settled today with its opening lot on the same (strategy,
    contract): realized = (open credit - close debit) x 100 x qty. `ok` is False when a close
    settled today cannot be matched, so the caller stands down rather than open on an unknown
    loss. This is what makes the daily loss stop a real control; a collateral cap measures new
    opening collateral and cannot see a realized loss."""
    opens = _opening_lots(records)
    by_strategy: dict[str, Decimal] = {}
    ok = True
    for r in records:
        if r.shadow or r.filled_qty <= 0 or r.avg_fill_price is None or r.intent in OPENING_INTENTS:
            continue
        if r.updated_at.date() != today:
            continue
        owner, lot = _attribute_close(r, opens)
        if not lot:
            ok = False
            continue
        _, open_px, open_qty = lot[-1]
        qty = min(open_qty, r.filled_qty)
        by_strategy[owner] = by_strategy.get(owner, Decimal(0)) + (open_px - r.avg_fill_price) * Decimal(100) * Decimal(qty)
    return by_strategy, sum(by_strategy.values(), Decimal(0)), ok


def open_limit(recorded_bid, live_bid, live_ask) -> Decimal | None:
    """The sell-to-open limit: the recorded basis lifted to the live bid, never above the live
    ask, None when nothing is readable. A sell limit under the bid fills at the bid anyway, so
    lifting it costs nothing and keeps a venue from refusing a limit far off the market."""

    def pos(x):
        d = _dec(x)
        return d if d is not None and d > 0 else None

    cands = [x for x in (pos(recorded_bid), pos(live_bid)) if x is not None]
    if not cands:
        return None
    basis = max(cands)
    ask = pos(live_ask)
    return min(basis, ask) if ask is not None else basis


def scan_quote_disagreement(recorded_bid, recorded_mid, live_bid, live_ask, recorded_source: str = NBBO_SCAN_SOURCE) -> str | None:
    """Why the live quote does not agree with the quote the signal was priced on, or None.

    Only a recorded quote that IS an NBBO counts as a second book; agreeing with a model price
    verifies nothing. Fails closed: an unreadable side is a disagreement."""
    if (recorded_source or "").strip().lower() != NBBO_SCAN_SOURCE:
        return f"recorded price is {recorded_source or 'unknown'}, not an NBBO; nothing to cross-check against"
    mid = _dec(recorded_mid)
    if mid is None or mid <= 0:
        return "no recorded quote to cross-check"
    rec_bid = _dec(recorded_bid)
    if rec_bid is None or rec_bid <= 0 or rec_bid > mid:
        rec_bid = mid
    lb, la = _dec(live_bid), _dec(live_ask)
    if lb is None or la is None or lb < 0 or la < lb:
        return "live quote unreadable, cannot cross-check against the recorded quote"
    now = datetime.now(timezone.utc)
    chk = nbbo_agreement(Quote("x", lb, la, QuoteSource.ROBINHOOD, now), Quote("x", rec_bid, mid * 2 - rec_bid, QuoteSource.MASSIVE, now))
    return None if chk.agree else f"live/recorded quotes disagree ({chk.reason})"


def limit_below_live_bid_reason(limit, live_bid) -> str | None:
    """A sell limit more than max($0.05, 10% of the bid) under the live bid is far off the
    market and a venue refuses it; stand down. Fails closed on an unreadable input."""
    lim, bid = _dec(limit), _dec(live_bid)
    if lim is None or bid is None or lim <= 0 or bid <= 0:
        return "limit or live bid unreadable, cannot price-check the order"
    tol = max(Decimal("0.05"), Decimal("0.10") * bid)
    if lim < bid - tol:
        return f"limit {lim} is far below the live bid {bid} (tol {tol}); the recorded basis looks stale"
    return None


def _credit_floor_reason(limit: Decimal, strike: Decimal, dte: int, cfg: GuardrailConfig) -> str | None:
    s = cfg.open_sanity
    if limit < s.min_credit:
        return f"credit {limit} below the {s.min_credit} floor"
    if dte < s.min_dte:
        return f"{dte} DTE is inside the {s.min_dte}-day minimum"
    ann = (limit / strike) * Decimal(365) / Decimal(max(dte, 1))
    if ann < s.min_annual_yield:
        return f"premium {ann:.1%}/yr on the live limit is below the {s.min_annual_yield:.0%}/yr bar"
    return None


def _same_day_guard_close_reason(ticker: str, records, today: date) -> str | None:
    for r in records:
        if (
            r.strategy == GUARD_STRATEGY
            and r.intent == OrderIntent.BTC
            and not r.shadow
            and r.filled_qty > 0
            and r.updated_at is not None
            and r.updated_at.date() == today
            and ticker_of(r.contract_symbol) == ticker
        ):
            return f"the assignment guard closed a {ticker} short today; no same-day re-entry"
    return None


def _threatened_short_reason(ticker: str, spot: Decimal, records, cfg: GuardrailConfig) -> str | None:
    cushion = Decimal(1) + cfg.open_sanity.threat_cushion_pct
    by_sym = {r.contract_symbol: r for r in records if r.strike is not None}
    for sym, net in net_positions(records).items():
        if net >= 0 or ticker_of(sym) != ticker or sym not in by_sym:
            continue
        k = Decimal(str(by_sym[sym].strike))
        if spot <= k * cushion:
            return f"existing {ticker} short {sym} is threatened (spot {spot} within {cfg.open_sanity.threat_cushion_pct:.0%} of strike {k}); not adding"
    return None


def _stack_reason(ticker: str, expiration: date, need: Decimal, equity, records, cfg: GuardrailConfig) -> str | None:
    """A SECOND short in a factor or on an expiry already held may only take it to the stack
    fraction of equity; a lone position may be larger. Counts filled positions plus the
    unfilled remainder of in-flight opens. No equity read or fixed-cap mode: no stack check."""
    eq = _dec(equity)
    if eq is None or eq <= 0 or not cfg.proportional_caps.enabled:
        return None
    limit = eq * cfg.proportional_caps.stack
    fm = cfg.factors
    factor = fm.factor_of(ticker)
    _, by_factor, _ = open_collateral(records, ticker_of=lambda sym: fm.factor_of(ticker_of(sym)))
    held_f = by_factor.get(factor, Decimal(0))
    held_e = open_collateral_by_expiry(records).get(expiration, Decimal(0))
    for r, left in inflight_open_remainders(records):
        c = r.collateral_per_contract() * Decimal(left)
        if fm.factor_of(ticker_of(r.contract_symbol)) == factor:
            held_f += c
        if r.expiration == expiration:
            held_e += c
    if held_f > 0 and held_f + need > limit:
        return f"{factor} already holds ${held_f:,.0f}; adding ${need:,.0f} would stack it past the {cfg.proportional_caps.stack:.0%} stack limit (${limit:,.0f})"
    if held_e > 0 and held_e + need > limit:
        return f"{expiration} already resolves ${held_e:,.0f}; adding ${need:,.0f} would stack it past the {cfg.proportional_caps.stack:.0%} stack limit (${limit:,.0f})"
    return None


def open_sanity_reason(
    *, cfg: GuardrailConfig, ticker: str, strike, expiration: date, limit, spot, today: date, records, equity=None
) -> str | None:
    """Why an otherwise-valid open is not worth placing, or None. Judgment floors, not
    correctness guards: credit size, DTE, annual yield on the live limit, no same-day re-entry
    after a guard close, no new short on a threatened underlier, no stacking past the limit."""
    limit_d, strike_d = Decimal(str(limit)), Decimal(str(strike))
    dte = (expiration - today).days
    reason = _credit_floor_reason(limit_d, strike_d, dte, cfg)
    if reason is None:
        reason = _same_day_guard_close_reason(ticker, records, today)
    if reason is None and spot is not None:
        reason = _threatened_short_reason(ticker, Decimal(str(spot)), records, cfg)
    if reason is None:
        reason = _stack_reason(ticker, expiration, strike_d * Decimal(100), equity, records, cfg)
    return reason
