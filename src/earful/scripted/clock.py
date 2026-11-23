"""Virtual time for a real LiveKit AgentSession: the event loop clock and wall-clock reads.
Adapted from ``tests/virtual_time.py`` in livekit/agents (Apache-2.0, see NOTICE)."""

from __future__ import annotations

import _thread
import asyncio
import contextlib
import heapq
import logging
import random
import selectors
import sys
import threading
import time
import weakref
from collections.abc import Callable, Coroutine, Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar
from unittest import mock

if TYPE_CHECKING:
    import traceback
    from concurrent.futures import Executor
    from contextvars import Context

    from pydantic import BaseModel
    from typing_extensions import TypeVarTuple, Unpack

    Ts = TypeVarTuple("Ts")  # ``typing`` has TypeVarTuple only from Python 3.11

T = TypeVar("T")

# A fixed epoch, so that a patched time.time() looks like a real timestamp.
WALL_CLOCK_EPOCH = 1_700_000_000.0

_REAL_TIME = time.time
_REAL_PERF = time.perf_counter
_REAL_TIME_NS = time.time_ns
_REAL_PERF_NS = time.perf_counter_ns
_REAL_MONOTONIC = time.monotonic
_REAL_MONOTONIC_NS = time.monotonic_ns

# Gap between wall-clock reads at one loop instant: ChatContext orders items by created_at.
_TICK_EPSILON = 1e-6

# Virtual time is an integer count of 1 us ticks, so an instant never depends on float sums.
_TICKS_PER_SECOND = 1_000_000
_HALF_TICK = 0.5 / _TICKS_PER_SECOND

# Real seconds an idle loop with nothing scheduled may wait before the run counts as deadlocked.
STALL_SECONDS = 60.0


class VirtualTimeStall(RuntimeError):
    """The virtual-time loop had nothing to run and nothing scheduled."""


def _tick(t: float) -> int:
    return round(t * _TICKS_PER_SECOND)


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


class _AutojumpSelector(selectors.BaseSelector):
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
        # Nothing is ready or scheduled: only an off-loop wakeup can make progress.
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


# The kinds of work a loop cannot move in virtual time. Any of it makes a run invalid.
_STARTED, _WOKEN = "threads started", "wake-ups from another thread"
_WATCHED, _CHILDREN = "file descriptors watched", "child processes started"


