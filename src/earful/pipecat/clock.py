"""Bind Pipecat’s clock to the virtual event loop."""

from __future__ import annotations

from pipecat.clocks.base_clock import BaseClock

from ..scripted.clock import now_us


class VirtualClock(BaseClock):
    """Pipecat's own clock, read from the event loop. Pipecat uses it to stamp frames and
    to release frames that carry a presentation time."""

    def __init__(self) -> None:
        self._t0_us: int | None = None

    def start(self) -> None:
        self._t0_us = now_us()

    def get_time(self) -> int:
        return 0 if self._t0_us is None else (now_us() - self._t0_us) * 1000  # nanoseconds
