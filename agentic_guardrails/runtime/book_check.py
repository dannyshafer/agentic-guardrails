"""Whole-book sanity reads against the broker's positions, pure.

The assignment stories share a shape: the bot's model of the book diverged from the broker's and
nothing said so until an order went wrong. Assignment arrives as a position change the next
morning, never as an order event the bot placed; a venue marks an assigned equity non-tradable
and the bot's close goes nowhere; a bot "forgot" positions it still held. The reconciler already
classifies per-contract drift (OVER/UNDER/ASSIGNMENT/ADJUSTED). This adds the reads that are
about the book, not a contract: unexpected equity in an options-only account, option positions
with no ledger lot, and a non-tradable flag on anything held.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class BookIssue(str, Enum):
    UNEXPECTED_EQUITY = "unexpected_equity"
    ORPHAN_OPTION = "orphan_option"
    NON_TRADABLE = "non_tradable"


@dataclass(frozen=True)
class BookFinding:
    issue: BookIssue
    symbol: str
    quantity: int
    detail: str

    @property
    def blocks_opens_on(self) -> str:
        """The underlying ticker whose opens should halt until a human acknowledges."""
        return _root(self.symbol)


def _root(symbol: str) -> str:
    s = symbol[2:] if symbol.startswith("O:") else symbol
    return "".join(ch for ch in s[:6] if not ch.isdigit()).strip() or s


def check_book(
    broker_equity: dict[str, int],
    broker_options: dict[str, int],
    ledger_open_options: set[str],
    non_tradable: set[str] = frozenset(),
) -> list[BookFinding]:
    """`broker_equity`: shares by ticker (signed). `broker_options`: contracts by OCC symbol (signed).
    `ledger_open_options`: OCC symbols the ledger believes are open. `non_tradable`: symbols the
    broker flags as restricted."""
    out: list[BookFinding] = []
    for t, q in sorted(broker_equity.items()):
        if q != 0:
            out.append(
                BookFinding(
                    BookIssue.UNEXPECTED_EQUITY,
                    t,
                    q,
                    f"{q:+d} shares of {t} in an options-only book: assignment or exercise landed; no new shorts on {t} until acknowledged",
                )
            )
    for sym, q in sorted(broker_options.items()):
        if q != 0 and sym not in ledger_open_options:
            out.append(
                BookFinding(
                    BookIssue.ORPHAN_OPTION,
                    sym,
                    q,
                    f"broker holds {q:+d} of {sym} with no open ledger lot: a manual trade, a lost fill, or a restart that dropped state",
                )
            )
    for sym in sorted(non_tradable):
        q = broker_options.get(sym, broker_equity.get(sym, 0))
        if q:
            out.append(
                BookFinding(
                    BookIssue.NON_TRADABLE,
                    sym,
                    q,
                    f"{sym} is flagged non-tradable while held ({q:+d}); a close will not route, escalate to a human",
                )
            )
    return out


def opens_blocked_tickers(findings: list[BookFinding]) -> set[str]:
    return {f.blocks_opens_on for f in findings}
