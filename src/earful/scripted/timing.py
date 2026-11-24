"""The origin and deadlines of a scripted timeline."""

from __future__ import annotations

import asyncio

from ..core.ledger import Ledger
from ..core.timebase import to_seconds
from .clock import now_us

REAL_CLOCK_ORIGIN_TOLERANCE_US = 5_000


async def sleep_until(t0_us: int, at_us: int) -> None:
    delay_us = t0_us + at_us - now_us()
    if delay_us > 0:
        await asyncio.sleep(to_seconds(delay_us))


class WorldOrigin:
    """The instant the scripted user's clock starts: the first audio frame a provider gets.
    VAD and STT must agree on it, or the run is invalid; ``tolerance_us`` is for real-clock runs."""

    def __init__(self, ledger: Ledger, errors: list[str], *, tolerance_us: int = 0) -> None:
        self._ledger = ledger
        self._errors = errors
        self._tolerance_us = tolerance_us
        self.t0_us: int | None = None
        self.by: str | None = None
        self.started = asyncio.Event()  # set once the origin is known

    def mark(self, by: str, at_us: int) -> None:
        if self.t0_us is None:
            self.t0_us, self.by = at_us, by
            self._ledger.append("world", "user.origin", by=by)
            self.started.set()
        elif abs(at_us - self.t0_us) > self._tolerance_us:
            self._errors.append(
                f"{by} started its clock at {at_us} us, {self.by} at {self.t0_us} us"
            )
