"""The application's own work still running when a run ends: its tasks and its timers."""

from __future__ import annotations

import asyncio
import functools
import sysconfig
from pathlib import Path
from typing import Any

# Code in these is the framework's, Earful's or Python's, never the application's.
_NOT_THE_APPLICATION = tuple(
    {
        str(Path(sysconfig.get_paths()[name]).resolve())
        for name in ("stdlib", "platstdlib", "purelib", "platlib")
    }
    | {str(Path(__file__).resolve().parents[2])}
)


def _application(code: Any) -> bool:
    filename = getattr(code, "co_filename", None)
    if not isinstance(filename, str) or filename.startswith("<"):
        return False
    where = str(Path(filename).resolve())
    return not where.startswith(_NOT_THE_APPLICATION) and "-packages" not in where


def _code(callback: Any) -> Any:
    while isinstance(callback, functools.partial):
        callback = callback.func
    callback = getattr(callback, "__func__", callback)
    return getattr(callback, "__code__", None)


def left_running(loop: asyncio.AbstractEventLoop) -> list[str]:
    """The application's tasks and timers that have not finished, by name."""
    current = asyncio.current_task()
    names = []
    for task in asyncio.all_tasks(loop):
        coro = task.get_coro()
        if task is not current and not task.done() and _application(getattr(coro, "cr_code", None)):
            names.append(f"task {getattr(coro, '__qualname__', type(coro).__name__)}")
    for timer in getattr(loop, "_scheduled", ()):
        callback = getattr(timer, "_callback", None)
        if not timer.cancelled() and _application(_code(callback)):
            names.append(f"timer {getattr(callback, '__qualname__', type(callback).__name__)}")
    return sorted(names)