class VirtualTimeLoop(asyncio.SelectorEventLoop):
    """A SelectorEventLoop whose clock jumps to the next timer when it would otherwise sleep."""

    if TYPE_CHECKING:
        # Loop internals that the methods below use. asyncio's stubs leave them out.
        _debug: bool
        _scheduled: list[asyncio.TimerHandle]
        _ssock: Any
        _thread_id: int | None

        def _check_closed(self) -> None: ...
        def _check_thread(self) -> None: ...
        def _check_callback(self, callback: object, method: str) -> None: ...

    def __init__(self) -> None:
        self._virtual_clock = _VirtualClock()
        # Set before the base class registers the loop's own pipe as a reader.
        self._left = dict.fromkeys((_STARTED, _WOKEN, _WATCHED, _CHILDREN), 0)
        self._left_lock = threading.Lock()
        super().__init__(selector=_AutojumpSelector(self._virtual_clock))
        # A timer is ready when its due time is within half a tick of the clock.
        self._clock_resolution = _HALF_TICK
        self._timer_order = 0
        self.executor_calls = 0

    def time(self) -> float:
        return self._virtual_clock.time()

    def time_us(self) -> int:
        """Virtual time in integer microseconds."""
        return self._virtual_clock.ticks()

    def wall_read(self) -> float:
        return self._virtual_clock.read()

    def read_ns(self) -> int:
        return self._virtual_clock.read_ns()

    def call_at(
        self,
        when: float,
        callback: Callable[[Unpack[Ts]], object],
        *args: Unpack[Ts],
        context: Context | None = None,
    ) -> asyncio.TimerHandle:
        # BaseEventLoop.call_at, with a handle that keeps same-instant timers in scheduling order.
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

    def run_in_executor(
        self, executor: Executor | None, func: Callable[[Unpack[Ts]], T], *args: Unpack[Ts]
    ) -> asyncio.Future[T]:
        # Thread work runs in real time: counted so that a run can report it.
        self.executor_calls += 1
        return super().run_in_executor(executor, func, *args)

    async def shutdown_default_executor(self, *args: Any) -> None:
        await super().shutdown_default_executor()

    def leaves(self, kind: str) -> None:
        """Count one piece of work the loop cannot move in virtual time."""
        with self._left_lock:
            self._left[kind] += 1

    def left(self) -> list[str]:
        """Why a run on this loop did not stay on the virtual clock. Empty if it did."""
        with self._left_lock:
            counted = [f"{kind}: {count}" for kind, count in self._left.items() if count]
        return [f"work left the virtual clock ({', '.join(counted)})"] if counted else []

    def call_soon_threadsafe(
        self,
        callback: Callable[[Unpack[Ts]], object],
        *args: Unpack[Ts],
        context: Context | None = None,
    ) -> asyncio.Handle:
        # Only work that ran in real time on another thread wakes the loop from there.
        if threading.get_ident() != self._thread_id:
            self.leaves(_WOKEN)
        return super().call_soon_threadsafe(callback, *args, context=context)

    def _add_reader(self, fd: Any, callback: Callable[..., object], *args: Any) -> Any:
        # A socket, a pipe or a child's descriptor becomes ready in real time.
        if fd != self._ssock.fileno():
            self.leaves(_WATCHED)
        return super()._add_reader(fd, callback, *args)  # type: ignore[misc]

    def _add_writer(self, fd: Any, callback: Callable[..., object], *args: Any) -> Any:
        self.leaves(_WATCHED)
        return super()._add_writer(fd, callback, *args)  # type: ignore[misc]

    async def subprocess_exec(self, *args: Any, **kwargs: Any) -> Any:
        self.leaves(_CHILDREN)
        return await super().subprocess_exec(*args, **kwargs)

    async def subprocess_shell(self, *args: Any, **kwargs: Any) -> Any:
        self.leaves(_CHILDREN)
        return await super().subprocess_shell(*args, **kwargs)


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


def _perf_ns() -> int:
    # livekit-agents orders the speeches it schedules at one instant by this read.
    loop = _virtual_loop()
    return loop.read_ns() if loop is not None else _REAL_PERF_NS()


def _monotonic() -> float:
    loop = _virtual_loop()
    return loop.time() if loop is not None else _REAL_MONOTONIC()


def _monotonic_ns() -> int:
    loop = _virtual_loop()
    return loop.time_us() * 1000 if loop is not None else _REAL_MONOTONIC_NS()


# The functions of the time module a run reads from the loop, and what stands in for each.
VIRTUAL_READS: dict[str, Callable[[], Any]] = {
    "time": _now,
    "time_ns": _now_ns,
    "perf_counter": _perf,
    "perf_counter_ns": _perf_ns,
    "monotonic": _monotonic,
    "monotonic_ns": _monotonic_ns,
}
_VIRTUAL_FOR: dict[Any, Callable[[], Any]] = {
    getattr(time, name): virtual for name, virtual in VIRTUAL_READS.items()
}


@dataclass
class ClockPatchReport:
    """What the patch covers in this process. Recorded in the run manifest."""

    # Model fields whose default captured a real clock, now read from the virtual one.
    model_fields: list[str] = field(default_factory=list)
    # Such fields that could not be patched. A run with any is not valid evidence.
    unpatched: list[str] = field(default_factory=list)
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


_PATCHED: dict[type[BaseModel], tuple[Any, Any]] = {}
_LOOKED_AT: weakref.WeakSet[type[BaseModel]] = weakref.WeakSet()
_REPORT = ClockPatchReport()


