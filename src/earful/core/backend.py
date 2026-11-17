"""Effect backend: the witness for durable side effects.

Tools under test write here instead of to a real service. Every write is recorded when it
is requested and again when its outcome is known: committed, deduplicated or failed. Both
are separate from whatever the tool goes on to return, so "the tool wrote, then its result
was lost" is observable.

A scenario can declare faults for individual writes:

* ``error``: the backend rejects the write. Nothing commits, and the caller is told.
* ``timeout``: the caller stops waiting after a given time and is told the write timed
  out. The write itself still commits at its own latency, before or after that.
* ``drop``: the write is lost. Nothing commits, and the caller is told it timed out.

Framework-independent: this module must never import livekit.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import Any, Literal

from .ledger import Ledger
from .timebase import to_seconds, to_us

# The witnessed tool invocation currently running, if any. A framework adapter sets it
# while a tool runs, so each write can name the invocation that made it.
current_tool: ContextVar[int | None] = ContextVar("earful_current_tool", default=None)

BackendMode = Literal["plain", "keyed"]
FAULTS = ("backend.error", "backend.timeout", "backend.drop")


class BackendError(RuntimeError):
    """The backend rejected a write. Nothing was committed."""


class BackendTimeout(TimeoutError):
    """The caller stopped waiting for a write. Whether it committed is not known to it."""


class EffectBackend:
    """A deterministic store.

    ``plain``: every write commits. ``keyed``: a write whose idempotency key has already
    committed is deduplicated and returns the original receipt.

    ``latencies_us`` maps a request number (from 1) to how long that write takes, in
    place of what the tool asked for. ``faults`` maps a request number to its fault:
    ``("backend.error", None)``, or ``("backend.timeout", us)`` / ``("backend.drop", us)``
    with the time after which the caller stops waiting.
    """

    def __init__(
        self,
        ledger: Ledger,
        *,
        mode: BackendMode = "plain",
        latencies_us: dict[int, int] | None = None,
        faults: dict[int, tuple[str, int | None]] | None = None,
    ) -> None:
        if mode not in ("plain", "keyed"):
            raise ValueError(f"unknown backend mode: {mode}")
        self.mode = mode
        self._latencies_us = dict(latencies_us or {})
        self._faults = dict(faults or {})
        self._ledger = ledger
        self._requests = 0
        self._receipts = 0
        self._commits: dict[str, int] = {}
        self._by_key: dict[str, str] = {}
        self._in_flight: set[asyncio.Task[Any]] = set()

    async def write(
        self,
        intent: str,
        payload: dict[str, Any],
        *,
        key: str | None = None,
        latency: float = 0.0,
    ) -> str:
        """Request one durable effect for ``intent`` and return its receipt.

        ``latency`` is the time, in seconds, the backend takes to make the write durable;
        a latency the scenario declared for this request takes its place. A keyed backend
        decides whether a key is a repeat at that moment, not when the write was
        requested, so two overlapping writes with one key commit once.

        Raises ``BackendError`` if the scenario declared that the backend rejects this
        write, and ``BackendTimeout`` if it declared that the caller stops waiting.
        """
        self._requests += 1
        request = self._requests
        latency_us = self._latencies_us.get(request, to_us(latency))
        fault, give_up_us = self._faults.get(request, (None, None))
        self._ledger.append(
            "backend",
            "effect.requested",
            request=request,
            intent=intent,
            key=key,
            payload=payload,
            tool=current_tool.get(),
            latency_us=latency_us,
        )
        if fault in ("backend.timeout", "backend.drop"):
            assert give_up_us is not None
            # The write goes on by itself; the caller waits only so long for it.
            if fault == "backend.timeout":
                task = asyncio.ensure_future(self._complete(request, intent, key, latency_us, rejected=False))
                self._in_flight.add(task)
                task.add_done_callback(self._in_flight.discard)
            await asyncio.sleep(to_seconds(give_up_us))
            self._ledger.append(
                "backend", "effect.timed_out", request=request, intent=intent, key=key, after_us=give_up_us
            )
            raise BackendTimeout(f"write {request} timed out after {give_up_us} us")
        return await self._complete(request, intent, key, latency_us, rejected=fault == "backend.error")

    async def _complete(self, request: int, intent: str, key: str | None, latency_us: int, *, rejected: bool) -> str:
        if latency_us > 0:
            await asyncio.sleep(to_seconds(latency_us))
        if rejected:
            self._ledger.append("backend", "effect.failed", request=request, intent=intent, key=key)
            raise BackendError(f"write {request} was rejected")

        if self.mode == "keyed" and key is not None and key in self._by_key:
            receipt = self._by_key[key]
            self._ledger.append(
                "backend",
                "effect.deduplicated",
                request=request,
                intent=intent,
                key=key,
                receipt=receipt,
            )
            return receipt

        self._receipts += 1
        receipt = f"R{self._receipts}"
        self._commits[intent] = self._commits.get(intent, 0) + 1
        if key is not None:
            self._by_key[key] = receipt
        self._ledger.append(
            "backend",
            "effect.committed",
            request=request,
            intent=intent,
            key=key,
            receipt=receipt,
            count_for_intent=self._commits[intent],
        )
        return receipt

    async def cancel_in_flight(self) -> None:
        """Stop every write still under way. The harness does this when the scenario is
        over: a write that had not committed by then has no outcome in the ledger."""
        tasks = list(self._in_flight)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def commits(self, intent: str) -> int:
        return self._commits.get(intent, 0)
