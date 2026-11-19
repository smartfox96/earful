"""What a scenario declares in any framework: intents, delays and faults."""

from __future__ import annotations

import dataclasses
import itertools
from dataclasses import dataclass
from typing import Any, Protocol, get_args

from ..core.backend import FAULTS, BackendMode, Effect
from ..core.checker.policy import parse_all
from ..core.timebase import to_us
from .world import UserSpeech


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


class CaseData(Protocol):
    speeches: list[UserSpeech]
    contracts: dict[str, dict[str, Any]]
    delays: list[Delay]
    faults: list[Fault]
    intents: list[Intent]
    hangup: float | None
    backend_mode: BackendMode


def check_case(case: CaseData) -> None:
    """Reject contradictory speeches, contracts and backend declarations."""
    parse_all(case.contracts)  # raises PolicyError, a ValueError, on anything undefined
    ordered = sorted(case.speeches, key=lambda s: s.start)
    for speech in ordered:
        if speech.end_us <= speech.start_us:
            raise ValueError(
                f"the speech at {speech.start} s ends at {speech.end} s; a speech ends after "
                "it starts"
            )
    modes = get_args(BackendMode)
    if case.backend_mode not in modes:
        raise ValueError(f"unknown backend mode {case.backend_mode!r}; choose from {list(modes)}")
    for before, after in itertools.pairwise(ordered):
        if before.end > after.start:
            raise ValueError(f"the speeches at {before.start} s and {after.start} s overlap")
    seen: set[tuple[str, int]] = set()
    for delay in case.delays:
        if delay.of not in DELAYS:
            raise ValueError(f"unknown delay {delay.of!r}; choose from {list(DELAYS)}")
        if delay.nth < 1 or delay.seconds < 0:
            raise ValueError(
                f"delay {delay.of} #{delay.nth}: nth counts from 1 and a delay cannot be negative"
            )
        if (delay.of, delay.nth) in seen:
            raise ValueError(f"delay {delay.of} #{delay.nth} is declared more than once")
        seen.add((delay.of, delay.nth))
    faulted: set[int] = set()
    for fault in case.faults:
        if fault.of not in FAULTS:
            raise ValueError(f"unknown fault {fault.of!r}; choose from {list(FAULTS)}")
        waits = fault.of != "backend.error"
        if (
            fault.nth < 1
            or (waits and (fault.seconds is None or fault.seconds < 0))
            or (not waits and fault.seconds is not None)
        ):
            raise ValueError(
                f"fault {fault.of} #{fault.nth}: nth counts from 1; a timeout or a drop says "
                "how long the caller waits, and an error does not"
            )
        if fault.nth in faulted:
            raise ValueError(f"backend write #{fault.nth} has more than one fault")
        faulted.add(fault.nth)
    if case.hangup is not None:
        if case.hangup < 0 or not case.speeches:
            raise ValueError(
                "the user hangs up after their last speech; a user who never speaks cannot"
            )
        late = [speech.start for speech in case.speeches if speech.end_us > to_us(case.hangup)]
        if late:
            raise ValueError(
                f"the user hangs up at {case.hangup} s, before the speech at {late[0]} s is over"
            )
    names = [it.intent for it in case.intents]
    if len(set(names)) != len(names):
        raise ValueError("an intent is declared more than once")


def declared_intents(
    explicit: list[Intent], effects: dict[str, Effect], speeches: list[UserSpeech]
) -> list[Intent]:
    """Include each effect's tool and check which intents the scripted user may retire."""
    intents = {it.intent: it for it in explicit}
    for tool, declared in sorted(effects.items()):
        known = intents.get(declared.intent, Intent(declared.intent))
        if tool not in known.tools:
            known = dataclasses.replace(known, tools=(*known.tools, tool))
        intents[declared.intent] = known
    for speech in speeches:
        undeclared = sorted(set(speech.retires) - intents.keys())
        if undeclared:
            raise ValueError(f"speech at {speech.start} s retires undeclared intents {undeclared}")
    return list(intents.values())
