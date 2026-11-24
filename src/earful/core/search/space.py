"""The moments, variables and placements a search can try."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .. import case as cases
from ..case import Variable
from .runner import Event

# One 10 ms audio frame either side: the finest step at which a framework is given audio.
OFFSETS_US = (-10_000, 0, 10_000)

# The longest a search will make a provider take.
DELAY_LIMIT_US = 10_000_000

# Longer than the default interruption threshold, so a speech can interrupt a stretched delay.
STRETCH_US = 1_500_000
STRETCHED = ("backend.latency", "llm.duration")

_NOT_TRANSITIONS = {"run.start", "run.end", "user.origin", "history.final", "fw.close"}


@dataclass(frozen=True)
class Transition:
    """An instant at which something in the run changed, and what did."""

    t_us: int
    kinds: tuple[str, ...]


def transitions(events: list[Event], *, words: bool = True) -> list[Transition]:
    """Every instant of a run that something else could race with, in time order."""
    header = events[0]["data"]["scenario"]
    end_of_turn = turn_end_delay(header)
    origin = next((e["t_us"] for e in events if e["kind"] == "user.origin"), 0)
    at: dict[int, list[str]] = {}

    def mark(t_us: int, kind: str) -> None:
        kinds = at.setdefault(t_us, [])
        if kind not in kinds:
            kinds.append(kind)

    utterances = {
        e["data"]["utterance"]: e["data"] for e in events if e["kind"] == "tts.synthesized"
    }
    for e in events:
        kind, d, t = e["kind"], e["data"], e["t_us"]
        if kind in _NOT_TRANSITIONS:
            continue
        mark(t, kind)
        if kind == "llm.response":
            mark(t + d["ttft_us"], "llm.first_token")
            mark(t + d["duration_us"], "llm.done")
        elif kind == "tts.synthesized":
            mark(d["started_us"] + d["ttfb_us"], "tts.first_audio")
        elif kind == "playout.started" and d["utterance"] in utterances:
            said = utterances[d["utterance"]]
            samples, rate = said["word_samples"], said["sample_rate"]
            middle = len(samples) // 2
            mark(
                t + (sum(samples[:middle]) + samples[middle] // 2) * 1_000_000 // rate,
                "playout.mid_word",
            )
        elif kind == "playout.finished" and words:
            for done in d["words_completed_us"]:
                mark(done, "playout.word_end")
    for speech in header["speeches"]:
        mark(origin + speech["end_us"] + end_of_turn, "user.turn_end")
    return [Transition(t, tuple(kinds)) for t, kinds in sorted(at.items())]


def turn_end_delay(header: dict[str, Any]) -> int:
    """How long after a speech ends the framework ends the turn, as the adapter declares it.
    A ledger from before the field has LiveKit's: the longer of the two waits."""
    vad, session = header["providers"]["vad"], header["session"]
    declared: int | None = session.get("turn_end_delay_us")
    if declared is not None:
        return declared
    return int(max(vad["min_silence_us"], session["min_endpointing_delay_us"]))


def anchors(header: dict[str, Any], speech: dict[str, Any]) -> dict[str, int]:
    """The moments at which a speech acts on the system, as microseconds after its start.
    ``header`` is the ``scenario`` of a ledger's ``run.start``."""
    vad, session = header["providers"]["vad"], header["session"]
    length = speech["end_us"] - speech["start_us"]
    out = {"vad_start": vad["min_speech_us"]}
    if length >= session["min_interruption_duration_us"]:
        out["interrupt"] = session["min_interruption_duration_us"]
    if speech["transcript"]:
        out["transcript"] = length + speech["stt_delay_us"]
    out["vad_end"] = length + vad["min_silence_us"]
    out["turn_end"] = length + turn_end_delay(header)
    return out


# Which of a speech's moments each of its variables moves.
_MOVED_BY = {
    "speech.position": ("vad_start", "interrupt", "transcript", "vad_end", "turn_end"),
    "speech.end": ("transcript", "vad_end", "turn_end"),
    "speech.stt_delay": ("transcript",),
}


@dataclass(frozen=True)
class Handle:
    """One moment a case controls, and the variable that moves it."""

    variable: Variable
    moment: str  # "interrupt", "transcript", ... for a speech; "done" for a delay
    t_us: int
    value_us: int

    @property
    def name(self) -> str:
        return f"{self.variable} {self.moment}"


