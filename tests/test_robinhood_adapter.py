"""Offline tests for `brokers.robinhood`: config from env, tool names, the rolled canary, the
read-only client's refusal to rotate the token, and the read-only check end to end over a fake
MCP client that speaks the live enveloped shapes."""

from __future__ import annotations

import json
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from agentic_guardrails.brokers import robinhood as rh
from agentic_guardrails.config import RobinhoodConfig
from agentic_guardrails.mcp_client import MCPAuthError, MCPToolResult, TokenBundle, TokenStore

NOW = datetime(2026, 10, 22, 15, 0, tzinfo=timezone.utc)
CANARY = rh.rolled_canary(rh.DEFAULT_CANARY, NOW.date())  # O:TQQQ261120P00080000 on Oct 22


# ── canary ────────────────────────────────────────────────────────────────────
def test_third_friday():
    assert rh.third_friday(2026, 11) == date(2026, 11, 20)
    assert rh.third_friday(2026, 12) == date(2026, 12, 18)
    assert rh.third_friday(2027, 1) == date(2027, 1, 15)


def test_rolled_canary_keeps_a_far_expiry_and_rolls_a_near_one():
    assert rh.rolled_canary("O:TQQQ261120P00080000", date(2026, 10, 22)) == "O:TQQQ261120P00080000"
    # within 14 days of expiry: roll to the third Friday two months out, same root/type/strike
    assert rh.rolled_canary("O:TQQQ261016P00080000", date(2026, 10, 10)) == "O:TQQQ261218P00080000"
    # already expired (the Oct 16 2026 incident)
    assert rh.rolled_canary("O:TQQQ261016P00080000", date(2026, 10, 19)) == "O:TQQQ261218P00080000"
    # year wrap
    assert rh.rolled_canary("O:QQQ261218C00500000", date(2026, 12, 10)) == "O:QQQ270219C00500000"


def test_rolled_canary_returns_unparseable_input_unchanged():
    assert rh.rolled_canary("TQQQ", date(2026, 10, 22)) == "TQQQ"
    assert rh.rolled_canary("", date(2026, 10, 22)) == ""
    assert rh.rolled_canary("O:TQQQ261399P00080000", date(2026, 10, 22)) == "O:TQQQ261399P00080000"


# ── config ────────────────────────────────────────────────────────────────────
def test_config_from_env_defaults_and_overrides():
    cfg = rh.config_from_env({})
    assert cfg == RobinhoodConfig()
    cfg = rh.config_from_env(
        {
            "AG_RH_MCP_ENDPOINT": "https://example/mcp",
            "AG_RH_TOKEN_PATH": "/tmp/tok.json",
            "AG_RH_ACCOUNT_ID": "AG-9",
            "AG_RH_PROTOCOL_VERSION": "2025-06-18",
            "AG_RH_TIMEOUT_S": "12.5",
        }
    )
    assert cfg.endpoint == "https://example/mcp" and cfg.token_path == "/tmp/tok.json"
    assert cfg.account_id == "AG-9" and cfg.timeout_s == 12.5


def test_config_from_env_rejects_a_bad_timeout():
    with pytest.raises(ValueError):
        rh.config_from_env({"AG_RH_TIMEOUT_S": "soon"})


def test_tools_from_env_overrides_one_verb_only():
    t = rh.tools_from_env({"AG_RH_TOOL_QUOTE": "get_quotes_v2"})
    assert t.quote == "get_quotes_v2" and t.place == rh.DEFAULT_TOOLS.place and t.instruments == "get_option_instruments"
    assert rh.tools_from_env({}) == rh.DEFAULT_TOOLS


def test_build_broker_from_config_uses_account_id_and_expands_token_path(monkeypatch):
    monkeypatch.setenv("HOME", "/home/someone")
    b = rh.build_broker(RobinhoodConfig(account_id="AG-7", token_path="~/tok.json"), read_only=True)
    assert b._account_id == "AG-7"
    assert isinstance(b._c, rh.ReadOnlyMCPClient)
    assert b._c.token_store.path == "/home/someone/tok.json"


