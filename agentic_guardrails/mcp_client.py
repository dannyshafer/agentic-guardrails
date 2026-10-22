"""Robinhood Agentic Trading MCP transport (docs/ROBINHOOD_MCP_EXECUTION_PLAN.md 3.1).

The thin network layer under `RobinhoodMCPBroker`. It speaks streamable-HTTP MCP
(JSON-RPC 2.0) to agent.robinhood.com and owns exactly three things: the OAuth
bearer token (load + refresh), the MCP session handshake, and the request/response
audit trail. It contains NO business logic: mapping tool results to order state,
normalizing to Decimal/OCC, and every trading decision live in the broker and the
engine above it.

Design constraints, each learned at cost elsewhere in this codebase:

- FAIL CLOSED, LOUDLY. A 401 tries exactly one token refresh, then raises
  `MCPAuthError` (never a blind retry, never a silent skip). A malformed or
  error JSON-RPC response raises `MCPProtocolError`. The broker maps these onto
  the executor's own error taxonomy so the engine halts rather than guesses.
- READS may retry; nothing here retries a state-changing call. The retry policy
  lives at the broker/engine layer (placements are status-checked, never
  blind-retried), so this transport does ONE attempt per `call_tool`, plus the
  single auth refresh.
- SECRETS NEVER HIT THE LOG. The audit sink receives a scrubbed record (the
  Authorization header and the token body are redacted); the bearer value is
  only ever in memory and on the token file (0600).
- TOOL NAMES/SCHEMAS ARE UNVERIFIED until Phase 0. This module does not hardcode
  any tool name; it calls whatever the broker passes. The `discover()` helper and
  the `__main__` CLI are how a session on the prod box confirms the live surface
  (open questions #2-#6) once the token is in place.

This module is import-safe: importing it starts no I/O and opens no socket. The
HTTP client is created lazily on first use and injected in tests.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

logger = logging.getLogger("options_executor.mcp")
audit_logger = logging.getLogger("options_executor.audit")


# ── Error taxonomy (mapped onto broker errors by RobinhoodMCPBroker) ──────────
class MCPError(Exception):
    """Base for every transport failure."""


class MCPAuthError(MCPError):
    """Auth is invalid and could not be refreshed. Halt and re-auth (never retry blind)."""


class MCPTransportError(MCPError):
    """A network-level blip (timeout, connection reset, 5xx). Reads may retry above."""


class MCPTimeout(MCPTransportError):
    """The request did not return in time. After a PLACE this is the UNKNOWN class."""


class MCPProtocolError(MCPError):
    """A response that is not valid JSON-RPC, or a tool error / missing field. Internal-class: halt."""


# ── Token store ───────────────────────────────────────────────────────────────
@dataclass
class TokenBundle:
    """The OAuth material, as exported from the desktop `claude mcp` flow. Only
    `access_token` is required; the rest enable unattended refresh (open
    question #1 — whether the refresh survives weeks headless is a Phase-0
    measurement, not an assumption)."""

    access_token: str
    refresh_token: str | None = None
    expires_at: float | None = None  # unix seconds; None => unknown, treat as not-expiring-proactively
    token_url: str | None = None
    client_id: str | None = None
    client_secret: str | None = None

    def is_expired(self, *, skew_s: float = 60.0, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now if now is not None else time.time()) >= (self.expires_at - skew_s)


class TokenStore:
    """Reads/writes the token JSON at a path. The path is the handoff surface:
    a human exports the desktop OAuth token to it once, and the executor refreshes
    it in place thereafter. A write is atomic (temp + rename) and 0600."""

    def __init__(self, path: str) -> None:
        self.path = path

    def load(self) -> TokenBundle:
        if not self.path or not os.path.exists(self.path):
            raise MCPAuthError(
                f"no Robinhood MCP token file at {self.path!r}; export it from the desktop `claude mcp` flow (see the plan runbook)"
            )
        try:
            with open(self.path, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise MCPAuthError(f"token file at {self.path!r} unreadable: {exc}") from exc
        tok = d.get("access_token")
        if not tok:
            raise MCPAuthError(f"token file at {self.path!r} has no access_token")
        return TokenBundle(
            access_token=tok,
            refresh_token=d.get("refresh_token"),
            expires_at=d.get("expires_at"),
            token_url=d.get("token_url"),
            client_id=d.get("client_id"),
            client_secret=d.get("client_secret"),
        )

    def save(self, bundle: TokenBundle) -> None:
        tmp = f"{self.path}.tmp"
        payload = {
            "access_token": bundle.access_token,
            "refresh_token": bundle.refresh_token,
            "expires_at": bundle.expires_at,
            "token_url": bundle.token_url,
            "client_id": bundle.client_id,
            "client_secret": bundle.client_secret,
        }
        # Create the temp file 0600 FROM THE START (os.open with mode, honored
        # before any bytes land), not chmod-after-write: a permissive umask or a
        # crash between write and chmod would otherwise expose the token for a
        # window (caught in review of PR #476). os.open respects the mode arg
        # regardless of umask on the create.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({k: v for k, v in payload.items() if v is not None}, fh)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        os.replace(tmp, self.path)
        # Belt-and-suspenders in case the destination pre-existed with looser bits.
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass


@dataclass
class MCPToolResult:
    """One tool call's outcome. `structured` is the server's structuredContent when
    present (the machine-readable payload we parse); `content` is the text blocks;
    `is_error` is the MCP tool-level error flag."""

    is_error: bool
    structured: Any
    content: list[dict]
    raw: dict = field(default_factory=dict)


def _scrub(headers: dict) -> dict:
    out = dict(headers)
    if "Authorization" in out:
        out["Authorization"] = "Bearer <redacted>"
    return out


# ── The client ────────────────────────────────────────────────────────────────
class RobinhoodMCPClient:
    """Streamable-HTTP MCP client. One instance per process; not thread-safe (the
    executor is single-loop). Injecting `client_factory` lets tests drive a fake
    httpx client with zero network."""

    def __init__(
        self,
        endpoint: str,
        token_store: TokenStore,
        *,
        protocol_version: str = "2025-06-18",
        timeout_s: float = 30.0,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        audit: Callable[[dict], None] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.token_store = token_store
        self.protocol_version = protocol_version
        self.timeout_s = timeout_s
        self._client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=timeout_s))
        self._client: httpx.AsyncClient | None = None
        self._token: TokenBundle | None = None
        self._session_id: str | None = None
        self._initialized = False
        self._id_seq = 0
        self._audit = audit or self._default_audit

    @staticmethod
    def _default_audit(record: dict) -> None:
        try:
            audit_logger.info("%s", json.dumps(record, default=str))
        except Exception:  # noqa: BLE001 — auditing must never break a trade path
            audit_logger.info("audit record (unserializable): %r", record)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def _bearer(self) -> TokenBundle:
        if self._token is None:
            self._token = self.token_store.load()
        return self._token

    def _next_id(self) -> int:
        self._id_seq += 1
        return self._id_seq

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── low-level JSON-RPC ────────────────────────────────────────────────────
    def _headers(self, token: TokenBundle) -> dict:
        h = {
            "Authorization": f"Bearer {token.access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocol_version,
        }
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    @staticmethod
    def _parse_body(resp: httpx.Response) -> dict:
        """A streamable-HTTP response is either application/json or an SSE stream
        whose `data:` lines carry the JSON-RPC message(s). Return the first
        message that has a `result` or `error` (the response to our request)."""
        ctype = resp.headers.get("content-type", "")
        text = resp.text
        if "text/event-stream" in ctype:
            for line in text.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if not payload:
                    continue
                try:
                    msg = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                    return msg
            raise MCPProtocolError("no JSON-RPC result found in SSE stream")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise MCPProtocolError(f"non-JSON response: {text[:200]!r}") from exc

    async def _post(self, body: dict, *, token: TokenBundle) -> httpx.Response:
        try:
            return await self._http().post(self.endpoint, json=body, headers=self._headers(token))
        except httpx.TimeoutException as exc:
            raise MCPTimeout(f"timeout calling {body.get('method')}") from exc
        except httpx.HTTPError as exc:
            raise MCPTransportError(f"transport error calling {body.get('method')}: {exc}") from exc

    async def _rpc(self, method: str, params: dict | None, *, is_request: bool = True) -> dict | None:
        """One JSON-RPC round trip. On 401 refreshes the token ONCE and retries
        once. Notifications (is_request=False) expect no body."""
        token = self._bearer()
        body: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if is_request:
            body["id"] = self._next_id()

        self._audit(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "dir": "request",
                "method": method,
                "headers": _scrub(self._headers(token)),
                "params": params,
            }
        )
        resp = await self._post(body, token=token)
        if resp.status_code == 401:
            token = await self._refresh_or_raise(token)
            resp = await self._post(body, token=token)
        self._capture_session(resp)
        if resp.status_code == 401:
            raise MCPAuthError("401 after token refresh; re-auth required (desktop flow)")
        if resp.status_code >= 500:
            raise MCPTransportError(f"{method} -> HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise MCPProtocolError(f"{method} -> HTTP {resp.status_code}: {resp.text[:200]!r}")
        if not is_request:
            return None  # a notification: accept 202/200 with no JSON-RPC body
        msg = self._parse_body(resp)
        self._audit({"at": datetime.now(timezone.utc).isoformat(), "dir": "response", "method": method, "body": msg})
        if "error" in msg:
            err = msg["error"] or {}
            raise MCPProtocolError(f"{method} JSON-RPC error {err.get('code')}: {err.get('message')}")
        return msg.get("result")

    def _capture_session(self, resp: httpx.Response) -> None:
        sid = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
        if sid:
            self._session_id = sid

    async def _refresh_or_raise(self, token: TokenBundle) -> TokenBundle:
        if not (token.refresh_token and token.token_url):
            raise MCPAuthError("access token rejected (401) and no refresh_token/token_url to refresh with; re-run the desktop auth")
        data = {
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
        }
        if token.client_id:
            data["client_id"] = token.client_id
        if token.client_secret:
            data["client_secret"] = token.client_secret
        try:
            r = await self._http().post(token.token_url, data=data, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise MCPAuthError(f"token refresh transport error: {exc}") from exc
        if r.status_code != 200:
            raise MCPAuthError(f"token refresh failed HTTP {r.status_code}: {r.text[:200]!r}")
        try:
            payload = r.json()
        except json.JSONDecodeError as exc:
            raise MCPAuthError(f"token refresh returned non-JSON: {r.text[:120]!r}") from exc
        new_access = payload.get("access_token")
        if not new_access:
            raise MCPAuthError("token refresh response had no access_token")
        expires_in = payload.get("expires_in")
        new = TokenBundle(
            access_token=new_access,
            refresh_token=payload.get("refresh_token") or token.refresh_token,
            expires_at=(time.time() + float(expires_in)) if expires_in else None,
            token_url=token.token_url,
            client_id=token.client_id,
            client_secret=token.client_secret,
        )
        self._token = new
        try:
            self.token_store.save(new)
        except Exception as exc:  # noqa: BLE001 — a save failure must not lose the in-memory refreshed token
            logger.warning("refreshed Robinhood token but could not persist it: %s", exc)
        logger.info("Robinhood MCP token refreshed")
        return new

    # ── MCP handshake + tools ─────────────────────────────────────────────────
    async def ensure_initialized(self) -> None:
        if self._initialized:
            return
        result = await self._rpc(
            "initialize",
            {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "polysentinel-saturn-executor", "version": "1"},
            },
        )
        # Some servers hand the session id back in the initialize result; the
        # header path (_capture_session) is the primary one.
        if isinstance(result, dict):
            sid = (result.get("_meta") or {}).get("sessionId")
            if sid and not self._session_id:
                self._session_id = sid
        await self._rpc("notifications/initialized", {}, is_request=False)
        self._initialized = True

    async def list_tools(self) -> list[dict]:
        await self.ensure_initialized()
        result = await self._rpc("tools/list", {})
        if not isinstance(result, dict) or "tools" not in result:
            raise MCPProtocolError(f"tools/list returned no tools: {result!r}")
        return list(result["tools"])

    async def call_tool(self, name: str, arguments: dict) -> MCPToolResult:
        await self.ensure_initialized()
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict):
            raise MCPProtocolError(f"tools/call {name} returned {result!r}")
        return MCPToolResult(
            is_error=bool(result.get("isError", False)),
            structured=result.get("structuredContent"),
            content=list(result.get("content", []) or []),
            raw=result,
        )

    async def discover(self) -> dict:
        """Phase-0 read-only discovery: prove auth works and enumerate the live
        tool surface so open questions #2-#6 can be answered against reality. Does
        NOT place, cancel, or trade anything."""
        tools = await self.list_tools()
        return {
            "session_id": self._session_id,
            "tool_count": len(tools),
            "tools": [{"name": t.get("name"), "description": (t.get("description") or "")[:160]} for t in tools],
        }


async def _cli_discover() -> int:
    """`python -m agentic_guardrails.mcp_client` — run on the PROD box once the
    token file is in place, to confirm connectivity and record the live tool
    surface. Read-only; safe to run any time."""
    import os

    token_path = os.environ.get("AG_RH_TOKEN_PATH", "")
    if not token_path:
        print("AG_RH_TOKEN_PATH is not set; export the desktop token first (see the README runbook).")
        return 2
    client = RobinhoodMCPClient(
        os.environ.get("AG_RH_MCP_ENDPOINT", "https://agent.robinhood.com/mcp/trading"),
        TokenStore(token_path),
        protocol_version=os.environ.get("AG_RH_PROTOCOL_VERSION", "2025-06-18"),
        timeout_s=float(os.environ.get("AG_RH_TIMEOUT_S", "30")),
    )
    try:
        info = await client.discover()
        print(json.dumps(info, indent=2, default=str))
        return 0
    except MCPError as exc:
        print(f"discovery FAILED: {type(exc).__name__}: {exc}")
        return 1
    finally:
        await client.aclose()


if __name__ == "__main__":
    import asyncio
    import sys

    sys.exit(asyncio.run(_cli_discover()))
