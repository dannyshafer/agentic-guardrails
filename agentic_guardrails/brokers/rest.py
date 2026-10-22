"""The shared REST transport the venue adapters speak through.

`RestTransport` is a small protocol (one `request` verb returning status, parsed body, headers)
so an adapter is tested offline against a scripted fake and run live over `HttpxTransport`.
The transport maps the TRANSPORT failures onto the executor's taxonomy (a timeout is
`BrokerTimeout`, which after a place is the UNKNOWN class; a connection error is
`BrokerTransient`); the adapter maps the HTTP STATUS with `raise_for`, where the one rule that
matters is: a 4xx on a PLACE is a reject, a 4xx on a read is malformed, and a 401/403 is an
expired session everywhere. Nothing here retries a place.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from agentic_guardrails.broker import BrokerAuthExpired, BrokerMalformed, BrokerRejected, BrokerTimeout, BrokerTransient
from agentic_guardrails.runtime.ratelimit import is_quota_violation_body, retry_after_seconds
from agentic_guardrails.venue_errors import ORDER_LEVEL_REJECTS, classify_reject

logger = logging.getLogger("options_executor.rest")


class BrokerThrottled(BrokerTransient):
    """A rate limit or 5xx with the venue's own retry delay attached, so a caller can sleep the
    right amount without parsing the message. Still a `BrokerTransient` to the engine."""

    def __init__(self, msg: str, retry_after: float) -> None:
        super().__init__(msg)
        self.retry_after = retry_after


@dataclass(frozen=True)
class RestResponse:
    status: int
    body: Any  # parsed JSON, or the raw text when the body is not JSON, or None
    headers: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class RestTransport(Protocol):
    async def request(
        self, method: str, path: str, *, params: dict | None = None, json: Any = None, data: dict | None = None, headers: dict | None = None
    ) -> RestResponse: ...


class StaticBearer:
    """`Authorization: Bearer <token>`; for venues whose token you refresh out of band."""

    def __init__(self, token: str, scheme: str = "Bearer") -> None:
        self._token, self._scheme = token, scheme

    async def __call__(self) -> dict:
        return {"Authorization": f"{self._scheme} {self._token}".strip()}


class StaticHeaders:
    """Fixed headers (API key pairs, a tastytrade session token)."""

    def __init__(self, headers: dict) -> None:
        self._h = dict(headers)

    async def __call__(self) -> dict:
        return dict(self._h)


class HttpxTransport:
    """`RestTransport` over httpx. `auth` is an async callable returning headers, so a refreshing
    OAuth client plugs in without the transport knowing. Bodies are logged on the audit logger
    with headers scrubbed; they hold account figures, route that logger with care."""

    def __init__(self, base_url: str, auth: Callable[[], Awaitable[dict]] | None = None, *, timeout_s: float = 30.0, client=None) -> None:
        import httpx

        self._base = base_url.rstrip("/")
        self._auth = auth
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._httpx = httpx

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(self, method: str, path: str, *, params=None, json=None, data=None, headers=None) -> RestResponse:
        h = dict(headers or {})
        if self._auth is not None:
            h.update(await self._auth())
        url = path if path.startswith("http") else f"{self._base}/{path.lstrip('/')}"
        try:
            r = await self._client.request(method, url, params=params, json=json, data=data, headers=h)
        except self._httpx.TimeoutException as exc:
            raise BrokerTimeout(f"{method} {path}: timeout") from exc
        except self._httpx.TransportError as exc:
            raise BrokerTransient(f"{method} {path}: {exc}") from exc
        body: Any
        try:
            body = r.json() if r.content else None
        except ValueError:
            body = r.text
        logging.getLogger("options_executor.audit").info("%s %s -> %s %s", method, path, r.status_code, _short(body))
        return RestResponse(r.status_code, body, dict(r.headers))


def _short(body) -> str:
    try:
        s = json.dumps(body, default=str)
    except Exception:  # noqa: BLE001
        s = str(body)
    return s if len(s) <= 2000 else s[:2000] + "..."


def raise_for(resp: RestResponse, what: str, *, placing: bool = False) -> None:
    """Map an HTTP status onto the executor's taxonomy. A 4xx on a place is a terminal reject (no
    order rests); on a read it is a malformed request we must not paper over. 401/403 is an
    expired session; 429 and 5xx are transient."""
    if resp.ok:
        return
    if resp.status in (401, 403):
        # One venue's 403 is a REJECT with a reason, not an auth failure: route by the text.
        if resp.status == 403 and placing and classify_reject(_short(resp.body)).cls in ORDER_LEVEL_REJECTS:
            raise BrokerRejected(f"{what}: HTTP {resp.status} {_short(resp.body)}")
        raise BrokerAuthExpired(f"{what}: HTTP {resp.status} {_short(resp.body)}")
    if placing and resp.status >= 500:
        # A 5xx on a PLACE is ambiguous: the venue may have accepted the order before failing.
        # Plain BrokerTransient, no retry hint: the engine parks it UNKNOWN and looks it up.
        raise BrokerTransient(f"{what}: HTTP {resp.status} {_short(resp.body)} (order may have landed; reconcile, never retry)")
    if resp.status == 429 or resp.status >= 500 or is_quota_violation_body(resp.status, resp.body):
        # A throttled request (or a 5xx on a read) never rested an order; the delay comes from the headers.
        wait = retry_after_seconds(resp.headers)
        raise BrokerThrottled(f"{what}: HTTP {resp.status} {_short(resp.body)} (retry after {wait:.0f}s)", wait)
    if placing:
        raise BrokerRejected(f"{what}: HTTP {resp.status} {_short(resp.body)}")
    raise BrokerMalformed(f"{what}: HTTP {resp.status} {_short(resp.body)}")


def body_dict(resp: RestResponse, what: str) -> dict:
    if not isinstance(resp.body, dict):
        raise BrokerMalformed(f"{what}: expected a JSON object, got {type(resp.body).__name__}")
    return resp.body


def body_list(resp: RestResponse, what: str) -> list:
    if not isinstance(resp.body, list):
        raise BrokerMalformed(f"{what}: expected a JSON list, got {type(resp.body).__name__}")
    if not all(isinstance(x, dict) for x in resp.body):
        raise BrokerMalformed(f"{what}: list has a non-object entry")
    return resp.body
