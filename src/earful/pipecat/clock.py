"""Bind Pipecat’s clock and monotonic time reads to the virtual event loop."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Iterator
from unittest import mock

from pipecat.clocks.base_clock import BaseClock

from ..scripted.clock import VirtualTimeLoop, now_us


class VirtualClock(BaseClock):
    """Pipecat's own clock, read from the event loop. Pipecat uses it to stamp frames and
    to release frames that carry a presentation time."""

    def __init__(self) -> None:
        self._t0_us: int | None = None

    def start(self) -> None:
        self._t0_us = now_us()

    def get_time(self) -> int:
        return 0 if self._t0_us is None else (now_us() - self._t0_us) * 1000  # nanoseconds


@contextlib.contextmanager
def monotonic_on_the_loop() -> Iterator[None]:
    """Pipecat reads ``time.monotonic`` where LiveKit reads ``time.perf_counter``, and
    Earful's clock patch does not cover it. Point it at the loop for the run."""

    real_seconds, real_ns = time.monotonic, time.monotonic_ns

    def monotonic() -> float:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return real_seconds()
        return loop.time() if isinstance(loop, VirtualTimeLoop) else real_seconds()

    def monotonic_ns() -> int:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return real_ns()
        return loop.time_us() * 1000 if isinstance(loop, VirtualTimeLoop) else real_ns()

    with (
        mock.patch.object(time, "monotonic", monotonic),
        mock.patch.object(time, "monotonic_ns", monotonic_ns),
    ):
        yield
