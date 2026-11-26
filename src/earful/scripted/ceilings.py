"""The bounds that make every run end: an idle loop, a loop that never waits, and the
tasks a run leaves behind."""

from __future__ import annotations

import asyncio
import math
import selectors
from collections.abc import Callable
from typing import Any

from .timers import VirtualTimeStall, _VirtualClock

# Real seconds an idle loop with nothing scheduled may wait before the run counts as deadlocked.
STALL_SECONDS = 60.0
# Loop passes at one instant before the clock moves to the next timer, as real time would.
# A run makes at most about a hundred; a task that polls with asyncio.sleep(0) makes more.
SPIN_PASSES = 10_000
# Virtual seconds the tasks a run leaves behind have to end once they are cancelled.
LEFTOVER_SECONDS = 5.0
# Tasks that ignored cancellation, kept so that no coroutine is closed outside its loop.
_KEPT: list[asyncio.Task[Any]] = []


class _AutojumpSelector(selectors.BaseSelector):
    """Moves the clock to the next timer when the loop would wait, or would never wait."""

    def __init__(
        self, clock: _VirtualClock, next_due: Callable[[], float | None], stall_seconds: float
    ) -> None:
        self._stall_seconds = stall_seconds
        self._clock = clock
        self._next_due = next_due
        self._real = selectors.DefaultSelector()
        self._passes = 0  # consecutive passes that did not wait

    def register(self, fileobj: Any, events: int, data: Any = None) -> selectors.SelectorKey:
        return self._real.register(fileobj, events, data)

    def unregister(self, fileobj: Any) -> selectors.SelectorKey:
        return self._real.unregister(fileobj)

    def modify(self, fileobj: Any, events: int, data: Any = None) -> selectors.SelectorKey:
        return self._real.modify(fileobj, events, data)

    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
        if timeout == 0:
            self._spin()
        else:
            self._passes = 0
        if timeout is not None and math.isinf(timeout):
            timeout = None  # only a timer that never fires is scheduled
        if len(self._real.get_map()) <= 1:
            # Only the loop's self-pipe is registered: no real I/O can be pending.
            if timeout is None:
                return self._idle()
            if timeout > 0:
                self._clock.advance_to_tick_of(self._clock.time() + timeout)
            return []
        ready = self._real.select(0)
        if ready or timeout == 0:
            return ready
        if timeout is None:
            return self._idle()
        self._clock.advance_to_tick_of(self._clock.time() + timeout)
        return []

    def _spin(self) -> None:
        self._passes += 1
        if self._passes < SPIN_PASSES:
            return
        self._passes = 0
        due = self._next_due()
        if due is None or not math.isfinite(due):
            raise VirtualTimeStall(
                f"virtual-time loop ran {SPIN_PASSES} passes at one instant with no timer "
                "to move to: a task never waits (check for a loop around asyncio.sleep(0))"
            )
        self._clock.advance_to_tick_of(due)

    def _idle(self) -> list[tuple[selectors.SelectorKey, int]]:
        # Nothing is ready or scheduled: only an off-loop wakeup can make progress.
        ready = self._real.select(self._stall_seconds)
        if not ready:
            raise VirtualTimeStall(
                f"virtual-time loop idle for {self._stall_seconds:g} real seconds with nothing "
                "scheduled: the run is deadlocked (check the framework's error log)"
            )
        return ready

    def get_map(self) -> Any:
        return self._real.get_map()

    def close(self) -> None:
        self._real.close()


def cancel(task: asyncio.Future[Any]) -> bool:
    """Ask ``task`` to stop. False if it awaits itself, which no cancellation can reach."""
    try:
        task.cancel()
    except RecursionError:
        return False
    return True


def finish(loop: asyncio.AbstractEventLoop) -> None:
    """What asyncio.run does at its end, with a bound on the tasks that ignore cancellation.
    The loop was never made the thread's current one."""
    try:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for task in pending:
            cancel(task)
        if pending:
            loop.run_until_complete(asyncio.wait(pending, timeout=LEFTOVER_SECONDS))
        stubborn = [task for task in pending if not task.done()]
        for task in stubborn:
            task._log_destroy_pending = False  # type: ignore[attr-defined]  # reported
        _KEPT[:] = [*_KEPT, *stubborn][-100:]
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
    finally:
        loop.close()
