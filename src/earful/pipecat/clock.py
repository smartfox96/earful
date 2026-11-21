"""Bind Pipecat’s clock to the virtual event loop."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Awaitable, Iterator
from typing import TypeVar
from unittest import mock

from pipecat.clocks.base_clock import BaseClock

from ..scripted.clock import now_us

T = TypeVar("T")


class VirtualClock(BaseClock):
    """Pipecat's own clock, read from the event loop. Pipecat uses it to stamp frames and
    to release frames that carry a presentation time."""

    def __init__(self) -> None:
        self._t0_us: int | None = None

    def start(self) -> None:
        self._t0_us = now_us()

    def get_time(self) -> int:
        return 0 if self._t0_us is None else (now_us() - self._t0_us) * 1000  # nanoseconds


async def _wait_for(awaitable: Awaitable[T], timeout: float | None) -> T:
    """``asyncio.wait_for`` as Python 3.12 made it: the awaitable runs in the calling task."""
    if timeout is not None and timeout <= 0:
        task = asyncio.ensure_future(awaitable)
        if not task.done():
            task.cancel()
            await asyncio.wait([task])
        try:
            return task.result()
        except asyncio.CancelledError as exc:
            raise TimeoutError from exc
    async with asyncio.timeout(timeout):  # type: ignore[attr-defined]  # mypy reads as 3.10
        return await awaitable


# On 3.11 pipecat-ai installs wait_for2's, whose extra loop steps reorder one instant's tasks.
_WAIT_FOR = asyncio.tasks.wait_for if sys.version_info >= (3, 12) else _wait_for


@contextlib.contextmanager
def wait_for_of_python_3_12() -> Iterator[None]:
    """``asyncio.wait_for`` is the standard library's of Python 3.12 and later in the block."""
    with mock.patch.object(asyncio, "wait_for", _WAIT_FOR):
        yield
