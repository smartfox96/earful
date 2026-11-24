"""The operations a run needs from its parties and its framework adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Iterable
from contextlib import AbstractContextManager
from typing import Any, Protocol, TypedDict, TypeVar


class Party(Protocol):
    """The state the kernel can observe while an adapter drives a scripted party."""

    def quiet(self) -> bool:
        """Whether the party has finished its current work at this instant."""
        ...


class FinalHistory(TypedDict):
    """The fields of history.final, including where the adapter read the items."""

    read_from: str
    items: list[dict[str, Any]]


class RunPorts(Protocol):
    """One built session or pipeline, observed on the run's clock."""

    async def start(self) -> None:
        """Start processing and return once the framework can take input."""
        ...

    def quiet(self) -> bool:
        """Whether the framework and its outstanding tool calls are idle right now."""
        ...

    def closed(self) -> Awaitable[str]:
        """Resolve with the framework's close reason when it ends on its own."""
        ...

    def config(self) -> dict[str, Any]:
        """Read the effective configuration using the run header's field names."""
        ...

    def history(self) -> FinalHistory:
        """Read the framework's final memory in ledger order."""
        ...

    async def close(self) -> None:
        """Finish shutdown within the case's drain timeout."""
        ...


CaseT_contra = TypeVar("CaseT_contra", contravariant=True)
PartiesT_contra = TypeVar("PartiesT_contra", bound=Iterable[Party], contravariant=True)


class AdapterPorts(Protocol[CaseT_contra, PartiesT_contra]):
    """An adapter bound to one run's ledger and witnesses."""

    def refusals(self, case: CaseT_contra, /) -> Iterable[str]:
        """Give a one-line reason for each unsupported option visible before build."""
        ...

    def application_seams(self) -> AbstractContextManager[None]:
        """Install the adapter's application observers until the context exits."""
        ...

    async def build(self, case: CaseT_contra, parties: PartiesT_contra, /) -> RunPorts:
        """Wire the parties to a fresh framework instance through the adapter's shims."""
        ...
