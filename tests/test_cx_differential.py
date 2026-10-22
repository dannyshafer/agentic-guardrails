"""Differential tests pinning the complexity-20 refactors of three executor functions
(broker._records, reconciler.unacknowledged_drift, types.OrderPlan.__post_init__) to the
frozen pre-refactor sources in tests/_frozen_executor_cx_src.py."""

from __future__ import annotations

import dataclasses
import itertools
import random
import textwrap
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from agentic_guardrails import guards as G
from agentic_guardrails import broker as B
from agentic_guardrails import reconciler as R
from agentic_guardrails import types as T
import _frozen_executor_cx_src as F


def _exec(src: str, module) -> object:
    ns = dict(vars(module))
    exec(textwrap.dedent(src), ns)  # noqa: S102 - frozen test fixture
    return ns[src.split("def ", 1)[1].split("(", 1)[0]]


OLD_RECORDS = _exec(F.RECORDS, B)
OLD_UNACK = _exec(F.UNACK, R)
OLD_POST_INIT = _exec("    " + F.POST_INIT, T)


def _run(fn, *a, **kw):
    try:
        return ("ok", fn(*a, **kw))
    except Exception as e:  # noqa: BLE001
        return ("err", type(e).__name__, str(e))


_PAYLOADS = [
    {},
    {"a": 1},
    {"order": {"id": "x"}},
    {"orders": [{"id": 1}, {"id": 2}]},
    {"orders": []},
    {"orders": [{"id": 1}, 3]},
    {"orders": "nope", "order": {"id": "y"}},
    {"results": [{"id": 1}], "orders": [{"id": 2}]},
    {"orders": None, "results": [{"id": 3}]},
    [],
    [{"a": 1}],
    [{"a": 1}, "x"],
    [None],
    None,
    "str",
    5,
]
_KEYS = [(), ("orders",), ("order",), ("orders", "order"), ("results", "orders"), ("instruments", "results")]


@pytest.mark.parametrize("payload", _PAYLOADS)
@pytest.mark.parametrize("keys", _KEYS)
def test_records_matches_old(payload, keys):
    assert _run(B._records, payload, *keys) == _run(OLD_RECORDS, payload, *keys)


_SYMS = [
    "O:NVDL261016P00034000",
    "O:NVDL1261016P00034000",
    "O:NVDL1261016P00030000",
    "O:NVDL2261016P00034000",
    "O:TQQQ261016P00080000",
    "O:TQQQ261023P00080000",
    "not-occ",
]


def _findings(rng):
    out = []
    for sym in rng.sample(_SYMS, rng.randint(0, len(_SYMS))):
        out.append(
            R.Finding(
                sym,
                rng.choice(list(R.DriftClass)),
                rng.choice([-3, -2, -1, 0, 1, 2]),
                rng.choice([-3, -2, -1, 0, 1, 2, 3]),
                "d",
            )
        )
    return out


def _adjusted_findings(rng):
    """Adjusted broker contracts + a legacy ledger symbol on one key, quantities that
    sometimes cover, exceed or fall short of each other."""
    out = []
    for sym in ("O:NVDL1261016P00034000", "O:NVDL1261016P00030000", "O:NVDL2261016P00034000"):
        if rng.random() < 0.7:
            out.append(R.Finding(sym, R.DriftClass.ADJUSTED, 0, rng.choice([-3, -2, -1, 1, 2]), "d"))
    for sym in ("O:NVDL261016P00034000", "O:NVDL261016P00030000"):
        if rng.random() < 0.7:
            out.append(R.Finding(sym, R.DriftClass.ADJUSTED, rng.choice([-3, -2, -1, 1, 2]), rng.choice([0, 0, 0, 1]), "d"))
    return out


def test_unacknowledged_drift_matches_old_on_adjusted_keys():
    rng = random.Random(99)
    adj = ["O:NVDL1261016P00034000", "O:NVDL1261016P00030000", "O:NVDL2261016P00034000", "O:NVDL261016P00034000"]
    for _ in range(4000):
        fs = _adjusted_findings(rng)
        acked = set(rng.sample(adj, rng.randint(0, 3)))
        assert _run(R.unacknowledged_drift, fs, acked) == _run(OLD_UNACK, fs, acked)


def test_unacknowledged_drift_matches_old_fuzz():
    rng = random.Random(20261010)
    for _ in range(4000):
        fs = _findings(rng)
        acked = set(rng.sample(_SYMS, rng.randint(0, 3)))
        if rng.random() < 0.3:
            acked = {a.lower() for a in acked}
        assert _run(R.unacknowledged_drift, fs, acked) == _run(OLD_UNACK, fs, acked)
        assert _run(R.unacknowledged_drift, fs) == _run(OLD_UNACK, fs)


NOW = datetime(2026, 9, 16, 15, 0, tzinfo=timezone.utc)
EXP = date(2026, 10, 16)
BASIS = T.PriceBasis("r", T.QuoteSource.ROBINHOOD, Decimal("1"), Decimal("2"), NOW)
_PLAN_SYMS = ["O:TQQQ261016P00080000", "O:TQQQ261016P00072000", "O:TQQQ261023P00072000", "O:TQQQ261016C00072000", "O:QQQ261016P00072000"]


def _raw_plan(**kw):
    base = dict(
        client_order_id="c",
        strategy="s",
        contract_symbol=_PLAN_SYMS[0],
        intent=T.OrderIntent.STO,
        quantity=1,
        limit_price=Decimal("0.9"),
        strike=Decimal("80"),
        expiration=EXP,
        basis=BASIS,
        created_at=NOW,
        long_contract_symbol=None,
        long_strike=None,
        time_in_force="gfd",
    )
    base.update(kw)
    p = object.__new__(T.OrderPlan)
    for k, v in base.items():
        object.__setattr__(p, k, v)
    return p


