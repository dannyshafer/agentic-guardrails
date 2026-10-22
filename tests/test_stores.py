"""The file-backed reference stores keep the two rules the dead-man gate and ledger depend on."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agentic_guardrails.health import freshness
from agentic_guardrails.ledger import OrderEvent, OrderRecord
from agentic_guardrails.stores import JsonFileHealthStore, SqliteLedger
from agentic_guardrails.types import OrderIntent, OrderState

NOW = datetime(2026, 10, 22, 14, 0, tzinfo=timezone.utc)


def _rec(coid="sat_a", state=OrderState.PLACED, filled=0, shadow=False, **kw) -> OrderRecord:
    base = dict(
        client_order_id=coid,
        strategy="control",
        contract_symbol="O:TQQQ261120P00080000",
        intent=OrderIntent.STO,
        quantity=1,
        limit_price=Decimal("1.05"),
        strike=Decimal("80"),
        expiration=date(2026, 11, 20),
        state=state,
        filled_qty=filled,
        avg_fill_price=None,
        broker_order_id=None,
        basis_rule="bid",
        basis_source="live",
        shadow=shadow,
        created_at=NOW,
        updated_at=NOW,
    )
    base.update(kw)
    return OrderRecord(**base)


async def test_sqlite_ledger_round_trips_a_record_exactly():
    led = SqliteLedger()
    rec = _rec(avg_fill_price=Decimal("1.05"), long_contract_symbol="O:TQQQ261120P00072000", long_strike=Decimal("72"))
    await led.upsert(rec)
    assert await led.get("sat_a") == rec
    assert await led.get("missing") is None
    assert (await led.all_orders()) == [rec]


async def test_sqlite_ledger_filled_qty_is_a_high_water_mark_and_broker_id_sticks():
    led = SqliteLedger()
    await led.upsert(_rec(state=OrderState.PARTIAL, filled=2, broker_order_id="b1"))
    await led.upsert(_rec(state=OrderState.FILLED, filled=1, broker_order_id=None))
    got = await led.get("sat_a")
    assert got.filled_qty == 2 and got.broker_order_id == "b1" and got.state == OrderState.FILLED


async def test_sqlite_ledger_working_orders_excludes_terminal_and_shadow_rows():
    led = SqliteLedger()
    await led.upsert(_rec("w1", OrderState.PLACED))
    await led.upsert(_rec("w2", OrderState.UNKNOWN))
    await led.upsert(_rec("w3", OrderState.FILLED))
    await led.upsert(_rec("w4", OrderState.PLACED, shadow=True))
    assert sorted(r.client_order_id for r in await led.working_orders()) == ["w1", "w2"]


async def test_sqlite_ledger_events_persist_with_raw_json(tmp_path):
    path = tmp_path / "ledger.db"
    led = SqliteLedger(path)
    ev = OrderEvent("sat_a", OrderState.PLANNED, OrderState.PLACED, 0, None, NOW, raw={"ack": {"id": "b1"}})
    await led.append_event(ev)
    led.close()
    again = SqliteLedger(path)
    assert await again.events("sat_a") == [ev]


async def test_json_health_store_missing_file_reads_as_stale_not_fine(tmp_path):
    store = JsonFileHealthStore(tmp_path / "health.json")
    assert await store.get("selftest") is None
    assert freshness(None).last_ok_at is None


async def test_json_health_store_unreadable_or_malformed_file_reads_as_stale(tmp_path):
    p = tmp_path / "health.json"
    p.write_text("{not json")
    assert await JsonFileHealthStore(p).get("selftest") is None
    p.write_text("[]")
    assert await JsonFileHealthStore(p).get("selftest") is None
    p.write_text('{"selftest": {"last_ok_at": "garbage"}}')
    assert await JsonFileHealthStore(p).get("selftest") is None


async def test_json_health_store_last_ok_is_a_high_water_mark(tmp_path):
    store = JsonFileHealthStore(tmp_path / "h.json")
    await store.record("selftest", ok=True, at=NOW, result="pass")
    await store.record("selftest", ok=False, at=NOW + timedelta(hours=1), result="fail", detail="canary")
    rec = await store.get("selftest")
    assert rec.last_ok_at == NOW and rec.last_run_at == NOW + timedelta(hours=1) and rec.last_result == "fail"
    await store.record("selftest", ok=True, at=NOW - timedelta(days=1), result="pass")
    assert (await store.get("selftest")).last_ok_at == NOW  # a late write never lowers it


async def test_json_health_store_never_passed_reads_none_not_epoch(tmp_path):
    store = JsonFileHealthStore(tmp_path / "h.json")
    await store.record("reconcile", ok=False, at=NOW, result="fail")
    assert (await store.get("reconcile")).last_ok_at is None


async def test_json_health_store_keeps_kinds_independent_and_survives_reopen(tmp_path):
    p = tmp_path / "h.json"
    await JsonFileHealthStore(p).record("a", ok=True, at=NOW, result="pass")
    await JsonFileHealthStore(p).record("b", ok=True, at=NOW, result="pass")
    assert (await JsonFileHealthStore(p).get("a")).last_ok_at == NOW
    assert (await JsonFileHealthStore(p).get("b")).last_ok_at == NOW


@pytest.mark.parametrize("bad", ["", "x"])
def test_json_health_store_accepts_pathlike(tmp_path, bad):
    JsonFileHealthStore(tmp_path / (bad or "h.json"))


async def test_json_health_store_record_overwrites_a_malformed_prior_row(tmp_path):
    p = tmp_path / "h.json"
    p.write_text('{"selftest": {"last_ok_at": "garbage"}, "other": 5}')
    store = JsonFileHealthStore(p)
    assert await store.get("selftest") is None
    await store.record("selftest", ok=True, at=NOW, result="pass")
    assert (await store.get("selftest")).last_ok_at == NOW
    await store.record("other", ok=False, at=NOW, result="fail")
    assert (await store.get("other")).last_ok_at is None
