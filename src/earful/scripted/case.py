"""What a scenario declares in any framework: intents, delays and faults."""

from __future__ import annotations

import dataclasses
import itertools
from dataclasses import dataclass, field
from typing import Any, get_args

from ..core import case_rules as rules
from ..core.backend import FAULTS, BackendMode, Effect
from ..core.checker.policy import parse_all
from ..core.timebase import seconds, to_us
from .model import Policy
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


@dataclass
class Application:
    """The application factory supplied by an adapter has the next positional field."""

    name: str


@dataclass
class Case(Application):
    """The scripted inputs shared by every adapter."""

    speeches: list[UserSpeech]
    policy: Policy
    intents: list[Intent] = field(default_factory=list)
    # Tool name -> the durable write a call of that tool makes, in place of its body.
    effects: dict[str, Effect] = field(default_factory=dict)
    contracts: dict[str, dict[str, Any]] = field(default_factory=dict)  # id -> policy values
    delays: list[Delay] = field(default_factory=list)
    faults: list[Fault] = field(default_factory=list)
    # When the user hangs up, in seconds from the origin. No speech may end after it.
    hangup: float | None = None
    backend_mode: BackendMode = "plain"
    seconds_per_letter: float = 0.06  # the scripted voice: a five-letter word takes 0.3 s
    tts_ttfb: float = 0.2


@dataclass
class DrainOptions:
    """How long the framework has to finish after the scripted input."""

    drain_delay: float = 5.0
    drain_timeout: float = 30.0


def check_case(case: Case) -> None:
    """Reject contradictory speeches, contracts and backend declarations."""
    parse_all(case.contracts)  # raises PolicyError, a ValueError, on anything undefined
    for speech in case.speeches:
        for name in ("start", "end", "stt_delay"):
            seconds(name, getattr(speech, name))
        if not isinstance(speech.transcript, str):
            raise ValueError("speech transcript must be a string")  # noqa: TRY004
        if speech.expects_reply and not speech.transcript.strip():
            raise ValueError("expects_reply requires a speech with a transcript")
        if not isinstance(speech.retires, (list, tuple)) or not all(
            isinstance(name, str) for name in speech.retires
        ):
            raise ValueError("retires must be a sequence of intent names")
    for intent in case.intents:
        if not isinstance(intent.intent, str) or not intent.intent:
            raise ValueError("intent must be a nonempty name")
        if not isinstance(intent.tools, (list, tuple)) or not all(
            isinstance(name, str) for name in intent.tools
        ):
            raise ValueError("intent tools must be a sequence of tool names")
        if type(intent.authorized) is not int or intent.authorized < 0:
            raise ValueError("authorized must be a nonnegative integer")
    ordered = sorted(case.speeches, key=lambda s: s.start)
    for speech in ordered:
        if rules.empty_speech(speech.start_us, speech.end_us):
            raise ValueError(
                f"the speech at {speech.start} s ends at {speech.end} s; a speech ends after "
                "it starts"
            )
    modes = get_args(BackendMode)
    if case.backend_mode not in modes:
        raise ValueError(f"unknown backend mode {case.backend_mode!r}; choose from {list(modes)}")
    for before, after in itertools.pairwise(ordered):
        if rules.overlaps(before.end, after.start):
            raise ValueError(f"the speeches at {before.start} s and {after.start} s overlap")
    seen: set[tuple[str, int]] = set()
    for delay in case.delays:
        seconds("delay.seconds", delay.seconds)
        if type(delay.nth) is not int:
            raise ValueError("delay.nth must be an integer")
        if delay.of not in DELAYS:
            raise ValueError(f"unknown delay {delay.of!r}; choose from {list(DELAYS)}")
        if delay.nth < 1 or delay.seconds < 0:
            raise ValueError(
                f"delay {delay.of} #{delay.nth}: nth counts from 1 and a delay cannot be negative"
            )
        if rules.repeated((delay.of, delay.nth), seen):
            raise ValueError(f"delay {delay.of} #{delay.nth} is declared more than once")
    faulted: set[int] = set()
    for fault in case.faults:
        if fault.seconds is not None:
            seconds("fault.seconds", fault.seconds)
        if type(fault.nth) is not int:
            raise ValueError("fault.nth must be an integer")
        if fault.of not in FAULTS:
            raise ValueError(f"unknown fault {fault.of!r}; choose from {list(FAULTS)}")
        if fault.nth < 1 or rules.wrong_wait(fault.of, fault.seconds):
            raise ValueError(
                f"fault {fault.of} #{fault.nth}: nth counts from 1; a timeout or a drop says "
                "how long the caller waits, and an error does not"
            )
        if rules.repeated(fault.nth, faulted):
            raise ValueError(f"backend write #{fault.nth} has more than one fault")
    if case.hangup is not None:
        seconds("hangup", case.hangup)
        if not case.speeches:
            raise ValueError(
                "the user hangs up after their last speech; a user who never speaks cannot"
            )
        late = [
            speech.start
            for speech in case.speeches
            if rules.after_hangup(speech.end_us, to_us(case.hangup))
        ]
        if late:
            raise ValueError(
                f"the user hangs up at {case.hangup} s, before the speech at {late[0]} s is over"
            )
    names = [it.intent for it in case.intents]
    if len(set(names)) != len(names):
        raise ValueError("an intent is declared more than once")


def check_intent_tools(intents: list[Intent], tools: set[str]) -> None:
    unknown = sorted({name for intent in intents for name in intent.tools} - tools)
    if unknown:
        raise ValueError(f"intents name unknown tools {unknown}")


def declared_intents(
    explicit: list[Intent],
    effects: dict[str, Effect],
    speeches: list[UserSpeech],
    contracts: dict[str, dict[str, Any]],
) -> list[Intent]:
    """Include each effect's tool and check which intents the scripted user may retire."""
    intents = {it.intent: it for it in explicit}
    for tool, declared in sorted(effects.items()):
        known = intents.get(declared.intent, Intent(declared.intent))
        if tool not in known.tools:
            known = dataclasses.replace(known, tools=(*known.tools, tool))
        intents[declared.intent] = known
    unknown = sorted(set(contracts.get("A1", {}).get("reissue_allowed", ())) - intents.keys())
    if unknown:
        raise ValueError(f"reissue_allowed names undeclared intents {unknown}")
    for speech in speeches:
        undeclared = sorted(set(speech.retires) - intents.keys())
        if undeclared:
            raise ValueError(f"speech at {speech.start} s retires undeclared intents {undeclared}")
    return list(intents.values())
