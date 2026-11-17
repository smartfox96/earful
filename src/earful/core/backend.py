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

Code under test reaches the backend of the run it is in through ``effect``. Nothing has to
be handed a backend: an agent is built as it is built anywhere, and the stand-in for the
service it writes to calls ``effect`` where the real one would make the write durable.

    class FakeReservations:
        async def create(self, party_size: int) -> str:
            return await effect("book-table-1", {"party_size": party_size})

Where a tool talks to its service directly and has nothing to stand in for, an ``Effect``
declares the write for the tool as a whole, and an adapter makes it in place of the tool's
body.

Framework-independent: this module must never import livekit.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Literal

from .ledger import Ledger, json_value
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
        """Stop every write still under way. The harness does this when the scenario is
        over: a write that had not committed by then has no outcome in the ledger."""
        for timer, _done in self._in_flight.values():
            timer.cancel()
        self._in_flight.clear()

    def commits(self, intent: str) -> int:
        return self._commits.get(intent, 0)


# The backend of the run in progress, if any. A harness installs one for each run.
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
    """Request one durable effect for ``intent`` from the backend of the run in progress
    and return its receipt: ``EffectBackend.write``, without having been handed a backend.

    Raises ``RuntimeError`` outside a run. A stand-in that calls this is test code, and a
    write that went nowhere would leave a contract with nothing to check.
    """
    backend = _backend.get()
    if backend is None:
        raise RuntimeError(
            f"effect({intent!r}) was called outside an Earful run: there is no backend to write to"
        )
    return await backend.write(intent, {} if payload is None else payload, key=key, latency=latency)


Arguments = dict[str, Any]  # a tool call's arguments, by parameter name


@dataclass(frozen=True)
class Effect:
    """One durable write, declared for a tool as a whole.

    In a run, a call of the tool makes the write in place of the tool's body, and the tool
    returns what ``result`` makes of the receipt. Outside a run the tool is untouched.

    ``intent`` is the logical action the write is for. ``payload`` gives what is written,
    from the call's arguments: by default the arguments themselves, as JSON holds them.
    ``key`` is the idempotency key sent with the write: one string for every call, or a
    function of the arguments. By default no key is sent. ``result`` gives what the tool
    returns, from the receipt and the arguments: by default the receipt. ``latency`` is
    how long the backend takes to make the write durable, in seconds.

    A write the backend rejects, or one whose caller stops waiting, raises from the tool
    as ``BackendError`` or ``BackendTimeout``.
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
