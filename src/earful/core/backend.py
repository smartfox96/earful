"""Effect backend: the witness for durable side effects.

Tools under test write here instead of to a real service. A committed write is recorded
in the ledger at the moment it becomes durable, separately from whatever the tool goes on
to return, so "the tool wrote, then its result was lost" is observable.

Framework-independent: this module must never import livekit.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .ledger import Ledger


class EffectBackend:
    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger
        self._commits: dict[str, int] = {}
        self._receipts = 0

    async def write(self, intent: str, payload: dict[str, Any], *, latency: float = 0.0) -> str:
        """Commit one durable effect for ``intent`` and return its receipt."""
        if latency > 0:
            await asyncio.sleep(latency)
        self._receipts += 1
        receipt = f"R{self._receipts}"
        self._commits[intent] = self._commits.get(intent, 0) + 1
        self._ledger.append(
            "backend",
            "effect.committed",
            intent=intent,
            receipt=receipt,
            payload=payload,
            count_for_intent=self._commits[intent],
        )
        return receipt

    def commits(self, intent: str) -> int:
        return self._commits.get(intent, 0)
