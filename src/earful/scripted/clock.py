"""Virtual time for adapter runs, including the event loop and wall-clock reads.
Adapted from ``tests/virtual_time.py`` in livekit/agents (Apache-2.0, see NOTICE)."""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import logging
import sys
import threading
import time
import weakref
from collections.abc import Callable, Coroutine, Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar
from unittest import mock

from . import ceilings
from .ceilings import _AutojumpSelector, finish
from .guards import STARTED, alone, threads_counted
from .timers import (
    _HALF_TICK,
    _TICKS_PER_SECOND,
    VirtualTimeStall,  # noqa: F401 - public clock exception
    _OrderedTimerHandle,
    _VirtualClock,
)

if TYPE_CHECKING:
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

# The kinds of work a loop cannot move in virtual time. Any of it makes a run invalid.
_STARTED, _WOKEN = STARTED, "wake-ups from another thread"
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
        selector = _AutojumpSelector(self._virtual_clock, self._next_due, ceilings.STALL_SECONDS)
        super().__init__(selector=selector)
        # A timer is ready when its due time is within half a tick of the clock.
        self._clock_resolution = _HALF_TICK
        self._timer_order = 0
        self.executor_calls = 0

    def time(self) -> float:
        return self._virtual_clock.time()

    def _next_due(self) -> float | None:
        due = [timer.when() for timer in self._scheduled if not timer.cancelled()]
        return min(due, default=None)

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


def left_the_clock(loop: asyncio.AbstractEventLoop) -> list[str]:
    """Why a run on ``loop`` did not stay on the virtual clock. Empty if it did."""
    return loop.left() if isinstance(loop, VirtualTimeLoop) else []


def run_virtual(main: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Run ``main()`` to completion on a fresh VirtualTimeLoop with the wall clock patched."""
    with alone():
        loop = VirtualTimeLoop()
        try:
            with virtual_wall_clock(), _logging_off_the_clock(), threads_counted():
                return loop.run_until_complete(main())
        finally:
            finish(loop)


def run_real(main: Callable[[], Coroutine[Any, Any, T]], *, timeout: float) -> T:
    """Run ``main()`` on a fresh loop on the real clock, for at most ``timeout`` seconds."""
    with alone():
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(asyncio.wait_for(main(), timeout))
        finally:
            finish(loop)


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
