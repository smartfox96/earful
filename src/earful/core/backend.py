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

Once a write is requested it belongs to the backend. Its outcome is scheduled on the clock
at that moment, from its latency and its fault, and nothing that happens to the caller
afterwards changes it: a tool that is cancelled while it waits has still written.

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
        self._in_flight: dict[int, tuple[asyncio.TimerHandle, asyncio.Future[None]]] = {}

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
        # The outcome is the backend's from here on: scheduled now, whatever becomes of
        # the caller. A dropped write has none.
        outcome: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        if fault != "backend.drop":
            self._schedule(outcome, request, intent, key, latency_us, rejected=fault == "backend.error")
        if fault in ("backend.timeout", "backend.drop"):
            assert give_up_us is not None
            await asyncio.sleep(to_seconds(give_up_us))
            self._ledger.append(
                "backend", "effect.timed_out", request=request, intent=intent, key=key, after_us=give_up_us
            )
            raise BackendTimeout(f"write {request} timed out after {give_up_us} us")
        return await outcome

    def _schedule(
        self, outcome: asyncio.Future[str], request: int, intent: str, key: str | None, latency_us: int, *, rejected: bool
    ) -> None:
        """Decide the write's outcome ``latency_us`` from now: at once if that is zero."""

        def settle() -> None:
            pending = self._in_flight.pop(request, None)
            try:
                receipt = self._complete(request, intent, key, rejected=rejected)
            except BackendError as exc:
                if not outcome.done():  # a caller that has gone is told nothing
                    outcome.set_exception(exc)
            else:
                if not outcome.done():
                    outcome.set_result(receipt)
            if pending is not None:
                pending[1].set_result(None)

        if latency_us <= 0:
            settle()
            return
        loop = asyncio.get_running_loop()
        self._in_flight[request] = (loop.call_later(to_seconds(latency_us), settle), loop.create_future())

    def _complete(self, request: int, intent: str, key: str | None, *, rejected: bool) -> str:
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

    async def settle_in_flight(self) -> None:
        """Wait until every write under way has had its outcome."""
        while self._in_flight:
            await next(iter(self._in_flight.values()))[1]

    async def cancel_in_flight(self) -> None:
        """Stop every write still under way. The harness does this when the scenario is
        over: a write that had not committed by then has no outcome in the ledger."""
        for timer, _done in self._in_flight.values():
            timer.cancel()
        self._in_flight.clear()

    def commits(self, intent: str) -> int:
        return self._commits.get(intent, 0)
