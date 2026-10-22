"""Frozen copies of the pre-refactor executor functions (master at the complexity-20 wave), kept as
source strings so differential tests can exec them and they are not complexity offenders. Do not edit."""

RECORDS = r'''def _records(payload: Any, *list_keys: str) -> list[dict]:
    """Every record as a dict, FAILING CLOSED on a non-object entry. A list with a
    non-dict element is a malformed payload, not something to silently filter: a
    dropped position understates exposure and can arm the health gate against a
    short we do not know we hold (caught in review of PR #476). So a stray
    non-object raises rather than being skipped."""
    if isinstance(payload, dict):
        for k in list_keys:
            v = payload.get(k)
            if isinstance(v, list):
                if not all(isinstance(x, dict) for x in v):
                    raise BrokerMalformed(f"{k} list has a non-object entry")
                return list(v)
            # A SINGULAR keyed object, e.g. place_option_order returns
            # {"data":{"order":{...}}} (verified live Sep 10 2026). Treat a
            # dict-valued key as one record — without this it fell through to
            # `return [payload]` (the WRAPPER {"order":{...}}), whose top level
            # has no id/state, so a filled place read as UNKNOWN with no
            # broker_order_id and the ledger never recorded the live fill.
            if isinstance(v, dict):
                return [v]
        return [payload]
    if isinstance(payload, list):
        if not all(isinstance(x, dict) for x in payload):
            raise BrokerMalformed("record list has a non-object entry")
        return list(payload)
    raise BrokerMalformed(f"expected an object or list of objects, got {type(payload)}")
'''

UNACK = r'''def unacknowledged_drift(findings: list[Finding], acknowledged: frozenset[str] | set[str] = frozenset()) -> list[Finding]:
    """Every finding that still means "the ledger does not describe the
    account", after the human acknowledgement policy for OCC-adjusted contracts.

    The policy is EXACT-symbol, never a family match:
    - an adjusted BROKER contract is waived only when its own symbol is in
      `acknowledged` (acking NVDL1 $34P does not waive NVDL1 $30P);
    - a legacy LEDGER symbol paired with adjusted contracts is waived only when
      the acknowledged adjusted broker quantity on its (base root, expiry, type)
      key exactly covers the legacy ledger quantity on that key. Any unacked
      adjusted contract on the key, or any excess / shortfall, keeps every
      finding on that key blocking.
    MATCH and DUST are benign; everything else (OVER, UNDER, ASSIGNMENT,
    unwaived ADJUSTED) is returned."""
    acked = {a.strip().upper() for a in acknowledged}
    adj_broker = [f for f in findings if f.drift == DriftClass.ADJUSTED and is_adjusted_symbol(f.contract_symbol)]
    legacy = [f for f in findings if f.drift == DriftClass.ADJUSTED and not is_adjusted_symbol(f.contract_symbol)]
    # A multiplier-only adjustment keeps a standard-looking symbol; it is a
    # broker-side finding when the broker holds it, and must itself be acked.
    adj_broker += [f for f in legacy if f.broker_qty != 0]
    legacy = [f for f in legacy if f.broker_qty == 0]

    waived: set[str] = {f.contract_symbol for f in adj_broker if f.contract_symbol in acked}
    by_key: dict = {}
    for f in adj_broker:
        k = adjusted_base_key(f.contract_symbol)
        tot = by_key.setdefault(k, {"acked": 0, "unacked": 0, "legacy": 0})
        tot["acked" if f.contract_symbol in acked else "unacked"] += f.broker_qty
    for f in legacy:
        by_key.setdefault(adjusted_base_key(f.contract_symbol), {"acked": 0, "unacked": 0, "legacy": 0})["legacy"] += f.ledger_qty
    for f in legacy:
        tot = by_key[adjusted_base_key(f.contract_symbol)]
        if tot["unacked"] == 0 and tot["acked"] != 0 and tot["acked"] == tot["legacy"]:
            waived.add(f.contract_symbol)

    benign = {DriftClass.MATCH, DriftClass.DUST}
    return [f for f in findings if f.drift not in benign and f.contract_symbol not in waived]
'''

