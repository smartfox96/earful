"""What keeps a run alone in its process, and counts the threads it starts."""

from __future__ import annotations

import _thread
import asyncio
import contextlib
import random
import threading
from collections.abc import Callable, Iterator
from typing import Any
from unittest import mock

# The kind of work a VirtualTimeLoop counts for a thread started from it.
STARTED = "threads started"
# The ways to start a thread without threading.Thread, on the Pythons Earful runs on.
_RAW_STARTS = ("start_new_thread", "start_new", "start_joinable_thread")
# The clock patches and the random module belong to the process, so one run holds them.
_RUNNING = threading.Lock()


@contextlib.contextmanager
def alone() -> Iterator[None]:
    """Refuse a run inside a running loop or beside another run, and restore ``random``."""
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
    if not _RUNNING.acquire(blocking=False):
        raise RuntimeError(
            "another Earful run is in progress on another thread: a process runs one case "
            "at a time, so give each thread a process of its own"
        )
    seeded = random.getstate()  # a run seeds the random module with its own seed
    try:
        yield
    finally:
        random.setstate(seeded)
        _RUNNING.release()


def _leaves() -> Callable[[str], None] | None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    leaves: Callable[[str], None] | None = getattr(loop, "leaves", None)
    return leaves


@contextlib.contextmanager
def threads_counted() -> Iterator[None]:
    """A thread started from the loop works in real time, whether or not it wakes the loop."""
    real = threading.Thread.start

    def start(thread: threading.Thread) -> None:
        leaves = _leaves()
        if leaves is not None:
            leaves(STARTED)
        real(thread)

    def counted(raw: Callable[..., Any]) -> Callable[..., Any]:
        def start_raw(*args: Any, **kwargs: Any) -> Any:
            leaves = _leaves()
            if leaves is not None:
                leaves(STARTED)
            return raw(*args, **kwargs)

        return start_raw

    # threading keeps its own reference to these, so a Thread is not counted twice.
    raw = [name for name in _RAW_STARTS if hasattr(_thread, name)]
    with contextlib.ExitStack() as patched:
        patched.enter_context(mock.patch.object(threading.Thread, "start", start))
        for name in raw:
            patched.enter_context(mock.patch.object(_thread, name, counted(getattr(_thread, name))))
        yield
