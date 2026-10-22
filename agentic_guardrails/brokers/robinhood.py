"""The Robinhood Agentic MCP adapter, assembled from config.

The adapter itself (`broker.RobinhoodMCPBroker`) and the transport (`mcp_client`) are the
modules that have run real money. This module is the glue a user needs
around them: tool names, construction from `RobinhoodConfig` or the `AG_RH_*` environment, a
canary contract that never expires under you, and a READ-ONLY check that proves auth, account,
positions and a quote parse before any order is ever placeable.

Nothing here places, cancels or reviews. The read-only check also never refreshes the OAuth
token: a second process rotating the refresh token under a running executor is how a live
bot loses its session, so `ReadOnlyMCPClient` raises instead.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from agentic_guardrails.broker import BrokerError, RhToolNames, RobinhoodMCPBroker
from agentic_guardrails.config import RobinhoodConfig
from agentic_guardrails.ledger import InMemoryLedger, LedgerStore
from agentic_guardrails.mcp_client import MCPAuthError, MCPError, RobinhoodMCPClient, TokenStore
from agentic_guardrails.selftest import evaluate, run_probe

# The live tool surface, verified against the MCP's own schema. Every response is
# enveloped `{data, guide}`; balances live on get_portfolio, not the account object; quotes take
# instrument ids, which get_option_instruments resolves from an OCC symbol.
DEFAULT_TOOLS = RhToolNames(
    quote="get_option_quotes",
    place="place_option_order",
    cancel="cancel_option_order",
    review="review_option_order",
    order_status="get_option_orders",
    positions="get_option_positions",
    account="get_accounts",
    portfolio="get_portfolio",
    instruments="get_option_instruments",
)

ENV_PREFIX = "AG_RH_"

# The default canary: a liquid, always-listed TQQQ $80 put. Only the ROOT, type and strike
# matter; `rolled_canary` keeps the expiry ahead of today. Never pin a dated contract (the
# original died on its expiry and the dead-man gate halted every open twenty hours later).
DEFAULT_CANARY = "O:TQQQ261120P00080000"

_OCC = re.compile(r"O:([A-Z][A-Z0-9]*)(\d{2})(\d{2})(\d{2})([CP])(\d{8})")


def third_friday(year: int, month: int) -> date:
    """The standard monthly expiration date (third Friday). Exchange holidays are not
    adjusted here; a Good Friday expiry is listed on the Thursday and still quotes."""
    first = date(year, month, 1)
    return first + timedelta(days=(4 - first.weekday()) % 7 + 14)


def rolled_canary(configured: str, today: date, *, min_days_ahead: int = 14) -> str:
    """`configured` with its expiry rolled to the standard monthly two months out once it is
    within `min_days_ahead` of expiry. Unparseable input is returned unchanged so a bad
    config fails the quote loudly rather than silently substituting a contract."""
    m = _OCC.fullmatch(configured or "")
    if not m:
        return configured
    root, yy, mm, dd, kind, strike = m.groups()
    try:
        exp = date(2000 + int(yy), int(mm), int(dd))
    except ValueError:
        return configured
    if exp > today + timedelta(days=min_days_ahead):
        return configured
    month = today.month + 2
    year = today.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    return f"O:{root}{third_friday(year, month):%y%m%d}{kind}{strike}"


def config_from_env(environ: dict[str, str] | None = None, *, prefix: str = ENV_PREFIX) -> RobinhoodConfig:
    """`RobinhoodConfig` from `AG_RH_MCP_ENDPOINT`, `AG_RH_TOKEN_PATH`, `AG_RH_ACCOUNT_ID`,
    `AG_RH_PROTOCOL_VERSION`, `AG_RH_TIMEOUT_S`; unset variables keep the dataclass default."""
    env = os.environ if environ is None else environ
    base = RobinhoodConfig()
    timeout_raw = env.get(f"{prefix}TIMEOUT_S")
    try:
        timeout = float(timeout_raw) if timeout_raw else base.timeout_s
    except ValueError as exc:
        raise ValueError(f"{prefix}TIMEOUT_S must be a number, got {timeout_raw!r}") from exc
    return RobinhoodConfig(
        endpoint=env.get(f"{prefix}MCP_ENDPOINT") or base.endpoint,
        token_path=env.get(f"{prefix}TOKEN_PATH") or base.token_path,
        account_id=env.get(f"{prefix}ACCOUNT_ID") or base.account_id,
        protocol_version=env.get(f"{prefix}PROTOCOL_VERSION") or base.protocol_version,
        timeout_s=timeout,
    )


def tools_from_env(environ: dict[str, str] | None = None, *, prefix: str = ENV_PREFIX) -> RhToolNames:
    """`DEFAULT_TOOLS` with any `AG_RH_TOOL_<VERB>` override applied (QUOTE, PLACE, CANCEL,
    REVIEW, ORDER_STATUS, POSITIONS, ACCOUNT, PORTFOLIO, INSTRUMENTS)."""
    env = os.environ if environ is None else environ
    kw = {}
    for f in RhToolNames.__dataclass_fields__:
        v = env.get(f"{prefix}TOOL_{f.upper()}")
        kw[f] = v or getattr(DEFAULT_TOOLS, f)
    return RhToolNames(**kw)


class ReadOnlyMCPClient(RobinhoodMCPClient):
    """A client that will not rotate the OAuth token. The access token is used as found; a
    401 raises `MCPAuthError` instead of refreshing, so a check run beside a live executor
    can never invalidate the executor's refresh token."""

    async def _refresh_or_raise(self, token):  # noqa: ARG002
        raise MCPAuthError("access token rejected (401) and refresh is disabled in read-only mode; let the owning process refresh it")


