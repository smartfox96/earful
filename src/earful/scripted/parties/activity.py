"""Live observations of work that must finish before drain."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass


class Activity:
    def __init__(self) -> None:
        self.pending = 0

    def quiet(self) -> bool:
        return self.pending == 0

    @contextmanager
    def working(self) -> Iterator[None]:
        self.pending += 1
        try:
            yield
        finally:
            self.pending -= 1


@dataclass
class Observed:
    """Activity already tracked by the framework or a witness."""

    busy: Callable[[], bool]

    def quiet(self) -> bool:
        return not self.busy()


class Schedule:
    """A scripted input stays active until it delivers its last event."""

    def __init__(self) -> None:
        self.done: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def quiet(self) -> bool:
        return self.done.done()

    def finish(self) -> None:
        if not self.done.done():
            self.done.set_result(None)