def _models() -> Iterator[type[BaseModel]]:
    from pydantic import BaseModel

    seen: set[type[BaseModel]] = set()
    stack: list[type[BaseModel]] = [BaseModel]
    while stack:
        for model in stack.pop().__subclasses__():
            if model not in seen:
                seen.add(model)
                stack.append(model)
                yield model


def _patch_new_models() -> None:
    # A field with default_factory=time.time holds the real function: patching time.time misses it.
    if sys.modules.get("pydantic") is None:
        return  # no model exists before pydantic is imported
    from pydantic_core import SchemaValidator

    unpatched = []
    for model in _models():
        if model in _LOOKED_AT:
            continue
        fields = {
            name: _VIRTUAL_FOR[info.default_factory]
            for name, info in model.model_fields.items()
            if info.default_factory in _VIRTUAL_FOR
        }
        names = [f"{model.__module__}.{model.__qualname__}.{name}" for name in fields]
        if fields:
            try:
                # A model whose build pydantic deferred has no validator to replace yet.
                if not model.__pydantic_complete__:
                    model.model_rebuild()
                schema = _to_plain(model.__pydantic_core_schema__)
                node = _model_node(schema)
                for name, factory in fields.items():
                    node["schema"]["fields"][name]["schema"]["default_factory"] = factory
                patched = SchemaValidator(schema, node.get("config"), _use_prebuilt=False)
            except Exception:  # noqa: BLE001  (whatever the reason, the field reads a real clock)
                unpatched += names
                continue  # looked at again on the next run, in case it can be built then
            _PATCHED[model] = (patched, model.__pydantic_validator__)
            _REPORT.model_fields = sorted([*_REPORT.model_fields, *names])
        _LOOKED_AT.add(model)
    _REPORT.unpatched = sorted(unpatched)


def patch_report() -> ClockPatchReport:
    """What the patch covers in this process, as of the last run that was started."""
    return _REPORT


def _point_factories(model: type[BaseModel], at: Mapping[Any, Any]) -> None:
    # model_construct builds a default from the field itself, without the validator.
    for info in model.__pydantic_fields__.values():
        if info.default_factory in at:
            info.default_factory = at[info.default_factory]


@contextlib.contextmanager
def _patched_model_clocks() -> Iterator[None]:
    _patch_new_models()
    real_for = {virtual: real for real, virtual in _VIRTUAL_FOR.items()}
    try:
        for model, (patched, _original) in _PATCHED.items():
            model.__pydantic_validator__ = patched
            _point_factories(model, _VIRTUAL_FOR)
        yield
    finally:
        for model, (_patched, original) in _PATCHED.items():
            model.__pydantic_validator__ = original
            _point_factories(model, real_for)


@contextlib.contextmanager
def virtual_wall_clock() -> Iterator[ClockPatchReport]:
    """Point wall-clock reads at the running VirtualTimeLoop for the duration of the block."""
    with contextlib.ExitStack() as stack:
        for name, virtual in VIRTUAL_READS.items():
            stack.enter_context(mock.patch.object(time, name, virtual))
        try:
            from opentelemetry.sdk import trace as otel_sdk_trace

            if hasattr(otel_sdk_trace, "time_ns"):
                # A span reads the clock as it starts and ends, and tracing may be off.
                spans = _reading_without_a_step(_now_ns)
                stack.enter_context(mock.patch.object(otel_sdk_trace, "time_ns", spans))
                _REPORT.otel_patched = True
        except ImportError:
            pass
        stack.enter_context(_patched_model_clocks())
        yield _REPORT


def _reading_without_a_step(call: Callable[..., T]) -> Callable[..., T]:
    def held(*args: Any, **kwargs: Any) -> T:
        loop = _virtual_loop()
        if loop is None:
            return call(*args, **kwargs)
        loop._virtual_clock.held += 1
        try:
            return call(*args, **kwargs)
        finally:
            loop._virtual_clock.held -= 1

    return held