POST_INIT = r"""def __post_init__(self) -> None:
        # Structural invariants that must hold for ANY plan the core will touch.
        # A malformed plan is a programming error, not a stand-down, so it raises.
        if self.time_in_force not in TIME_IN_FORCE:
            raise ValueError(f"OrderPlan.time_in_force must be one of {sorted(TIME_IN_FORCE)}, got {self.time_in_force!r}")
        if self.time_in_force == "gtc" and self.intent not in CLOSING_INTENTS:
            raise ValueError("only a closing order may rest GTC; an opening order is a day order")
        if self.quantity <= 0:
            raise ValueError(f"OrderPlan.quantity must be > 0, got {self.quantity}")
        if self.limit_price <= 0:
            raise ValueError("OrderPlan.limit_price must be > 0 (no market orders)")
        if self.strike <= 0:
            raise ValueError("OrderPlan.strike must be > 0")
        if self.intent in SPREAD_INTENTS:
            if not self.long_contract_symbol or self.long_strike is None:
                raise ValueError("a spread plan needs its long leg (long_contract_symbol + long_strike)")
            if self.long_strike <= 0 or self.long_strike >= self.strike:
                raise ValueError("a put credit spread's wing must sit strictly BELOW the short strike")
            if self.long_contract_symbol.strip().upper() == self.contract_symbol.strip().upper():
                raise ValueError("a spread's two legs must be different contracts")
            if self.limit_price >= self.width():
                raise ValueError("a spread's net price must be below its width (a credit at or above the width is not a spread)")
            # The symbols are what the broker trades; the strike/expiration
            # fields are what the caps and guards read. They must be the SAME
            # contracts: two puts, one root, one expiration equal to the plan's,
            # strikes equal to the plan's. Otherwise a plan could sell calls
            # under PCS_OPEN or fabricate a $1 width that every collateral cap
            # then trusts (Devin, PR #529).
            short_leg = occ_parts(self.contract_symbol)
            wing_leg = occ_parts(self.long_contract_symbol)
            if (short_leg.kind, wing_leg.kind) != ("P", "P"):
                raise ValueError("a put credit spread needs two PUT legs")
            if short_leg.root != wing_leg.root:
                raise ValueError(f"spread legs are on different roots ({short_leg.root} vs {wing_leg.root})")
            if short_leg.expiration != wing_leg.expiration or short_leg.expiration != self.expiration:
                raise ValueError("spread legs must share the plan's expiration (a different wing expiry is a diagonal, not a vertical)")
            if short_leg.strike != self.strike or wing_leg.strike != self.long_strike:
                raise ValueError("spread strike fields do not match the leg symbols")
        elif self.long_contract_symbol is not None or self.long_strike is not None:
            raise ValueError(f"intent {self.intent.value} is single-leg; it cannot carry a long leg")
"""

CONTEXT = r"""async def _context(self, plan: OrderPlan) -> guards.ExecContext:
        records = await self.ledger.all_orders()
        total, by_ticker, by_strategy = open_collateral(records, ticker_of=ticker_of)
        today = self.clock().date()
        committed_today = sum(
            (
                r.collateral_per_contract() * Decimal(r.filled_qty)
                for r in records
                if not r.shadow and r.intent in OPENING_INTENTS and r.filled_qty > 0 and r.created_at.date() == today
            ),
            Decimal(0),
        )
        unknown = any(r.strategy == plan.strategy and r.state == OrderState.UNKNOWN and not r.shadow for r in records)
        # One in flight per CONTRACT, on EITHER leg: a spread whose wing is
        # already working in another order (or a single-leg order on a contract
        # this spread wants as its wing) is an overlap, not a new order.
        legs = set(plan.legs())
        inflight = tuple(
            r.state
            for r in records
            if (r.contract_symbol in legs or (r.long_contract_symbol in legs if r.long_contract_symbol else False))
            and not r.shadow
            and not is_terminal(r.state)
            and r.state != OrderState.PLANNED
            # A resting take-profit close of ANOTHER arm is not an overlap for this
            # arm's own resting close: two arms can hold one contract at different
            # credits, and each rests its own buy at its own 50%. Every other plan
            # (an open, the assignment guard's protective close) still sees it.
            and not (plan.basis.rule == RESTING_TP_RULE and r.basis_rule == RESTING_TP_RULE and r.strategy != plan.strategy)
        )
        spreads_open = open_spreads(records).get(plan.strategy, 0)
        acct = await self.broker.account()
        t = ticker_of(plan.contract_symbol)
        # Aggregate the per-ticker collateral onto risk factors, so the factor cap
        # sees TQQQ and QLD as one bet. Derived here rather than in
        # open_collateral so the ledger stays free of the risk model.
        by_factor: dict[str, Decimal] = {}
        for tk, amt in by_ticker.items():
            f = guards.factor_of(tk)
            by_factor[f] = by_factor.get(f, Decimal(0)) + amt
        # Same collateral keyed by expiration date, for the expiry cap: every
        # armed arm fires into the same weekly expiry on a scan day, and the
        # factor map cannot see that.
        by_expiry = open_collateral_by_expiry(records)
        # Live opens that have not settled still commit collateral: count their
        # unfilled remainder in every cap scope (the Sep 23 same-tick overshoot).
        for r, left in inflight_open_remainders(records, exclude=plan.client_order_id):
            c = r.collateral_per_contract() * Decimal(left)
            tk = ticker_of(r.contract_symbol)
            total += c
            by_ticker[tk] = by_ticker.get(tk, Decimal(0)) + c
            by_strategy[r.strategy] = by_strategy.get(r.strategy, Decimal(0)) + c
            f = guards.factor_of(tk)
            by_factor[f] = by_factor.get(f, Decimal(0)) + c
            by_expiry[r.expiration] = by_expiry.get(r.expiration, Decimal(0)) + c
            if r.created_at.date() == today:
                committed_today += c
        next_earnings, earnings_failed = await self._earnings(t)
        adjusted, adjusted_read_failed = await self._adjusted_contracts()
        return guards.ExecContext(
            free_cash=acct.free_cash,
            open_collateral_total=total,
            open_collateral_ticker=by_ticker.get(t, Decimal(0)),
            open_collateral_strategy=by_strategy.get(plan.strategy, Decimal(0)),
            committed_today=committed_today,
            ticker=t,
            strategy_has_unknown_order=unknown,
            inflight_states_for_contract=inflight,
            open_collateral_by_factor=by_factor,
            open_collateral_by_expiry=by_expiry,
            next_earnings=next_earnings,
            earnings_lookup_failed=earnings_failed,
            expiration=plan.expiration,
            open_spreads_strategy=spreads_open,
            adjusted_contracts=adjusted,
            adjusted_read_failed=adjusted_read_failed,
        )
"""