def test_post_init_matches_old_over_a_grid():
    grid = dict(
        intent=list(T.OrderIntent),
        time_in_force=["gfd", "gtc", "ioc"],
        quantity=[0, 1],
        limit_price=[Decimal("0"), Decimal("0.9"), Decimal("9")],
        strike=[Decimal("0"), Decimal("80")],
        contract_symbol=_PLAN_SYMS[:4],
        long_contract_symbol=[None, "", *_PLAN_SYMS],
        long_strike=[None, Decimal("0"), Decimal("72"), Decimal("80"), Decimal("85")],
        expiration=[EXP, date(2026, 10, 23)],
    )
    keys = list(grid)
    rng = random.Random(7)
    n = 0
    for combo in itertools.product(*grid.values()):
        if rng.random() > 0.05:
            continue
        p = _raw_plan(**dict(zip(keys, combo)))
        assert _run(T.OrderPlan.__post_init__, p) == _run(OLD_POST_INIT, p), dict(zip(keys, combo))
        n += 1
    assert n > 500


def test_post_init_accepts_a_valid_spread_and_single_leg():
    for p in (
        _raw_plan(),
        _raw_plan(
            intent=T.OrderIntent.PCS_OPEN,
            long_contract_symbol=_PLAN_SYMS[1],
            long_strike=Decimal("72"),
            limit_price=Decimal("0.94"),
        ),
    ):
        assert _run(T.OrderPlan.__post_init__, p) == ("ok", None) == _run(OLD_POST_INIT, p)


# ── Engine._context ──────────────────────────────────────────────────────────

import asyncio  # noqa: E402

from agentic_guardrails import engine as E  # noqa: E402
from agentic_guardrails.broker import SimBroker  # noqa: E402
from agentic_guardrails.guards import Caps  # noqa: E402
from agentic_guardrails.ledger import InMemoryLedger, OrderRecord  # noqa: E402

OLD_CONTEXT = _exec("    " + F.CONTEXT, E)

_CTX_SYMS = [
    ("O:TQQQ261016P00080000", Decimal("80"), date(2026, 10, 16)),
    ("O:TQQQ261023P00075000", Decimal("75"), date(2026, 10, 23)),
    ("O:QLD261016P00060000", Decimal("60"), date(2026, 10, 16)),
    ("O:NVDL261016P00034000", Decimal("34"), date(2026, 10, 16)),
    ("O:SOXL261120P00090000", Decimal("90"), date(2026, 11, 20)),
]
_WING = ("O:TQQQ261016P00072000", Decimal("72"))


def _rand_record(rng, i):
    sym, strike, exp = rng.choice(_CTX_SYMS)
    intent = rng.choice(list(T.OrderIntent))
    qty = rng.randint(1, 4)
    day = rng.choice([date(2026, 9, 16), date(2026, 9, 15)])
    wing = intent in T.SPREAD_INTENTS
    state = rng.choice(list(T.OrderState))
    return OrderRecord(
        client_order_id=f"r{i}",
        strategy=rng.choice(["a", "b"]),
        contract_symbol=sym,
        intent=intent,
        quantity=qty,
        limit_price=Decimal("1.0"),
        strike=strike,
        expiration=exp,
        state=state,
        filled_qty=rng.randint(0, qty),
        avg_fill_price=None,
        broker_order_id=None,
        basis_rule=rng.choice(["sto_bid", T.RESTING_TP_RULE]),
        basis_source="robinhood",
        shadow=rng.random() < 0.2,
        created_at=datetime(day.year, day.month, day.day, 14, 0, tzinfo=timezone.utc),
        updated_at=NOW,
        long_contract_symbol=_WING[0] if wing else None,
        long_strike=_WING[1] if wing else None,
    )


def _rand_plan(rng):
    sym, strike, exp = rng.choice(_CTX_SYMS[:1])
    rule = rng.choice(["sto_bid", T.RESTING_TP_RULE])
    basis = T.PriceBasis(rule, T.QuoteSource.ROBINHOOD, Decimal("1"), Decimal("2"), NOW)
    if rng.random() < 0.4:
        return T.OrderPlan(
            "plan", "a", sym, T.OrderIntent.PCS_OPEN, 1, Decimal("0.9"), strike, exp, basis, NOW,
            long_contract_symbol=_WING[0], long_strike=_WING[1],
        )  # fmt: skip
    return T.OrderPlan("plan", "a", sym, rng.choice([T.OrderIntent.STO, T.OrderIntent.BTC]), 1, Decimal("0.9"), strike, exp, basis, NOW)


def test_engine_context_matches_old_fuzz():
    rng = random.Random(1010)
    caps = Caps()
    for _ in range(300):
        ledger = InMemoryLedger()
        for i in range(rng.randint(0, 9)):
            rec = _rand_record(rng, i)
            ledger.orders[rec.client_order_id] = rec
        broker = SimBroker()
        cfg = E.EngineConfig(caps=caps)
        eng = E.Engine(broker, ledger, cfg, clock=lambda: NOW)
        plan = _rand_plan(rng)
        new = asyncio.run(eng._context(plan))
        old = asyncio.run(OLD_CONTEXT(eng, plan))
        # `factor` (the plan's own factor under the engine's model) postdates the frozen copy; with the
        # default model it must equal the module table the frozen code read, and nothing else may differ.
        assert new.factor == G.factor_of(new.ticker)
        assert dataclasses.replace(new, factor=None) == old
