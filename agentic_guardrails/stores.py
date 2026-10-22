"""File-backed reference stores: a SQLite order ledger and a JSON-file health store.

The core defines two storage protocols (`ledger.LedgerStore`, `health.HealthStore`) and ships
in-memory implementations for tests and shadow mode. These are the smallest durable ones. They
exist so a user can run the engine for real without writing a database layer first; a Postgres
user writes their own against the same protocols (the extracting project's SQLAlchemy stores are
the worked example in the README).

Two rules carried over from the production stores, because the dead-man gate depends on them:

- A health store that cannot be READ returns None, which the gate reads as stale and halts. It
  never raises into the engine, and it never invents a record.
- `last_ok_at` is a high-water mark. A failing run advances `last_run_at` only; an out-of-order
  write can never move a good timestamp backwards. The same discipline applies to the ledger's
  `filled_qty` on upsert.

Both stores are synchronous underneath (sqlite3, a small JSON file) behind the async protocol;
the engine's call volume is a few writes per order, so this is fine for one process. They are
not safe for two processes writing the same file.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from agentic_guardrails.health import HealthRecord
from agentic_guardrails.ledger import OrderEvent, OrderRecord
from agentic_guardrails.types import OrderIntent, OrderState

logger = logging.getLogger(__name__)

_WORKING = tuple(s.value for s in (OrderState.PLACED, OrderState.PARTIAL, OrderState.UNKNOWN))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    strategy TEXT NOT NULL,
    contract_symbol TEXT NOT NULL,
    intent TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    limit_price TEXT NOT NULL,
    strike TEXT NOT NULL,
    expiration TEXT NOT NULL,
    state TEXT NOT NULL,
    filled_qty INTEGER NOT NULL,
    avg_fill_price TEXT,
    broker_order_id TEXT,
    basis_rule TEXT NOT NULL,
    basis_source TEXT NOT NULL,
    shadow INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    long_contract_symbol TEXT,
    long_strike TEXT
);
CREATE TABLE IF NOT EXISTS order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    filled_qty INTEGER NOT NULL,
    avg_fill_price TEXT,
    at TEXT NOT NULL,
    raw TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS order_events_coid ON order_events (client_order_id);
"""


def _dec(s: str | None) -> Decimal | None:
    return Decimal(s) if s is not None else None


