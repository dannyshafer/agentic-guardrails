"""agentic-guardrails: let an AI agent trade a brokerage account without it hurting you.

A broker-agnostic execution core for options orders: a deterministic order state
machine, idempotent client order ids, eleven pre-trade guards (limits, caps,
concentration, earnings, spread structure), a dead-man gate that halts opens when
the reconcile or self-test goes stale, a position reconciler that classifies
ledger-vs-broker drift, an assignment guard that keeps a short put from being
assigned, and a fault-injection SimBroker to test all of it offline.

Everything here is stdlib-only and I/O-free except the Robinhood MCP adapter
(`mcp_client`, `broker.RobinhoodMCPBroker`; httpx). Importing the package starts
nothing. Orders are sent only through a `BrokerAdapter` you construct, and only
when `EngineConfig.shadow` is False.

Extracted from PolySentinel's `app/options_executor/` (Oct 2026); see the README
for what each control exists to prevent. The runtime layer (`agentic_guardrails.runtime`)
and the venue adapters (`agentic_guardrails.brokers`) are imported from their packages.
"""

from agentic_guardrails.config import DeadmanWindows, FixedCaps, GuardrailConfig, OpenSanity, ProportionalCaps, RobinhoodConfig
from agentic_guardrails.factors import POLYSENTINEL_FACTORS, FactorMap
from agentic_guardrails.stores import JsonFileHealthStore, SqliteLedger
from agentic_guardrails.broker import BrokerAdapter, BrokerError, BrokerRejected, SimBroker
from agentic_guardrails.engine import Engine, EngineConfig, SubmitResult
from agentic_guardrails.guards import Caps, ExecContext, GuardResult
from agentic_guardrails.health import HealthRecord, HealthStore, InMemoryHealthStore
from agentic_guardrails.ledger import InMemoryLedger, LedgerStore, OrderRecord
from agentic_guardrails.types import OrderIntent, OrderPlan, OrderState, Quote

__all__ = [
    "SqliteLedger",
    "RobinhoodConfig",
    "ProportionalCaps",
    "POLYSENTINEL_FACTORS",
    "OpenSanity",
    "JsonFileHealthStore",
    "GuardrailConfig",
    "FixedCaps",
    "FactorMap",
    "DeadmanWindows",
    "BrokerAdapter",
    "BrokerError",
    "BrokerRejected",
    "Caps",
    "Engine",
    "EngineConfig",
    "ExecContext",
    "GuardResult",
    "HealthRecord",
    "HealthStore",
    "InMemoryHealthStore",
    "InMemoryLedger",
    "LedgerStore",
    "OrderIntent",
    "OrderPlan",
    "OrderRecord",
    "OrderState",
    "Quote",
    "SimBroker",
    "SubmitResult",
]

__version__ = "0.3.0"
