"""The operations a run needs from its parties and its framework adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, TypedDict, TypeVar

if TYPE_CHECKING:
    from .run import RunContext


class Party(Protocol):
    """The state the kernel can observe while an adapter drives a scripted party."""

    def quiet(self) -> bool:
        """Whether the party has finished its current work at this instant."""
        ...


class ScriptedInput(Party, Protocol):
    done: asyncio.Future[None]

    @property
    def last_us(self) -> int:
        """The last input deadline, relative to the scenario origin."""
        ...


class FinalHistory(TypedDict):
    """The fields of history.final, including where the adapter read the items."""

    read_from: str
    items: list[dict[str, Any]]


@dataclass(frozen=True)
class Ended:
    """When the framework ended the conversation, and the reason it gave."""

    t_us: int
    reason: str


@dataclass
class RunPorts:
    """Operations on a session and the participants whose work it must finish.
    ``parties`` maps what each party is doing, while it is busy, to the party."""

    config: Callable[[], dict[str, Any]]
    start: Callable[[], Awaitable[asyncio.Future[Ended]]]  # resolves when the framework ends
    input: ScriptedInput
    parties: dict[str, Party]
    running: Callable[[], list[str]]  # the tools still running, by name
    drain: Callable[[], Awaitable[None]]  # asks the framework to finish its work and end
    disconnect: Callable[[], Awaitable[None]]  # the user leaves
    history: Callable[[], FinalHistory]
    close: Callable[[], Awaitable[None]]  # stops whatever still runs and releases the run


CaseT_contra = TypeVar("CaseT_contra", contravariant=True)


class AdapterPorts(Protocol[CaseT_contra]):
    """Prepare a fresh run and restore its application observers on every exit."""

    def __call__(
        self, case: CaseT_contra, context: RunContext, /
    ) -> AbstractAsyncContextManager[RunPorts]: ...
