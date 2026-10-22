"""A user-supplied map from traded symbol to the risk factor it expresses.

`guards.guard_factor_concentration` bounds total open collateral per FACTOR, because a per-ticker
cap cannot see that TQQQ and QLD are two tickers and one bet on the Nasdaq, and the engine's
earnings guard needs to know whether a wrapper's underlier is a single company (scheduled
earnings) or an index (none). The core ships the extracting project's table as module constants
in `guards.py`; this module is the same idea as a value you construct, so a user can describe
their own universe without editing the package.

Deliberately coarse: a lookup, not a covariance model. The failure it prevents is "all of it is
the same trade", which does not need a precise beta to spot. An unknown symbol maps to ITSELF
(its own factor, never pooled into someone else's budget) and is treated as a single name for
earnings purposes (refuse an unfamiliar ticker rather than sell through an event you could not
see). Both defaults are the conservative reading.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from agentic_guardrails import guards


@dataclass(frozen=True)
class FactorMap:
    """`symbol_to_factor` groups collateral for the concentration cap. `single_name_factors` names
    the factors that are companies (a missing earnings date is then a refusal, not "no event").
    `earnings_underliers` names the company whose calendar governs a wrapper when that is NOT its
    factor: pool NVDL under a custom "TECH" group and you must still say `{"NVDL": "NVDA"}`, or the
    engine asks the provider about TECH, reads None as "no event", and sells through NVDA's report.
    A wrapper whose factor is a single name needs no entry (the factor is the company)."""

    symbol_to_factor: Mapping[str, str] = field(default_factory=dict)
    single_name_factors: frozenset[str] = frozenset()
    earnings_underliers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol_to_factor", MappingProxyType({k.upper(): v.upper() for k, v in self.symbol_to_factor.items()}))
        object.__setattr__(self, "single_name_factors", frozenset(f.upper() for f in self.single_name_factors))
        object.__setattr__(
            self, "earnings_underliers", MappingProxyType({k.upper(): v.upper() for k, v in self.earnings_underliers.items()})
        )

    def factor_of(self, ticker: str) -> str:
        """The risk factor a symbol expresses. Unknown symbols are their own factor."""
        t = ticker.upper()
        return self.symbol_to_factor.get(t, t)

    def earnings_underlier(self, ticker: str) -> str:
        """The symbol to ask the earnings provider about: the explicit entry, else the factor when
        it is a company, else the ticker itself (an index or basket has no company to ask)."""
        t = ticker.upper()
        explicit = self.earnings_underliers.get(t)
        if explicit is not None:
            return explicit
        f = self.symbol_to_factor.get(t)
        return f if f is not None and f in self.single_name_factors else t

    def underlier_has_earnings(self, ticker: str) -> bool:
        """Whether a MISSING earnings lookup for this symbol is a fail-closed refusal (single
        name, an explicit earnings underlier, or unknown) or benign (index or basket factor)."""
        t = ticker.upper()
        if t in self.earnings_underliers:
            return True
        f = self.symbol_to_factor.get(t)
        return True if f is None else f in self.single_name_factors

    def factors(self) -> frozenset[str]:
        return frozenset(self.symbol_to_factor.values())


# The extracting project's map, as it stood at extraction: Nasdaq-100, S&P 500 and Russell
# wrappers, five single-name wrappers factored to their underliers, and a crude oil fund. Use it
# as a starting point, not a recommendation; your universe is yours to describe.
POLYSENTINEL_FACTORS = FactorMap(symbol_to_factor=dict(guards.FACTOR_OF), single_name_factors=guards.SINGLE_NAME_FACTORS)