def build_client(cfg: RobinhoodConfig, *, read_only: bool = False, **kw: Any) -> RobinhoodMCPClient:
    """The MCP transport for `cfg`. `read_only=True` disables token refresh (see `ReadOnlyMCPClient`).
    Extra keyword arguments (`client_factory`, `audit`) pass through to the client."""
    cls = ReadOnlyMCPClient if read_only else RobinhoodMCPClient
    return cls(
        cfg.endpoint,
        TokenStore(os.path.expanduser(cfg.token_path)),
        protocol_version=cfg.protocol_version,
        timeout_s=cfg.timeout_s,
        **kw,
    )


def build_broker(
    cfg: RobinhoodConfig,
    *,
    tools: RhToolNames = DEFAULT_TOOLS,
    client: Any | None = None,
    read_only: bool = False,
    clock=None,
    **client_kw: Any,
) -> RobinhoodMCPBroker:
    """A `RobinhoodMCPBroker` over `cfg`. Pass `client` to supply your own transport (tests);
    otherwise one is built from the config. The broker is a singleton's worth of state (an MCP
    session id, a refreshed token, instrument caches): build it once per process."""
    c = client if client is not None else build_client(cfg, read_only=read_only, **client_kw)
    return RobinhoodMCPBroker(c, tools, account_id=cfg.account_id or "", clock=clock)


@dataclass(frozen=True)
class ReadOnlyCheck:
    """What the read-only check proved. `ok` is the self-test verdict (`selftest.evaluate`),
    which is what the dead-man gate would record; everything else is for the human."""

    ok: bool
    reasons: tuple[str, ...]
    tool_count: int | None
    tools_present: tuple[str, ...]
    tools_missing: tuple[str, ...]
    account_ok: bool
    free_cash: str | None
    positions_ok: bool
    reconcile_over: int
    reconcile_under: int
    canary: str
    quote_ok: bool
    quote_bid: str | None
    quote_ask: str | None
    quote_age_s: float | None
    min_tick: str | None
    errors: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


