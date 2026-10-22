"""FactorMap and the config dataclasses: the conservative defaults are the point."""

from __future__ import annotations

from decimal import Decimal

from agentic_guardrails import guards
from agentic_guardrails.config import FixedCaps, GuardrailConfig, ProportionalCaps
from agentic_guardrails.factors import POLYSENTINEL_FACTORS, FactorMap


def test_factor_map_unknown_symbol_is_its_own_factor_and_a_single_name():
    fm = FactorMap({"tqqq": "ndx"}, {"nvda"})
    assert fm.factor_of("TQQQ") == "NDX" and fm.factor_of("qld") == "QLD"
    assert fm.underlier_has_earnings("QLD") is True  # unknown: refuse rather than sell through an event
    assert fm.underlier_has_earnings("TQQQ") is False
    assert FactorMap({"nvdl": "nvda"}, {"nvda"}).underlier_has_earnings("NVDL") is True


def test_factor_map_is_immutable():
    fm = FactorMap({"A": "B"})
    try:
        fm.symbol_to_factor["C"] = "D"  # type: ignore[index]
    except TypeError:
        return
    raise AssertionError("FactorMap must be read-only")


def test_polysentinel_map_matches_the_core_table():
    for sym in guards.FACTOR_OF:
        assert POLYSENTINEL_FACTORS.factor_of(sym) == guards.factor_of(sym)
        assert POLYSENTINEL_FACTORS.underlier_has_earnings(sym) == guards.underlier_has_earnings(sym)
    assert POLYSENTINEL_FACTORS.factor_of("TQQQ") == "NDX" and "NVDA" in POLYSENTINEL_FACTORS.factors()


def test_proportional_caps_scale_with_cash_and_fall_back_on_a_failed_read():
    pc, fixed = ProportionalCaps(), FixedCaps()
    caps = pc.caps_for(Decimal("19000"), fixed)
    assert caps.per_order == Decimal("14250.00") and caps.per_factor == caps.per_ticker == caps.per_order
    assert caps.per_portfolio == caps.per_day == caps.per_strategy == Decimal("19000.00")
    assert caps.per_expiry == Decimal("14250.00")
    assert pc.caps_for(None, fixed) == fixed.as_caps()
    assert pc.caps_for(Decimal("0"), fixed) == fixed.as_caps()
    assert ProportionalCaps(enabled=False).caps_for(Decimal("19000"), fixed) == fixed.as_caps()
    low = pc.caps_for(Decimal("1000"), fixed)
    assert low.per_order == Decimal("750.00")  # no floor: a low read tightens


def test_guardrail_config_defaults_are_dark():
    cfg = GuardrailConfig()
    assert cfg.enabled is False and cfg.shadow is True and cfg.guard_shadow is True
    ec = cfg.engine_config(cfg.fixed_caps.as_caps())
    assert ec.shadow is True and ec.caps.per_portfolio == Decimal("16000")
    assert cfg.assignment_guard.pre_cutoff_breach is False


def test_config_caps_carry_the_spread_limits_in_both_fixed_and_proportional_form():
    cfg = GuardrailConfig()
    for caps in (cfg.caps_for(None), cfg.caps_for(Decimal("20000")), cfg.engine_config().caps):
        assert caps.max_open_spreads_per_strategy == 4 and caps.min_spread_credit_frac == Decimal("0.10")
    custom = GuardrailConfig(max_open_spreads_per_strategy=2, min_spread_credit_frac=Decimal("0.2"))
    assert custom.caps_for(Decimal("20000")).max_open_spreads_per_strategy == 2
    assert custom.caps_for(None).min_spread_credit_frac == Decimal("0.2")