# ── read-only client never refreshes ──────────────────────────────────────────
class _Resp:
    def __init__(self, status_code=200, headers=None, json_body=None, text=""):
        self.status_code = status_code
        self.headers = headers or {"content-type": "application/json"}
        self.text = text or (json.dumps(json_body) if json_body is not None else "")

    def json(self):
        return json.loads(self.text)


class _HTTP:
    def __init__(self, responses):
        self._r = list(responses)
        self.token_posts = 0

    async def post(self, url, json=None, headers=None, data=None):
        if data is not None:
            self.token_posts += 1
            raise AssertionError("token refresh must not be attempted in read-only mode")
        return self._r.pop(0)

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_read_only_client_raises_on_401_without_posting_a_refresh():
    http = _HTTP([_Resp(status_code=401, text="expired")])
    store = TokenStore(str(Path(tempfile.mkdtemp()) / "token.json"))
    c = rh.ReadOnlyMCPClient("https://x/mcp", store, client_factory=lambda: http)
    c._token = TokenBundle(access_token="old", refresh_token="ref", token_url="https://x/token", client_id="cid")
    with pytest.raises(MCPAuthError, match="read-only"):
        await c.list_tools()
    assert http.token_posts == 0
    assert not store.path or not Path(store.path).exists()  # nothing written


# ── the read-only check over a fake MCP client ────────────────────────────────
def _ok(data):
    return MCPToolResult(is_error=False, structured={"data": data, "guide": "ignored"}, content=[], raw={})


_INSTR = _ok(
    {
        "instruments": [
            {
                "id": "iid1",
                "strike_price": "80.0000",
                "type": "put",
                "expiration_date": "2026-11-20",
                "chain_symbol": "TQQQ",
                "min_ticks": {"cutoff_price": "3.00", "above_tick": "0.10", "below_tick": "0.01"},
            }
        ]
    }
)


class _FakeMCP:
    def __init__(self, by_tool, tools=None):
        self.by_tool = by_tool
        self.tool_names = tools
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append(name)
        v = self.by_tool[name]
        if isinstance(v, Exception):
            raise v
        return v

    async def discover(self):
        names = self.tool_names if self.tool_names is not None else list(self.by_tool)
        return {"session_id": "s", "tool_count": len(names), "tools": [{"name": n} for n in names]}

    async def aclose(self):
        pass


def _healthy_tools():
    return {
        "get_accounts": _ok({"accounts": [{"account_number": "AG-1", "agentic_allowed": True, "option_level": "option_level_3"}]}),
        "get_portfolio": _ok({"cash": "19000", "buying_power": {"buying_power": "2500"}}),
        "get_option_positions": _ok({"positions": [{"option_id": "iid1", "quantity": "1", "type": "short", "average_price": "1.10"}]}),
        "get_option_instruments": _INSTR,
        "get_option_quotes": _ok({"results": [{"quote": {"instrument_id": "iid1", "bid_price": "1.20", "ask_price": "1.30"}}]}),
        "place_option_order": None,
        "cancel_option_order": None,
        "review_option_order": None,
        "get_option_orders": None,
    }


@pytest.mark.asyncio
async def test_readonly_check_passes_on_a_healthy_surface_and_never_calls_a_trading_tool():
    fake = _FakeMCP(_healthy_tools())
    broker = rh.build_broker(RobinhoodConfig(account_id="AG-1"), client=fake, clock=lambda: NOW)
    r = await rh.readonly_check(broker, now=NOW)
    assert r.ok and r.reasons == ()
    assert r.canary == CANARY and r.tool_count == 9 and r.tools_missing == ()
    assert r.account_ok and r.free_cash == "19000"
    assert r.positions_ok and (r.reconcile_over, r.reconcile_under) == (0, 1)  # the empty ledger under-records the broker short
    assert r.quote_ok and r.quote_bid == "1.20" and r.quote_ask == "1.30" and r.quote_age_s == 0.0
    assert r.min_tick == "0.01"
    assert not {"place_option_order", "cancel_option_order", "review_option_order"} & set(fake.calls)
    assert json.dumps(r.as_dict())  # serialisable for the CLI


