"""OCC symbol forms. The core keys everything by the compact prefixed form `O:TQQQ261120P00080000`;
venues differ: Schwab and tastytrade use the 21-character padded form (`TQQQ  261120P00080000`,
root left-justified to six), Tradier and Alpaca the compact form without the prefix, IBKR its
own contract ids. These are the only two translations, both pure and reversible."""

from __future__ import annotations

import re

from agentic_guardrails.broker import BrokerMalformed

_TAIL = re.compile(r"^([A-Z][A-Z0-9]{0,5})\s*(\d{6})([CP])(\d{8})$")


def core_occ(venue_symbol: str) -> str:
    """Padded or compact venue symbol -> `O:ROOTYYMMDDCSSSSSSSS`. Fails closed on anything else."""
    s = venue_symbol.strip().upper().removeprefix("O:")
    m = _TAIL.match(s)
    if not m:
        raise BrokerMalformed(f"not an OCC option symbol: {venue_symbol!r}")
    root, ymd, cp, strike = m.groups()
    return f"O:{root}{ymd}{cp}{strike}"


def compact(contract_symbol: str) -> str:
    """`O:TQQQ261120P00080000` -> `TQQQ261120P00080000`."""
    return core_occ(contract_symbol)[2:]


def padded(contract_symbol: str) -> str:
    """`O:TQQQ261120P00080000` -> `TQQQ  261120P00080000` (root padded to six)."""
    c = compact(contract_symbol)
    root, tail = c[:-15], c[-15:]
    return f"{root:<6}{tail}"
