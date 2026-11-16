"""Effect backend: the witness for durable side effects.

Tools under test write here instead of to a real service. Every write is recorded twice:
when it is requested and when it becomes durable (or is deduplicated). Both are separate
from whatever the tool goes on to return, so "the tool wrote, then its result was lost" is
observable.

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


class EffectBackend:
    """A deterministic store.

    ``plain``: every write commits. ``keyed``: a write whose idempotency key has already
    committed is deduplicated and returns the original receipt.
    """

    def __init__(
        self, ledger: Ledger, *, mode: BackendMode = "plain", latencies_us: dict[int, int] | None = None
    ) -> None:
        if mode not in ("plain", "keyed"):
            raise ValueError(f"unknown backend mode: {mode}")
        self.mode = mode
        # Request number (from 1) -> how long that write takes, whatever the tool asked for.
        self._latencies_us = dict(latencies_us or {})
        self._ledger = ledger
        self._requests = 0
        self._receipts = 0
        self._commits: dict[str, int] = {}
        self._by_key: dict[str, str] = {}

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
        a latency the scenario declared for this request takes its place.
        A keyed backend decides whether a key is a repeat at that moment, not when the
        write was requested, so two overlapping writes with one key commit once.
        """
        self._requests += 1
        request = self._requests
        latency_us = self._latencies_us.get(request, to_us(latency))
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
        if latency_us > 0:
            await asyncio.sleep(to_seconds(latency_us))

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

    def commits(self, intent: str) -> int:
        return self._commits.get(intent, 0)