def test_spread_limits_from_config_actually_block_at_the_engine():
    """Connected, not decorative: with the config's cap at 1, a second spread on the strategy is refused."""
    import asyncio
    from datetime import datetime, timedelta, timezone

    from agentic_guardrails.broker import SimBroker
    from agentic_guardrails.engine import Engine
    from agentic_guardrails.killswitch import Freshness, KillState
    from agentic_guardrails.ledger import InMemoryLedger
    from agentic_guardrails.spreads import build_pcs_open
    from agentic_guardrails.types import Account, Quote, QuoteSource

    now = datetime(2026, 9, 16, 15, 0, tzinfo=timezone.utc)
    short, wing_a, wing_b = "O:TQQQ261016P00080000", "O:TQQQ261016P00072000", "O:TQQQ261016P00070000"

    def q(sym, bid, ask):
        return Quote(sym, Decimal(bid), Decimal(ask), QuoteSource.ROBINHOOD, now)

    broker = SimBroker(account_state=Account(free_cash=Decimal(20000), buying_power=Decimal(20000)))
    broker.quotes[short], broker.quotes[wing_a], broker.quotes[wing_b] = (
        q(short, "1.50", "1.60"),
        q(wing_a, "0.50", "0.56"),
        q(wing_b, "0.40", "0.46"),
    )
    cfg = GuardrailConfig(max_open_spreads_per_strategy=1)
    eng = Engine(broker, InMemoryLedger(), cfg.engine_config(cfg.caps_for(Decimal(20000)), shadow=False), clock=lambda: now)
    kill = KillState(
        True,
        Decimal(0),
        Decimal(500),
        Decimal(0),
        Decimal(1000),
        Freshness(now - timedelta(minutes=5)),
        Freshness(now - timedelta(hours=1)),
    )
    first = asyncio.run(
        eng.submit(
            build_pcs_open(strategy="pcs", short_quote=broker.quotes[short], long_quote=broker.quotes[wing_a], quantity=1, now=now), kill
        )
    )
    second = asyncio.run(
        eng.submit(
            build_pcs_open(strategy="pcs", short_quote=broker.quotes[short], long_quote=broker.quotes[wing_b], quantity=1, now=now), kill
        )
    )
    assert first.accepted, first.reason
    assert not second.accepted and "concurrency" in second.reason


def test_the_configured_factor_map_reaches_the_engine_guards():
    """GuardrailConfig.factors is injected into the engine: a map that pools TQQQ and QLD under one
    factor makes the factor cap name that factor, and a map that calls TQQQ a single name makes the
    missing earnings lookup a refusal (the default map opens it: an index wrapper has no event)."""
    import asyncio
    from datetime import date, datetime, timedelta, timezone

    from agentic_guardrails.broker import SimBroker
    from agentic_guardrails.engine import Engine
    from agentic_guardrails.ids import client_order_id
    from agentic_guardrails.killswitch import Freshness, KillState
    from agentic_guardrails.ledger import InMemoryLedger
    from agentic_guardrails.types import Account, OrderIntent, OrderPlan, PriceBasis, Quote, QuoteSource

    now = datetime(2026, 9, 2, 14, 30, tzinfo=timezone.utc)
    tqqq, qld = "O:TQQQ261016P00080000", "O:QLD261016P00080000"

    def plan(sym):
        basis = PriceBasis("sto_bid", QuoteSource.ROBINHOOD, Decimal("1.50"), Decimal("1.60"), now)
        return OrderPlan(
            client_order_id("s", sym, OrderIntent.STO, now.date()),
            "s",
            sym,
            OrderIntent.STO,
            1,
            Decimal("1.50"),
            Decimal(80),
            date(2026, 10, 16),
            basis,
            now,
        )

    def engine(fm, per_factor):
        broker = SimBroker(account_state=Account(free_cash=Decimal(100000), buying_power=Decimal(100000)))
        for s in (tqqq, qld):
            broker.quotes[s] = Quote(s, Decimal("1.50"), Decimal("1.60"), QuoteSource.ROBINHOOD, now)
        cfg = GuardrailConfig(
            factors=fm,
            fixed_caps=FixedCaps(
                per_order=Decimal(20000),
                per_strategy=Decimal(50000),
                per_day=Decimal(50000),
                per_expiry=Decimal(50000),
                portfolio=Decimal(50000),
                per_factor=Decimal(per_factor),
            ),
        )
        return Engine(broker, InMemoryLedger(), cfg.engine_config(shadow=False, poll_delay_s=0.0), clock=lambda: now), broker

    kill = KillState(
        True,
        Decimal(0),
        Decimal(500),
        Decimal(0),
        Decimal(1000),
        Freshness(now - timedelta(minutes=5)),
        Freshness(now - timedelta(hours=1)),
    )

    pooled = FactorMap({"TQQQ": "POOL", "QLD": "POOL"})
    eng, _ = engine(pooled, 9000)
    assert asyncio.run(eng.submit(plan(tqqq), kill)).accepted  # $8,000 of POOL
    res = asyncio.run(eng.submit(plan(qld), kill))
    assert not res.accepted and "factor cap (POOL)" in res.reason

    single = FactorMap({"TQQQ": "TQQQCO"}, {"TQQQCO"})
    eng, broker = engine(single, 50000)
    res = asyncio.run(eng.submit(plan(tqqq), kill))
    assert not res.accepted and "earnings" in res.reason.lower() and broker._orders == {}

    eng, _ = engine(POLYSENTINEL_FACTORS, 50000)
    assert asyncio.run(eng.submit(plan(tqqq), kill)).accepted


