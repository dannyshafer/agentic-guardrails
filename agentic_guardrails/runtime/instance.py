"""Single-instance lock and mode/host consistency, stdlib only.

Two of the duplicate-order stories had nothing to do with retries: an old process that never
stopped after a code edit, and the same bot started on a laptop and a server. Every trade
appears exactly twice in the log. The lock refuses the second process on the SAME machine at
startup; the cross-host case needs a shared lease (see `InstanceLock`).

The other startup check is paper-vs-live consistency: a sandbox token against the production
host returns an opaque 401, a production key with `live=False` sends real orders while the bot
believes it is paper. `mode_mismatch_reason` compares what the config CLAIMS with what the base
URL IS, from a table of known sandbox hosts, and refuses to start on a disagreement.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

SANDBOX_HOST_MARKERS: tuple[str, ...] = (
    "sandbox.tradier.com",
    "api.cert.tastyworks.com",
    "paper-api.alpaca.markets",
)
# IBKR's Client Portal gateway is a LOCAL process for live and paper alike, so a loopback address
# says nothing about mode; its account id does (a DU prefix is paper). Pass `account_id`.


@dataclass
class InstanceLock:
    """`with InstanceLock(path): ...` holds an advisory file lock for the process lifetime.
    A second holder raises `RuntimeError` naming the pid that owns it.

    SCOPE: one filesystem. It stops the stale-process and double-start cases on a single machine.
    It cannot see a copy of the bot on another host (the laptop-and-server case), because each
    host locks its own file. For that, hold a lease the venue itself can arbitrate: a row keyed
    by trading account in a shared store with an expiry, taken before every open and refused when
    another holder's lease is live. The package does not ship that store; `InstanceLock` is the
    local half."""

    path: str
    _fh = None

    def __enter__(self) -> InstanceLock:
        import fcntl

        self._fh = open(self.path, "a+")  # noqa: SIM115
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._fh.seek(0)
            owner = self._fh.read().strip() or "unknown"
            self._fh.close()
            self._fh = None
            raise RuntimeError(f"another instance holds {self.path} (pid {owner}); refusing to start a second trading process") from exc
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        return self

    def __exit__(self, *exc) -> None:
        if self._fh is not None:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None


def host_is_sandbox(base_url: str) -> bool:
    u = base_url.lower()
    return any(m in u for m in SANDBOX_HOST_MARKERS)


def _is_loopback(base_url: str) -> bool:
    u = base_url.lower()
    return "localhost" in u or "127.0.0.1" in u


def mode_mismatch_reason(base_url: str, *, live: bool, account_id: str | None = None) -> str | None:
    """None when the declared mode and the host agree. Live against a sandbox host is refused as
    firmly as paper against a production host: the first trades nothing while believing it does,
    the second trades real money while believing it does not. A loopback host (the IBKR gateway)
    is judged by `account_id` instead; with neither signal the check cannot decide and refuses."""
    if _is_loopback(base_url):
        if account_id is None:
            return f"{base_url} is a local gateway; pass account_id so paper (DU...) and live can be told apart"
        paper = account_id.strip().upper().startswith("DU")
        if live and paper:
            return f"config says live but account {account_id} is a paper account"
        if not live and not paper:
            return f"config says paper but account {account_id} is a live account"
        return None
    sandbox = host_is_sandbox(base_url)
    if live and sandbox:
        return f"config says live but {base_url} is a sandbox host"
    if not live and not sandbox:
        return f"config says paper but {base_url} is a production host"
    return None
