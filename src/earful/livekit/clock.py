"""Virtual time for a real LiveKit AgentSession.

Adapted from ``tests/virtual_time.py`` in livekit/agents (Apache-2.0, see NOTICE). The
upstream helper is a pytest fixture that only exists from livekit-agents 1.6.0 and is not
shipped in the wheel. This version has no pytest dependency, tolerates modules that are
missing in older releases, and reports what it patched so a run can record it.

Two things are controlled:

1. The event loop clock. ``VirtualTimeLoop`` never sleeps: when it has nothing ready it jumps
   its clock to the next scheduled timer. Order of timer firings is therefore fixed by their
   scheduled times, not by scheduler jitter.
2. Wall-clock reads. The voice module reads ``time.time()`` / ``time.perf_counter()``
   directly in ~100 places and captures ``time.time`` as a pydantic ``default_factory`` on
   event and chat-context models. Both are pointed at the loop's virtual clock for the
   duration of a run.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import importlib
import selectors
import time
from collections.abc import Callable, Coroutine, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar
from unittest import mock

T = TypeVar("T")

# A fixed epoch so patched time.time() looks like a real timestamp. Only differences matter.
WALL_CLOCK_EPOCH = 1_700_000_000.0

_REAL_TIME = time.time
_REAL_PERF = time.perf_counter
_REAL_TIME_NS = time.time_ns

# Smallest separation between two wall-clock reads at the same loop instant. Items created
# synchronously still need distinct, ordered created_at values (ChatContext orders by it).
_TICK_EPSILON = 1e-6

# Virtual time is an integer count of 1 us ticks. The float the loop reports is always
# ticks / 1e6, so the time at an instant does not depend on the sequence of float additions
# that led to it, and a timer due "at 5.5 s" fires with loop.time() == 5.5 however its due
# time was computed. Timers are ordered by tick, then by scheduling order.
_TICKS_PER_SECOND = 1_000_000
_HALF_TICK = 0.5 / _TICKS_PER_SECOND


# Real seconds the loop may sit idle with no timer due and no ready callback before the run
# is declared stalled. Inside the deterministic boundary nothing can wake an idle loop (no
# work runs on threads), so idling means the run is deadlocked: typically a scripted
# provider raised inside the framework and the session is waiting for audio that will never
# come. Failing beats hanging a CI job.
STALL_SECONDS = 60.0


class VirtualTimeStall(RuntimeError):
    """The virtual-time loop had nothing to run and nothing scheduled."""


def _tick(t: float) -> int:
    return round(t * _TICKS_PER_SECOND)



# Modules whose pydantic models may capture time.time as a default_factory. Missing modules
# are skipped: the list is a superset across supported livekit-agents releases.
_MODEL_MODULES = (
    "livekit.agents.voice.events",
    "livekit.agents.llm.chat_context",
    "livekit.agents.inference.interruption",
)


class _VirtualClock:
    __slots__ = ("_read", "_ticks")

    def __init__(self) -> None:
        self._ticks = 0
        self._read = 0.0

    def ticks(self) -> int:
        return self._ticks

    def time(self) -> float:
        return self._ticks / _TICKS_PER_SECOND

    def advance_to_tick_of(self, t: float) -> None:
        """Jump forward to the tick nearest ``t``; never move backwards.

        A due time exactly half a tick past a tick rounds down, yet a timer there is not
        ready until the clock passes it; take the next tick so the loop cannot stall on it.
        """
        tick = _tick(t)
        if t >= tick / _TICKS_PER_SECOND + _HALF_TICK:
            tick += 1
        self._ticks = max(self._ticks, tick)

    def read(self) -> float:
        """Wall-clock view: strictly increasing, even within one loop instant."""
        now = self.time()
        self._read = now if now > self._read else self._read + _TICK_EPSILON
        return self._read


class _AutojumpSelector(selectors.BaseSelector):
    """Never blocks for a timer: jumps the virtual clock to it instead."""

    def __init__(self, clock: _VirtualClock) -> None:
        self._clock = clock
        self._real = selectors.DefaultSelector()

    def register(self, fileobj: Any, events: int, data: Any = None) -> selectors.SelectorKey:
        return self._real.register(fileobj, events, data)

    def unregister(self, fileobj: Any) -> selectors.SelectorKey:
        return self._real.unregister(fileobj)

    def modify(self, fileobj: Any, events: int, data: Any = None) -> selectors.SelectorKey:
        return self._real.modify(fileobj, events, data)

    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
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

    def _idle(self) -> list[tuple[selectors.SelectorKey, int]]:
        """Nothing is ready or scheduled: only an off-loop wakeup can make progress."""
        ready = self._real.select(STALL_SECONDS)
        if not ready:
            raise VirtualTimeStall(
                f"virtual-time loop idle for {STALL_SECONDS:g} real seconds with nothing "
                "scheduled: the run is deadlocked (check the framework's error log)"
            )
        return ready

    def get_map(self) -> Any:
        return self._real.get_map()

    def close(self) -> None:
        self._real.close()


class _OrderedTimerHandle(asyncio.TimerHandle):
    """A timer ordered by (grid tick of its due time, scheduling order).

    asyncio orders its timer heap by due time alone and the heap is not stable, so two
    timers due at the same instant could fire in either order, and float noise decides
    which of two timers meant for the same instant comes first. This handle puts timers
    due within the same microsecond in the order they were scheduled.
    """

    __slots__ = ("_order", "_tick")

    def __init__(self, when: float, order: int, callback: Any, args: Any, loop: Any, context: Any):
        super().__init__(when, callback, args, loop, context)
        self._order = order
        self._tick = _tick(when)

    def _key(self) -> tuple[int, int]:
        return (self._tick, self._order)

    def __lt__(self, other: Any) -> bool:
        return self._key() < other._key() if isinstance(other, _OrderedTimerHandle) else NotImplemented

    def __le__(self, other: Any) -> bool:
        return self._key() <= other._key() if isinstance(other, _OrderedTimerHandle) else NotImplemented

    def __gt__(self, other: Any) -> bool:
        return self._key() > other._key() if isinstance(other, _OrderedTimerHandle) else NotImplemented

    def __ge__(self, other: Any) -> bool:
        return self._key() >= other._key() if isinstance(other, _OrderedTimerHandle) else NotImplemented


class VirtualTimeLoop(asyncio.SelectorEventLoop):
    """A SelectorEventLoop whose clock advances only when the loop would otherwise sleep.

    The clock jumps to the 1 us tick of the next timer. Timers due within the same
    microsecond fire in the order they were scheduled.
    """

    def __init__(self) -> None:
        self._virtual_clock = _VirtualClock()
        super().__init__(selector=_AutojumpSelector(self._virtual_clock))
        # A timer is ready when its due time is within half a tick of the clock.
        self._clock_resolution = _HALF_TICK
        self._timer_order = 0
        self.executor_calls = 0

    def time(self) -> float:
        return self._virtual_clock.time()

    def time_us(self) -> int:
        """Virtual time as the exact integer the clock holds."""
        return self._virtual_clock.ticks()

    def wall_read(self) -> float:
        return self._virtual_clock.read()

    def call_at(self, when: float, callback: Any, *args: Any, context: Any = None) -> Any:
        # Same steps as BaseEventLoop.call_at, with a handle that keeps same-instant timers
        # in scheduling order.
        if when is None:
            raise TypeError("when cannot be None")
        self._check_closed()
        if self._debug:
            self._check_thread()
            self._check_callback(callback, "call_at")
        self._timer_order += 1
        timer = _OrderedTimerHandle(when, self._timer_order, callback, args, self, context)
        if timer._source_traceback:
            del timer._source_traceback[-1]
        heapq.heappush(self._scheduled, timer)
        timer._scheduled = True
        return timer

    def run_in_executor(self, executor: Any, func: Any, *args: Any) -> Any:
        # Work handed to a thread runs in real time and escapes the virtual clock. Count it
        # so a run can report that it left the deterministic boundary.
        self.executor_calls += 1
        return super().run_in_executor(executor, func, *args)

    async def shutdown_default_executor(self, *args: Any) -> None:
        await super().shutdown_default_executor()


def _virtual_loop() -> VirtualTimeLoop | None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    return loop if isinstance(loop, VirtualTimeLoop) else None


def _now() -> float:
    loop = _virtual_loop()
    return WALL_CLOCK_EPOCH + loop.wall_read() if loop is not None else _REAL_TIME()


def _perf() -> float:
    loop = _virtual_loop()
    return loop.wall_read() if loop is not None else _REAL_PERF()


def _now_ns() -> int:
    loop = _virtual_loop()
    return int(_now() * 1e9) if loop is not None else _REAL_TIME_NS()


_VIRTUAL_FOR: dict[Any, Callable[[], Any]] = {_REAL_TIME: _now, _REAL_PERF: _perf}


@dataclass
class ClockPatchReport:
    """What the patch covered on this livekit-agents version. Recorded in the run manifest."""

    model_fields: list[str] = field(default_factory=list)
    skipped_modules: list[str] = field(default_factory=list)
    otel_patched: bool = False


def _to_plain(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_plain(v) for v in obj]
    return obj


def _model_node(schema: dict[str, Any]) -> dict[str, Any]:
    node = schema
    while node.get("type") != "model":
        node = node["schema"]
    return node


_PATCHED: dict[type, tuple[Any, Any]] = {}
_REPORT = ClockPatchReport()
_BUILT = False


def _build_patch_cache() -> None:
    """Build, once, a virtual-clock validator for every model that captured a real clock."""
    global _BUILT
    if _BUILT:
        return
    _BUILT = True

    from pydantic import BaseModel
    from pydantic_core import SchemaValidator

    for module_name in _MODEL_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            _REPORT.skipped_modules.append(module_name)
            continue
        for obj in vars(module).values():
            if not (isinstance(obj, type) and issubclass(obj, BaseModel)) or obj in _PATCHED:
                continue
            fields = {
                name: _VIRTUAL_FOR[info.default_factory]  # type: ignore[index]
                for name, info in obj.model_fields.items()
                if info.default_factory in _VIRTUAL_FOR
            }
            if not fields:
                continue
            schema = _to_plain(obj.__pydantic_core_schema__)
            node = _model_node(schema)
            field_schemas = node["schema"]["fields"]
            for name, factory in fields.items():
                field_schemas[name]["schema"]["default_factory"] = factory
                _REPORT.model_fields.append(f"{obj.__module__}.{obj.__name__}.{name}")
            patched = SchemaValidator(schema, node.get("config"), _use_prebuilt=False)
            _PATCHED[obj] = (patched, obj.__pydantic_validator__)
    _REPORT.model_fields.sort()


@contextlib.contextmanager
def _patched_model_clocks() -> Iterator[None]:
    _build_patch_cache()
    try:
        for model, (patched, _original) in _PATCHED.items():
            model.__pydantic_validator__ = patched  # type: ignore[attr-defined]
        yield
    finally:
        for model, (_patched, original) in _PATCHED.items():
            model.__pydantic_validator__ = original  # type: ignore[attr-defined]


@contextlib.contextmanager
def virtual_wall_clock() -> Iterator[ClockPatchReport]:
    """Point wall-clock reads at the running VirtualTimeLoop for the duration of the block."""
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(time, "time", _now))
        stack.enter_context(mock.patch.object(time, "time_ns", _now_ns))
        stack.enter_context(mock.patch.object(time, "perf_counter", _perf))
        try:
            from opentelemetry.sdk import trace as otel_sdk_trace

            if hasattr(otel_sdk_trace, "time_ns"):
                stack.enter_context(mock.patch.object(otel_sdk_trace, "time_ns", _now_ns))
                _REPORT.otel_patched = True
        except ImportError:
            pass
        stack.enter_context(_patched_model_clocks())
        yield _REPORT


def run_virtual(main: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Run ``main()`` to completion on a fresh VirtualTimeLoop with the wall clock patched.

    Every run gets a new loop, so no timer, task or clock state carries over between runs.
    """
    loop = VirtualTimeLoop()
    try:
        asyncio.set_event_loop(loop)
        with virtual_wall_clock():
            return loop.run_until_complete(main())
    finally:
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            asyncio.set_event_loop(None)
            loop.close()


def now_us() -> int:
    """The running loop's time in integer microseconds.

    On the virtual clock this is the clock's own integer, with no float in between. It is
    the only time Earful's providers, sink and ledger read.
    """
    loop = asyncio.get_running_loop()
    if isinstance(loop, VirtualTimeLoop):
        return loop.time_us()
    return round(loop.time() * _TICKS_PER_SECOND)  # real-clock control runs