@contextlib.contextmanager
def _logging_off_the_clock() -> Iterator[None]:
    # A log record reads the clock as it is made, and the logging level decides how many are.
    make = logging.getLogRecordFactory()
    handle = _reading_without_a_step(logging.Logger.handle)
    logging.setLogRecordFactory(_reading_without_a_step(make))
    try:
        with mock.patch.object(logging.Logger, "handle", handle):
            yield
    finally:
        logging.setLogRecordFactory(make)


@contextlib.contextmanager
def _threads_counted() -> Iterator[None]:
    # A thread started from the loop works in real time, whether or not it ever wakes the loop.
    real = threading.Thread.start

    def start(thread: threading.Thread) -> None:
        loop = _virtual_loop()
        if loop is not None:
            loop.leaves(_STARTED)
        real(thread)

    def counted(raw: Callable[..., Any]) -> Callable[..., Any]:
        def start_raw(*args: Any, **kwargs: Any) -> Any:
            loop = _virtual_loop()
            if loop is not None:
                loop.leaves(_STARTED)
            return raw(*args, **kwargs)

        return start_raw

    # threading keeps its own reference to these, so a Thread is not counted twice.
    raw = [name for name in _RAW_STARTS if hasattr(_thread, name)]
    with contextlib.ExitStack() as patched:
        patched.enter_context(mock.patch.object(threading.Thread, "start", start))
        for name in raw:
            patched.enter_context(mock.patch.object(_thread, name, counted(getattr(_thread, name))))
        yield


# The ways to start a thread without threading.Thread, on the Pythons Earful runs on.
_RAW_STARTS = ("start_new_thread", "start_new", "start_joinable_thread")


def left_the_clock(loop: asyncio.AbstractEventLoop) -> list[str]:
    """Why a run on ``loop`` did not stay on the virtual clock. Empty if it did."""
    return loop.left() if isinstance(loop, VirtualTimeLoop) else []


@contextlib.contextmanager
def _leaving_no_trace() -> Iterator[None]:
    # Refused before a loop or a coroutine exists, so that nothing is left to warn about.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "an Earful run starts its own event loop and cannot be started from inside a "
            "running one: call it from a function that is not async"
        )
    seeded = random.getstate()  # a run seeds the random module with its own seed
    try:
        yield
    finally:
        random.setstate(seeded)


def _finish(loop: asyncio.AbstractEventLoop) -> None:
    # What asyncio.run does at its end. The loop was never made the thread's current one.
    try:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
    finally:
        loop.close()


def run_virtual(main: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Run ``main()`` to completion on a fresh VirtualTimeLoop with the wall clock patched."""
    with _leaving_no_trace():
        loop = VirtualTimeLoop()
        try:
            with virtual_wall_clock(), _logging_off_the_clock(), _threads_counted():
                return loop.run_until_complete(main())
        finally:
            _finish(loop)


def run_real(main: Callable[[], Coroutine[Any, Any, T]], *, timeout: float) -> T:
    """Run ``main()`` on a fresh loop on the real clock, for at most ``timeout`` seconds."""
    with _leaving_no_trace():
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(asyncio.wait_for(main(), timeout))
        finally:
            _finish(loop)


def now_us() -> int:
    """The running loop's time in integer microseconds."""
    loop = asyncio.get_running_loop()
    if isinstance(loop, VirtualTimeLoop):
        return loop.time_us()
    return round(loop.time() * _TICKS_PER_SECOND)  # real-clock control runs


QUIET_FOR_US = 1_000_000


async def quiet(busy: Callable[[], bool]) -> None:
    """Wait until ``busy()`` has been false for a second, read every tenth of a second."""
    since = now_us()
    while now_us() - since < QUIET_FOR_US:
        await asyncio.sleep(0.1)
        if busy():
            since = now_us()
