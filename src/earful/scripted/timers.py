"""Clock ticks and stable ordering of simultaneous timers.
Adapted from tests/virtual_time.py in livekit/agents (Apache-2.0, see NOTICE)."""

from __future__ import annotations

import asyncio
import math
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import traceback

# Gap between wall-clock reads at one loop instant: ChatContext orders items by created_at.
_TICK_EPSILON = 1e-6

# Virtual time is an integer count of 1 us ticks, so an instant never depends on float sums.
_TICKS_PER_SECOND = 1_000_000
_HALF_TICK = 0.5 / _TICKS_PER_SECOND


# The tick of a timer due at an infinite time, which never fires, as on a real loop.
_NEVER = sys.maxsize


class VirtualTimeStall(RuntimeError):
    """The virtual-time loop had nothing to run and nothing scheduled."""


def _tick(t: float) -> int:
    if math.isfinite(t):
        return round(t * _TICKS_PER_SECOND)
    # A real loop runs a timer due at minus infinity or at NaN at once.
    return _NEVER if t > 0 else -_NEVER


class _VirtualClock:
    __slots__ = ("_read", "_read_ns", "_ticks", "held")

    def __init__(self) -> None:
        self._ticks = 0
        self._read = 0.0
        self._read_ns = 0
        self.held = 0  # above zero while a read must leave the next one as it would have been

    def ticks(self) -> int:
        return self._ticks

    def time(self) -> float:
        return self._ticks / _TICKS_PER_SECOND

    def advance_to_tick_of(self, t: float) -> None:
        if not math.isfinite(t):
            return
        tick = _tick(t)
        # A due time half a tick past a tick rounds down, and its timer would never be ready.
        if t >= tick / _TICKS_PER_SECOND + _HALF_TICK:
            tick += 1
        self._ticks = max(self._ticks, tick)

    def read(self) -> float:
        # Strictly increasing, even within one loop instant.
        now = self.time()
        if self.held:
            return max(now, self._read)
        self._read = now if now > self._read else self._read + _TICK_EPSILON
        return self._read

    def read_ns(self) -> int:
        # Strictly increasing by a count of its own, so that it never moves what read() returns.
        self._read_ns = max(self._read_ns + 1, self._ticks * 1000)
        return self._read_ns


class _OrderedTimerHandle(asyncio.TimerHandle):
    # asyncio's timer heap is not stable: timers due in one microsecond fire in scheduling order.
    __slots__ = ("_order", "_tick")

    if TYPE_CHECKING:
        # TimerHandle internals that call_at sets. asyncio's stubs leave them out.
        _scheduled: bool
        _source_traceback: traceback.StackSummary | None

    def __init__(self, when: float, order: int, callback: Any, args: Any, loop: Any, context: Any):
        super().__init__(when, callback, args, loop, context)
        self._order = order
        self._tick = _tick(when)

    def _key(self) -> tuple[int, int]:
        return (self._tick, self._order)

    def __lt__(self, other: Any) -> bool:
        return (
            self._key() < other._key() if isinstance(other, _OrderedTimerHandle) else NotImplemented
        )

    def __le__(self, other: Any) -> bool:
        return (
            self._key() <= other._key()
            if isinstance(other, _OrderedTimerHandle)
            else NotImplemented
        )

    def __gt__(self, other: Any) -> bool:
        return (
            self._key() > other._key() if isinstance(other, _OrderedTimerHandle) else NotImplemented
        )

    def __ge__(self, other: Any) -> bool:
        return (
            self._key() >= other._key()
            if isinstance(other, _OrderedTimerHandle)
            else NotImplemented
        )