def speech_handles(
    case: cases.Case, index: int, events: list[Event], kinds: Iterable[str]
) -> list[Handle]:
    """The handles of one speech. ``events`` is any ledger of the scenario."""
    speech = case["speeches"][index]
    origin = next((e["t_us"] for e in events if e["kind"] == "user.origin"), 0)
    points = anchors(events[0]["data"]["scenario"], speech)
    out = []
    for kind in kinds:
        variable = Variable(kind, index)
        current = cases.value(case, variable)
        assert current is not None
        for moment in _MOVED_BY[kind]:
            if moment in points:
                out.append(
                    Handle(variable, moment, origin + speech["start_us"] + points[moment], current)
                )
    return out


def delay_handles(events: list[Event], kinds: Iterable[str]) -> list[Handle]:
    """The handles of every provider delay the run applied, as the ledger records them."""
    wanted, out = set(kinds), []
    for e in events:
        kind, d, t = e["kind"], e["data"], e["t_us"]
        found: list[tuple[str, int, int]] = []
        if kind == "llm.response":
            found = [
                ("llm.ttft", d["call"], d["ttft_us"]),
                ("llm.duration", d["call"], d["duration_us"]),
            ]
        elif kind == "tts.synthesized":
            found = [("tts.ttfb", d["utterance"], d["ttfb_us"])]
            t = d["started_us"]
        elif kind == "effect.requested":
            found = [("backend.latency", d["request"], d["latency_us"])]
        out += [
            Handle(Variable(of, nth), "done", t + us, us) for of, nth, us in found if of in wanted
        ]
    return out


def hangup_handle(case: cases.Case, events: list[Event]) -> Handle:
    """The hangup's handle. A user who stays has it at the origin, so moving it declares one."""
    origin = next((e["t_us"] for e in events if e["kind"] == "user.origin"), 0)
    at = case["hangup_us"] or 0
    return Handle(Variable(cases.HANGUP, 0), "hangup", origin + at, at)


@dataclass(frozen=True)
class Placement:
    """Why a case was tried: so that ``handle`` fell ``offset_us`` from a transition."""

    handle: Handle
    transition: Transition
    offset_us: int


@dataclass
class Candidate:
    case: cases.Case
    variable: Variable | None  # the variable that was moved; None if nothing was
    value_us: int | None  # what it was moved to
    placements: list[Placement] = field(default_factory=list)
    base: str = ""  # what was changed before anything was moved: a fault, a stretch, a profile

    @property
    def what(self) -> str:
        """What was changed, for a person, such as ``backend.drop 1 + speech.position 1``."""
        return " + ".join(
            part for part in (self.base, str(self.variable) if self.variable else "") if part
        )


def candidates(
    case: cases.Case,
    handles: Iterable[Handle],
    targets: list[Transition],
    *,
    offsets_us: Iterable[int] = OFFSETS_US,
    delay_limit_us: int = DELAY_LIMIT_US,
    base: str = "",
    apart: bool = False,
) -> tuple[list[Candidate], list[Placement]]:
    """Every case that puts one of ``handles`` at an offset from one of ``targets``, and the
    placements for which no case exists. Cases that coincide are one candidate. ``apart``
    says the targets are of a run without the handles, so that a target at the instant of a
    handle is something else and the handle is put either side of it. Otherwise it is the
    handle's own doing, and no target."""
    found: dict[str, Candidate] = {}
    impossible: list[Placement] = []
    offsets = tuple(offsets_us)
    for handle in handles:
        for transition in targets:
            there = transition.t_us == handle.t_us
            if there and not apart:
                continue
            for offset in offsets:
                if there and offset == 0:
                    continue  # where it already is
                placement = Placement(handle, transition, offset)
                to = handle.value_us + transition.t_us + offset - handle.t_us
                waits = handle.variable.kind in (*cases.DELAY_KINDS, cases.WAIT)
                too_long = waits and to > max(delay_limit_us, handle.value_us)
                moved = None if too_long else cases.with_value(case, handle.variable, to)
                if moved is None:
                    impossible.append(placement)
                    continue
                key = cases.canonical(moved)
                if key not in found:
                    found[key] = Candidate(moved, handle.variable, to, base=base)
                found[key].placements.append(placement)
    return list(found.values()), impossible
