"""The operations a run needs from its parties and its framework adapter."""

from __future__ import annotations

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


class FinalHistory(TypedDict):
    """The fields of history.final, including where the adapter read the items."""

    read_from: str
    items: list[dict[str, Any]]


WaitForQuiet = Callable[[], Awaitable[None]]


@dataclass
class RunPorts:
    """One framework instance with its bound operations and live observations."""

    config: Callable[[], dict[str, Any]]
    start: Callable[[], Awaitable[None]]
    drive: Callable[[WaitForQuiet], Awaitable[None]]
    history: Callable[[], FinalHistory]
    close: Callable[[], Awaitable[None]]
    quiet: Callable[[], bool]
    closed: Callable[[], Awaitable[str | None]]


CaseT_contra = TypeVar("CaseT_contra", contravariant=True)


class AdapterPorts(Protocol[CaseT_contra]):
    """Prepare a fresh run and restore its application observers on every exit."""

    def __call__(
        self, case: CaseT_contra, context: RunContext, /
    ) -> AbstractAsyncContextManager[RunPorts]: ...