async def readonly_check(
    broker: RobinhoodMCPBroker,
    *,
    ledger: LedgerStore | None = None,
    canary: str = DEFAULT_CANARY,
    now: datetime | None = None,
    discover: bool = True,
) -> ReadOnlyCheck:
    """Prove the adapter end to end without trading: list the tool surface and confirm every
    configured tool name is on it, read the account, read and reconcile positions against
    `ledger`, quote the (rolled) canary and read its tick size, then score it exactly as the
    self-test would. A failed discovery or an unreadable ledger fails the check (`discover=False`
    is the explicit opt-out). Any failure is reported, never raised, so the output is the whole
    picture in one run; positions are read once, by the probe."""
    now = now or datetime.now(timezone.utc)
    canary = rolled_canary(canary, now.date())
    errors: list[str] = []
    blocking: list[str] = []
    tool_count = None
    present: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    client = broker._c  # noqa: SLF001 (the transport is the adapter's own)
    if discover and hasattr(client, "discover"):
        try:
            info = await client.discover()
            names = {t.get("name") for t in info.get("tools", [])}
            tool_count = info.get("tool_count")
            wanted = [getattr(broker._tools, f) for f in RhToolNames.__dataclass_fields__]  # noqa: SLF001
            present = tuple(n for n in wanted if n in names)
            missing = tuple(n for n in wanted if n not in names)
            if missing:
                blocking.append("configured tool names missing from the live surface: " + ", ".join(missing))
        except MCPError as exc:
            errors.append(f"discover: {type(exc).__name__}: {exc}")
            blocking.append("tool surface could not be listed, so the configured tool names are unverified")
    # The probe reconciles the ledger against the broker's positions; a ledger that cannot be
    # read is itself a failed check (the dead-man gate would halt on it), not an exception.
    probe_ledger: LedgerStore = ledger or InMemoryLedger()
    try:
        await probe_ledger.all_orders()
    except Exception as exc:  # noqa: BLE001 (any store failure is the same finding)
        errors.append(f"ledger: {type(exc).__name__}: {exc}")
        blocking.append("ledger unreadable, so positions could not be reconciled")
        probe_ledger = InMemoryLedger()
    probe = await run_probe(broker, probe_ledger, canary, now)
    verdict = evaluate(probe)
    tick = None
    if probe.quote_ok and probe.quote_ask is not None:
        try:
            tick = str(await broker.min_tick(canary, probe.quote_ask))
        except BrokerError as exc:
            errors.append(f"min_tick: {exc}")
    return ReadOnlyCheck(
        ok=verdict.passed and not blocking,
        reasons=tuple(verdict.reasons) + tuple(blocking),
        tool_count=tool_count,
        tools_present=present,
        tools_missing=missing,
        account_ok=probe.account_ok,
        free_cash=None if probe.free_cash is None else str(probe.free_cash),
        positions_ok=probe.positions_ok,
        reconcile_over=probe.reconcile_over,
        reconcile_under=probe.reconcile_under,
        canary=canary,
        quote_ok=probe.quote_ok,
        quote_bid=None if probe.quote_bid is None else str(probe.quote_bid),
        quote_ask=None if probe.quote_ask is None else str(probe.quote_ask),
        quote_age_s=probe.quote_age_s,
        min_tick=tick,
        errors=tuple(probe.errors) + tuple(errors),
    )


async def _main(argv: list[str]) -> int:
    """`python -m agentic_guardrails.brokers.robinhood [--canary O:...] [--allow-refresh]`.
    Read-only. Exit 0 when the self-test verdict is a pass, 1 otherwise, 2 when unconfigured."""
    canary = DEFAULT_CANARY
    allow_refresh = False
    it = iter(argv)
    for a in it:
        if a == "--canary":
            canary = next(it, canary)
        elif a == "--allow-refresh":
            allow_refresh = True
        else:
            print(f"unknown argument {a!r}")
            return 2
    cfg = config_from_env()
    if not os.path.exists(os.path.expanduser(cfg.token_path)):
        print(f"no token file at {cfg.token_path!r}; set {ENV_PREFIX}TOKEN_PATH to the exported desktop token (see README).")
        return 2
    broker = build_broker(cfg, tools=tools_from_env(), read_only=not allow_refresh)
    try:
        result = await readonly_check(broker, canary=canary)
    finally:
        await broker.aclose()
    print(json.dumps(result.as_dict(), indent=2, default=str))
    return 0 if result.ok else 1


if __name__ == "__main__":
    import sys

    sys.exit(asyncio.run(_main(sys.argv[1:])))
