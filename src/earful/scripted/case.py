"""What a scenario declares in any framework: intents, delays and faults."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class InvalidRun(RuntimeError):
    """The run finished but may not be used as evidence. ``result`` holds its ledger."""

    def __init__(self, reasons: list[str], result: Any) -> None:
        super().__init__("run is invalid: " + "; ".join(reasons))
        self.reasons = reasons
        self.result = result


@dataclass(frozen=True)
class Intent:
    """A logical action the user wants done, carried out by the named tools."""

    intent: str
    tools: tuple[str, ...] = ()
    authorized: int = 1  # durable effects the scenario allows for this intent


DELAYS = ("llm.ttft", "llm.duration", "tts.ttfb", "backend.latency")


@dataclass(frozen=True)
class Delay:
    """A provider delay the scenario declares as data, in seconds.
    ``of`` is one of ``DELAYS``; ``nth`` is the occurrence, counted from 1."""

    of: str
    nth: int
    seconds: float


@dataclass(frozen=True)
class Fault:
    """A fault the scenario declares for backend write ``nth``, counted from 1.
    A ``backend.timeout`` write still commits; ``backend.error`` and ``backend.drop`` do not."""

    of: str
    nth: int
    seconds: float | None = None  # how long the caller waits; not used by backend.error