def test_factor_map_earnings_underlier_is_independent_of_the_risk_group():
    grouped = FactorMap({"NVDL": "TECH", "AMZU": "TECH", "TQQQ": "NDX"}, earnings_underliers={"NVDL": "NVDA"})
    assert grouped.factor_of("NVDL") == "TECH" and grouped.earnings_underlier("NVDL") == "NVDA"
    assert grouped.underlier_has_earnings("NVDL") is True  # named a company: a missing date refuses
    assert grouped.earnings_underlier("TQQQ") == "TQQQ" and grouped.underlier_has_earnings("TQQQ") is False
    assert grouped.earnings_underlier("AMZU") == "AMZU" and grouped.underlier_has_earnings("AMZU") is False  # not named: the user's call
    single = FactorMap({"NVDL": "NVDA"}, {"NVDA"})
    assert single.earnings_underlier("NVDL") == "NVDA"  # the factor is the company, no entry needed
    for sym in guards.FACTOR_OF:
        assert POLYSENTINEL_FACTORS.earnings_underlier(sym) == guards.factor_of(sym) or not guards.underlier_has_earnings(sym)


def test_a_grouped_wrapper_still_asks_the_provider_about_its_company_at_the_engine():
    import asyncio
    from datetime import date, datetime, timedelta, timezone

    from agentic_guardrails.broker import SimBroker
    from agentic_guardrails.engine import Engine
    from agentic_guardrails.ids import client_order_id
    from agentic_guardrails.killswitch import Freshness, KillState
    from agentic_guardrails.ledger import InMemoryLedger
    from agentic_guardrails.types import Account, OrderIntent, OrderPlan, PriceBasis, Quote, QuoteSource

    now = datetime(2026, 9, 2, 14, 30, tzinfo=timezone.utc)
    nvdl = "O:NVDL260918P00030000"
    asked = []

    async def provider(t):
        asked.append(t)
        return {"NVDA": date(2026, 9, 10)}.get(t)

    broker = SimBroker(account_state=Account(free_cash=Decimal(100000), buying_power=Decimal(100000)))
    broker.quotes[nvdl] = Quote(nvdl, Decimal("0.65"), Decimal("0.70"), QuoteSource.ROBINHOOD, now)
    cfg = GuardrailConfig(factors=FactorMap({"NVDL": "TECH", "AMZU": "TECH"}, earnings_underliers={"NVDL": "NVDA"}))
    eng = Engine(broker, InMemoryLedger(), cfg.engine_config(shadow=False, poll_delay_s=0.0, earnings_provider=provider), clock=lambda: now)
    basis = PriceBasis("sto_bid", QuoteSource.ROBINHOOD, Decimal("0.65"), Decimal("0.70"), now)
    plan = OrderPlan(
        client_order_id("s", nvdl, OrderIntent.STO, now.date()),
        "s",
        nvdl,
        OrderIntent.STO,
        1,
        Decimal("0.65"),
        Decimal(30),
        date(2026, 9, 18),
        basis,
        now,
    )
    kill = KillState(
        True,
        Decimal(0),
        Decimal(500),
        Decimal(0),
        Decimal(1000),
        Freshness(now - timedelta(minutes=5)),
        Freshness(now - timedelta(hours=1)),
    )
    res = asyncio.run(eng.submit(plan, kill))
    assert not res.accepted and "earnings 2026-09-10" in res.reason
    assert "NVDA" in asked and "TECH" not in asked and broker._orders == {}
