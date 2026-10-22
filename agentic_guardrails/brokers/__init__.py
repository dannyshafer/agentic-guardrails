"""Broker adapters: every one speaks `broker.BrokerAdapter` and maps its venue's errors onto the
executor's taxonomy, so the engine's fail-closed branches hold regardless of venue.

- `SimBroker` (in `broker`): fault injection for tests.
- `DryRunBroker`: records what it would send, fills by policy; the paper-trading adapter.
- `RobinhoodMCPBroker` (in `broker`): the one that has run real money. `brokers.robinhood` assembles
  it from `RobinhoodConfig` or the `AG_RH_*` environment and ships the read-only check
  (`python -m agentic_guardrails.brokers.robinhood`). It is imported on demand, not here, because
  it needs httpx and because `-m` on a module its package imports warns.
- `SchwabBroker`, `TradierBroker`, `TastytradeBroker`, `IBKRBroker`, `AlpacaBroker`: REST adapters
  written against each venue's published API over the shared `rest.RestTransport`. They are
  tested offline against recorded shapes and have NOT yet placed a live order; the first live
  order through each is its acceptance test, so keep it probe-sized.

Fidelity has no public trading API and cannot be supported.
"""

from agentic_guardrails.brokers.alpaca import AlpacaBroker
from agentic_guardrails.brokers.dryrun import DryRunBroker
from agentic_guardrails.brokers.ibkr import IBKRBroker
from agentic_guardrails.brokers.rest import HttpxTransport, RestTransport, StaticBearer
from agentic_guardrails.brokers.schwab import SchwabBroker
from agentic_guardrails.brokers.tastytrade import TastytradeBroker
from agentic_guardrails.brokers.tradier import TradierBroker

__all__ = [
    "AlpacaBroker",
    "DryRunBroker",
    "HttpxTransport",
    "IBKRBroker",
    "RestTransport",
    "SchwabBroker",
    "StaticBearer",
    "TastytradeBroker",
    "TradierBroker",
]