def _dt(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


class SqliteLedger:
    """`LedgerStore` over one SQLite file (":memory:" works for tests).

    Decimals are stored as their string form so no precision is lost through float; the
    columns mirror the extracting project's `options_live_orders` so a user can migrate
    between the two with a row-for-row copy."""

    def __init__(self, path: str | os.PathLike[str] = ":memory:") -> None:
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _row_to_rec(r: sqlite3.Row) -> OrderRecord:
        return OrderRecord(
            client_order_id=r["client_order_id"],
            strategy=r["strategy"],
            contract_symbol=r["contract_symbol"],
            intent=OrderIntent(r["intent"]),
            quantity=int(r["quantity"]),
            limit_price=Decimal(r["limit_price"]),
            strike=Decimal(r["strike"]),
            expiration=date.fromisoformat(r["expiration"]),
            state=OrderState(r["state"]),
            filled_qty=int(r["filled_qty"]),
            avg_fill_price=_dec(r["avg_fill_price"]),
            broker_order_id=r["broker_order_id"],
            basis_rule=r["basis_rule"],
            basis_source=r["basis_source"],
            shadow=bool(r["shadow"]),
            created_at=_dt(r["created_at"]),
            updated_at=_dt(r["updated_at"]),
            long_contract_symbol=r["long_contract_symbol"],
            long_strike=_dec(r["long_strike"]),
        )

    async def get(self, client_order_id: str) -> OrderRecord | None:
        r = self._conn.execute("SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)).fetchone()
        return self._row_to_rec(r) if r else None

    async def upsert(self, rec: OrderRecord) -> None:
        self._conn.execute(
            """INSERT INTO orders (client_order_id, strategy, contract_symbol, intent, quantity, limit_price, strike,
                 expiration, state, filled_qty, avg_fill_price, broker_order_id, basis_rule, basis_source, shadow,
                 created_at, updated_at, long_contract_symbol, long_strike)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (client_order_id) DO UPDATE SET
                 state = excluded.state,
                 filled_qty = MAX(orders.filled_qty, excluded.filled_qty),
                 avg_fill_price = excluded.avg_fill_price,
                 broker_order_id = COALESCE(excluded.broker_order_id, orders.broker_order_id),
                 updated_at = excluded.updated_at""",
            (
                rec.client_order_id,
                rec.strategy,
                rec.contract_symbol,
                rec.intent.value,
                rec.quantity,
                str(rec.limit_price),
                str(rec.strike),
                rec.expiration.isoformat(),
                rec.state.value,
                rec.filled_qty,
                str(rec.avg_fill_price) if rec.avg_fill_price is not None else None,
                rec.broker_order_id,
                rec.basis_rule,
                rec.basis_source,
                int(rec.shadow),
                rec.created_at.isoformat(),
                rec.updated_at.isoformat(),
                rec.long_contract_symbol,
                str(rec.long_strike) if rec.long_strike is not None else None,
            ),
        )
        self._conn.commit()

    async def append_event(self, ev: OrderEvent) -> None:
        self._conn.execute(
            "INSERT INTO order_events (client_order_id, from_state, to_state, filled_qty, avg_fill_price, at, raw) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ev.client_order_id,
                ev.from_state.value,
                ev.to_state.value,
                ev.filled_qty,
                str(ev.avg_fill_price) if ev.avg_fill_price is not None else None,
                ev.at.isoformat(),
                json.dumps(ev.raw, default=str),
            ),
        )
        self._conn.commit()

    async def all_orders(self) -> list[OrderRecord]:
        return [self._row_to_rec(r) for r in self._conn.execute("SELECT * FROM orders ORDER BY created_at").fetchall()]

    async def working_orders(self) -> list[OrderRecord]:
        q = f"SELECT * FROM orders WHERE state IN ({','.join('?' * len(_WORKING))}) AND shadow = 0 ORDER BY created_at"
        return [self._row_to_rec(r) for r in self._conn.execute(q, _WORKING).fetchall()]

    async def events(self, client_order_id: str) -> list[OrderEvent]:
        rows = self._conn.execute("SELECT * FROM order_events WHERE client_order_id = ? ORDER BY id", (client_order_id,)).fetchall()
        return [
            OrderEvent(
                client_order_id=r["client_order_id"],
                from_state=OrderState(r["from_state"]),
                to_state=OrderState(r["to_state"]),
                filled_qty=int(r["filled_qty"]),
                avg_fill_price=_dec(r["avg_fill_price"]),
                at=_dt(r["at"]),
                raw=json.loads(r["raw"]),
            )
            for r in rows
        ]


class JsonFileHealthStore:
    """`HealthStore` over one JSON file: `{kind: {last_run_at, last_ok_at, last_result, detail}}`.

    Writes are atomic (temp file + rename) so a crash mid-write leaves the previous file, never
    a truncated one. A file that is missing, unreadable or malformed reads as NO record for every
    kind, which the dead-man gate treats as stale: the store fails toward halting, like the
    production Postgres store it mirrors."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)

    def _load(self) -> dict | None:
        try:
            if not self._path.exists():
                return {}
            data = json.loads(self._path.read_text())
            return data if isinstance(data, dict) else None
        except Exception as exc:  # noqa: BLE001 - unreadable health must halt, not crash
            logger.warning("health store: read failed for %s, treating every kind as stale: %s", self._path, exc)
            return None

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=self._path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            finally:
                raise

    async def get(self, kind: str) -> HealthRecord | None:
        data = self._load()
        if not data or kind not in data:
            return None
        row = data[kind]
        try:
            return HealthRecord(
                kind=kind,
                last_run_at=_dt(row["last_run_at"]) if row.get("last_run_at") else None,
                last_ok_at=_dt(row["last_ok_at"]) if row.get("last_ok_at") else None,
                last_result=row.get("last_result"),
                detail=row.get("detail"),
            )
        except Exception as exc:  # noqa: BLE001 - a malformed row is stale, not a crash
            logger.warning("health store: malformed row for %s, treating as stale: %s", kind, exc)
            return None

    async def record(self, kind: str, *, ok: bool, at: datetime, result: str, detail: str | None = None) -> None:
        data = self._load() or {}
        prev = data.get(kind) if isinstance(data.get(kind), dict) else {}
        try:
            prev_ok = _dt(prev["last_ok_at"]) if prev.get("last_ok_at") else None
        except Exception:  # noqa: BLE001 - a malformed prior row must not block the next write
            logger.warning("health store: malformed last_ok_at for %s, overwriting it", kind)
            prev_ok = None
        # High-water mark: a failure never advances last_ok_at, and a late write never lowers it.
        last_ok = at if ok and (prev_ok is None or at > prev_ok) else prev_ok
        data[kind] = {
            "last_run_at": at.isoformat(),
            "last_ok_at": last_ok.isoformat() if last_ok else None,
            "last_result": result,
            "detail": detail,
        }
        self._save(data)