@pytest.mark.asyncio
async def test_readonly_check_fails_when_a_configured_tool_is_missing_from_the_live_surface():
    by_tool = _healthy_tools()
    fake = _FakeMCP(by_tool, tools=[n for n in by_tool if n != "place_option_order"])
    broker = rh.build_broker(RobinhoodConfig(account_id="AG-1"), client=fake, clock=lambda: NOW)
    r = await rh.readonly_check(broker, now=NOW)
    assert not r.ok and r.tools_missing == ("place_option_order",)
    assert any("missing from the live surface" in x for x in r.reasons)
    assert r.account_ok and r.quote_ok  # the rest still ran: the output is the whole picture


@pytest.mark.asyncio
async def test_readonly_check_reports_a_failed_read_instead_of_raising():
    by_tool = _healthy_tools()
    by_tool["get_portfolio"] = _ok({"not_cash": "x"})  # malformed balances: fail closed
    fake = _FakeMCP(by_tool)
    broker = rh.build_broker(RobinhoodConfig(account_id="AG-1"), client=fake, clock=lambda: NOW)
    r = await rh.readonly_check(broker, now=NOW)
    assert not r.ok and not r.account_ok and r.free_cash is None
    assert any(e.startswith("account:") for e in r.errors)
    assert r.positions_ok and r.quote_ok


@pytest.mark.asyncio
async def test_readonly_check_skips_discovery_when_asked():
    fake = _FakeMCP(_healthy_tools())
    broker = rh.build_broker(RobinhoodConfig(account_id="AG-1"), client=fake, clock=lambda: NOW)
    r = await rh.readonly_check(broker, now=NOW, discover=False)
    assert r.ok and r.tool_count is None and r.tools_present == ()


@pytest.mark.asyncio
async def test_readonly_check_fails_when_discovery_raises_even_if_every_read_works():
    from agentic_guardrails.mcp_client import MCPProtocolError

    class _NoList(_FakeMCP):
        async def discover(self):
            raise MCPProtocolError("tools/list unavailable")

    fake = _NoList(_healthy_tools())
    broker = rh.build_broker(RobinhoodConfig(account_id="AG-1"), client=fake, clock=lambda: NOW)
    r = await rh.readonly_check(broker, now=NOW)
    assert not r.ok and r.account_ok and r.quote_ok
    assert any("unverified" in x for x in r.reasons) and any(e.startswith("discover:") for e in r.errors)


@pytest.mark.asyncio
async def test_readonly_check_reports_an_unreadable_ledger_instead_of_raising():
    class _BrokenLedger:
        async def all_orders(self):
            raise OSError("disk gone")

    fake = _FakeMCP(_healthy_tools())
    broker = rh.build_broker(RobinhoodConfig(account_id="AG-1"), client=fake, clock=lambda: NOW)
    r = await rh.readonly_check(broker, ledger=_BrokenLedger(), now=NOW)
    assert not r.ok and r.positions_ok and r.quote_ok
    assert any("ledger unreadable" in x for x in r.reasons) and any(e.startswith("ledger: OSError") for e in r.errors)


@pytest.mark.asyncio
async def test_readonly_check_reads_positions_exactly_once():
    fake = _FakeMCP(_healthy_tools())
    broker = rh.build_broker(RobinhoodConfig(account_id="AG-1"), client=fake, clock=lambda: NOW)
    await rh.readonly_check(broker, now=NOW)
    assert fake.calls.count("get_option_positions") == 1
