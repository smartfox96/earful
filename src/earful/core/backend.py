"""Effect backend: records each durable write a tool requests and what became of it."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Literal

from .ledger import Ledger, json_value
from .timebase import to_seconds, to_us

# The witnessed tool invocation currently running, if any; set by a framework adapter.
current_tool: ContextVar[int | None] = ContextVar("earful_current_tool", default=None)

BackendMode = Literal["plain", "keyed"]
FAULTS = ("backend.error", "backend.timeout", "backend.drop")


class BackendError(RuntimeError):
    """The backend rejected a write. Nothing was committed."""


class BackendTimeout(TimeoutError):
    """The caller stopped waiting for a write. Whether it committed is not known to it."""


class EffectBackend:
    """A deterministic store for the writes of one run.

    ``plain`` commits every write; ``keyed`` deduplicates a write whose key has committed.
    ``latencies_us`` and ``faults`` are keyed by request number, from 1. A fault is
    ``("backend.error", None)``, or a timeout or drop with how long the caller waits, in us.
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
        """Request one durable effect for ``intent`` and return its receipt. ``latency`` is
        in seconds; a keyed backend decides whether a key repeats when the write commits.
        Raises ``BackendError`` or ``BackendTimeout`` for a write with a declared fault."""
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
        # The outcome is scheduled now, whatever becomes of the caller. A dropped write has none.
        outcome: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        if fault != "backend.drop":
            self._schedule(
                outcome, request, intent, key, latency_us, rejected=fault == "backend.error"
            )
        if fault in ("backend.timeout", "backend.drop"):
            assert give_up_us is not None
            await asyncio.sleep(to_seconds(give_up_us))
            self._ledger.append(
                "backend",
                "effect.timed_out",
                request=request,
                intent=intent,
                key=key,
                after_us=give_up_us,
            )
            raise BackendTimeout(f"write {request} timed out after {give_up_us} us")
        return await outcome

    def _schedule(
        self,
        outcome: asyncio.Future[str],
        request: int,
        intent: str,
        key: str | None,
        latency_us: int,
        *,
        rejected: bool,
    ) -> None:
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
        self._in_flight[request] = (
            loop.call_later(to_seconds(latency_us), settle),
            loop.create_future(),
        )

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
        """Stop every write still under way: such a write has no outcome in the ledger."""
        for timer, _done in self._in_flight.values():
            timer.cancel()
        self._in_flight.clear()

    def commits(self, intent: str) -> int:
        return self._commits.get(intent, 0)


_backend: ContextVar[EffectBackend | None] = ContextVar("earful_backend", default=None)


def install(backend: EffectBackend | None) -> Token[EffectBackend | None]:
    """Make ``backend`` the one ``effect`` writes to in this context; returns a reset token."""
    return _backend.set(backend)


def uninstall(token: Token[EffectBackend | None]) -> None:
    _backend.reset(token)


def active() -> EffectBackend | None:
    """The backend of the run in progress, or None outside a run."""
    return _backend.get()


async def effect(
    intent: str,
    payload: dict[str, Any] | None = None,
    *,
    key: str | None = None,
    latency: float = 0.0,
) -> str:
    """``EffectBackend.write`` on the backend of the run in progress. Raises ``RuntimeError``
    outside a run."""
    backend = _backend.get()
    if backend is None:
        raise RuntimeError(
            f"effect({intent!r}) was called outside an Earful run: there is no backend to write to"
        )
    return await backend.write(intent, {} if payload is None else payload, key=key, latency=latency)


Arguments = dict[str, Any]  # a tool call's arguments, by parameter name


@dataclass(frozen=True)
class Effect:
    """One durable write, declared for a tool as a whole and made in place of its body.

    ``payload`` defaults to the call's arguments as JSON, ``key`` to no idempotency key and
    ``result`` to the receipt. ``latency`` is in seconds.
    """

    intent: str
    payload: Callable[[Arguments], dict[str, Any]] | None = None
    key: str | Callable[[Arguments], str | None] | None = None
    result: Callable[[str, Arguments], Any] | None = None
    latency: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.intent, str) or not self.intent:
            raise ValueError(f"an effect names its intent, a non-empty string; got {self.intent!r}")
        if self.latency < 0:
            raise ValueError(f"an effect's latency cannot be negative; got {self.latency}")

    async def make(self, arguments: Arguments) -> Any:
        """Make the write for a call with ``arguments`` and return the tool's result."""
        payload = json_value(arguments) if self.payload is None else self.payload(arguments)
        key = self.key(arguments) if callable(self.key) else self.key
        receipt = await effect(self.intent, payload, key=key, latency=self.latency)
        return receipt if self.result is None else self.result(receipt, arguments)


__all__ = [
    "FAULTS",
    "Arguments",
    "BackendError",
    "BackendMode",
    "BackendTimeout",
    "Effect",
    "EffectBackend",
    "active",
    "current_tool",
    "effect",
    "install",
    "uninstall",
]
