"""The boundary scheduler: put each moment a scenario controls just before, at and just
after every moment the system under test changes state, and check each run."""

from __future__ import annotations

import contextlib
import os
import random
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, field
from functools import partial
from typing import Any

from . import case as cases
from .case import Variable
from .checker import ContractFailure, check
from .checker.report import allowed
from .ledger import ledger_hash
from .outcome import Decision, decide
from .predicate import Predicate, details_of, failures

Event = dict[str, Any]
Run = Callable[[cases.Case], list[Event]]


class CaseRefused(ValueError):
    """What a ``Run`` raises for a case its adapter will not run, before it runs anything."""


# One 10 ms audio frame either side: the finest step at which a framework is given audio.
OFFSETS_US = (-10_000, 0, 10_000)

# The longest a search will make a provider take.
DELAY_LIMIT_US = 10_000_000

# Longer than the default interruption threshold, so a speech can interrupt a stretched delay.
STRETCH_US = 1_500_000
STRETCHED = ("backend.latency", "llm.duration")

_NOT_TRANSITIONS = {"run.start", "run.end", "user.origin", "history.final", "fw.close"}


@dataclass
class Outcome:
    case: cases.Case
    failures: tuple[Predicate, ...] = ()
    outcomes: dict[str, str] = field(default_factory=dict)  # contract -> outcome
    hash: str | None = None
    missing: dict[str, tuple[str, ...]] = field(default_factory=dict)  # contract -> codes
    invalid: tuple[str, ...] = ()  # the run is not valid evidence
    refused: str | None = None  # the adapter would not run the case
    error: str | None = None

    @property
    def ran(self) -> bool:
        return self.refused is None and self.error is None and not self.invalid

    @property
    def why_not(self) -> str | None:
        """Why nothing can be concluded from the run, or None if it can be judged."""
        if self.refused is not None:
            return "refused"
        if self.error is not None:
            return "error"
        return "invalid" if self.invalid else None

    @property
    def reason(self) -> str:
        """Why not, in the words of the refusal, the error or the validity check."""
        if self.refused is not None:
            return self.refused
        return self.error if self.error is not None else "; ".join(self.invalid)


def judged(case: cases.Case, events: list[Event]) -> Outcome:
    """What the ledger of running ``case`` comes to. An invalid run has no failures."""
    report = check(events)
    if not report.valid:
        return Outcome(case, invalid=report.invalid, hash=ledger_hash(events))
    missing = {
        name: tuple(dict.fromkeys(f.code for v in r.verdicts for f in v.findings if f.code))
        for name, r in report.results.items()
    }
    return Outcome(
        case,
        tuple(sorted(failures(report))),
        report.outcomes(),
        ledger_hash(events),
        missing=missing,
    )


def attempt(run: Run, case: cases.Case) -> tuple[Outcome, list[Event] | None]:
    """Run and check a case. A refused case or a run that raises is recorded, with no ledger.
    An explicit refusal, a process exit or an operating system failure is recorded as a refusal."""
    try:
        events = run(case)
    except SystemExit as exc:
        return Outcome(case, refused=f"scenario called sys.exit({exc.code!r})"), None
    except (OSError, CaseRefused) as exc:
        return Outcome(case, refused=str(exc) or type(exc).__name__), None
    except Exception as exc:  # noqa: BLE001
        return Outcome(case, error=f"{type(exc).__name__}: {exc}"), None
    outcome = judged(case, events)
    return outcome, events if outcome.ran else None


def evaluate(run: Run, case: cases.Case) -> Outcome:
    return attempt(run, case)[0]


# How a batch of runs is carried out: ``map``, or a process pool's ``map``.
Each = Callable[..., Iterable[Any]]


class Runner:
    """Runs each case once, however often it is asked for, and counts the runs by purpose."""

    def __init__(self, run: Run) -> None:
        self.run = run
        self.runs = 0
        self.purpose = "other"  # what the runs made now are for
        self.by_purpose: dict[str, int] = {}
        self._seen: dict[str, Outcome] = {}
        self._ledgers: dict[str, list[Event] | None] = {}

    @contextlib.contextmanager
    def counting(self, purpose: str) -> Iterator[None]:
        """Count the runs made inside the block as made for ``purpose``."""
        before, self.purpose = self.purpose, purpose
        try:
            yield
        finally:
            self.purpose = before

    def _counted(self) -> None:
        self.runs += 1
        self.by_purpose[self.purpose] = self.by_purpose.get(self.purpose, 0) + 1

    def outcome(self, case: cases.Case) -> Outcome:
        return self.many([case])[0]

    def seen(self, case: cases.Case) -> bool:
        """Whether the outcome of ``case`` is known, so that asking for it runs nothing."""
        return cases.canonical(case) in self._seen

    def many(self, wanted: list[cases.Case], each: Each = map) -> list[Outcome]:
        keys = [cases.canonical(case) for case in wanted]
        todo = {key: case for key, case in zip(keys, wanted) if key not in self._seen}
        for key, outcome in zip(todo, each(partial(evaluate, self.run), list(todo.values()))):
            self._seen[key] = outcome
            self._counted()
        return [self._seen[key] for key in keys]

    def ledger(self, case: cases.Case) -> list[Event] | None:
        return self.ledgers([case])[0]

    def ledgers(self, wanted: list[cases.Case], each: Each = map) -> list[list[Event] | None]:
        """The ledger of each case, or None for a case whose run cannot be judged."""
        keys = [cases.canonical(case) for case in wanted]
        todo = {key: case for key, case in zip(keys, wanted) if key not in self._ledgers}
        attempts = each(partial(attempt, self.run), list(todo.values()))
        for key, (outcome, events) in zip(todo, attempts):
            self._ledgers[key] = events
            self._seen.setdefault(key, outcome)
            self._counted()
        return [self._ledgers[key] for key in keys]

    def breaks(self, case: cases.Case, predicate: Predicate) -> bool:
        return predicate in self.outcome(case).failures


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


@dataclass
class Window:
    """How far a variable can move while the run still breaks a rule. Each end says why
    probing stopped: ``passes`` (a run beyond it does not break the rule), ``blocked`` (no
    case beyond it exists or can be judged) or ``reach`` (the search gave up)."""

    variable: Variable
    lo_us: int
    hi_us: int
    lo_end: str
    hi_end: str
    resolution_us: int
    # Why the run beyond a blocked end could not be judged, where one exists.
    lo_unjudged: str | None = None
    hi_unjudged: str | None = None

    @property
    def width_us(self) -> int:
        return self.hi_us - self.lo_us

    @property
    def instant(self) -> bool:
        """It breaks at one value only: a matter of which of two same-instant events ran first."""
        return (
            self.width_us == 0
            and self.resolution_us == 1
            and self.lo_end == self.hi_end == "passes"
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "variable": str(self.variable),
            "lo_us": self.lo_us,
            "hi_us": self.hi_us,
            "width_us": self.width_us,
            "lo_end": self.lo_end,
            "hi_end": self.hi_end,
            "resolution_us": self.resolution_us,
            "instant": self.instant,
            **({"lo_unjudged": self.lo_unjudged} if self.lo_unjudged else {}),
            **({"hi_unjudged": self.hi_unjudged} if self.hi_unjudged else {}),
        }


class Spent(Exception):
    """The budget of runs was used up while a window was being measured."""


# Where a window ends, why probing stopped there, and why the run beyond was not judged.
_End = tuple[int, str, str | None]


def window(
    runner: Runner,
    case: cases.Case,
    variable: Variable,
    predicate: Predicate,
    *,
    resolution_us: int = 1_000,
    reach_us: int = 30_000_000,
    spent: Callable[[], bool] | None = None,
) -> Window:
    """The window around ``case`` along ``variable`` in which a run still breaks ``predicate``.
    Raises ``ValueError`` if ``case`` does not break it. Each end is located to within
    ``resolution_us``; a window narrower than that is located again to the microsecond.
    Raises ``Spent`` before a probe that needs a run once ``spent()`` is true."""
    at = cases.value(case, variable)
    if at is None or not runner.breaks(case, predicate):
        raise ValueError(f"the case does not break {predicate} with {variable} declared")

    def breaks(to: int) -> bool | str | None:
        """None where there is no case, and why where its run cannot be judged."""
        moved = cases.with_value(case, variable, to)
        if moved is None:
            return None
        if spent is not None and not runner.seen(moved) and spent():
            raise Spent
        outcome = runner.outcome(moved)
        if not outcome.ran:
            return f"{outcome.why_not}: {outcome.reason}"
        return predicate in outcome.failures

    def end(direction: int, resolution: int, reach: int) -> _End:
        """The farthest breaking value in one direction, why the search stopped, and why the
        run beyond could not be judged."""
        good, step = at, resolution
        while True:
            probe = at + direction * step
            result = breaks(probe)
            if result is not True:
                bad, stopped = probe, result
                break
            good = probe
            if step >= reach:
                return good, "reach", None
            step *= 2
        while abs(bad - good) > resolution:
            middle = (good + bad) // 2
            result = breaks(middle)
            if result is True:
                good = middle
            else:
                bad, stopped = middle, result
        if stopped is False:
            return good, "passes", None
        return good, "blocked", stopped

    def ends(resolution: int, reach: int) -> tuple[_End, _End]:
        return end(-1, resolution, reach), end(1, resolution, reach)

    (lo, lo_end, lo_why), (hi, hi_end, hi_why) = ends(resolution_us, reach_us)
    if lo == hi and resolution_us > 1:
        (lo, lo_end, lo_why), (hi, hi_end, hi_why) = ends(1, resolution_us)
        resolution_us = 1
    return Window(variable, lo, hi, lo_end, hi_end, resolution_us, lo_why, hi_why)


def _joined(one: Window, other: Window) -> Window:
    """Two measurements of one window, as one: each end where it reaches further."""
    lo = one if one.lo_us <= other.lo_us else other
    hi = one if one.hi_us >= other.hi_us else other
    return Window(
        one.variable,
        lo.lo_us,
        hi.hi_us,
        lo.lo_end,
        hi.hi_end,
        max(one.resolution_us, other.resolution_us),
        lo.lo_unjudged,
        hi.hi_unjudged,
    )


@dataclass
class Tried:
    candidate: Candidate
    outcome: Outcome


@dataclass(frozen=True)
class Break:
    """A contract rule a search found broken, and how it was reached."""

    predicate: Predicate
    candidates: int  # how many of the cases tried break it
    everywhere: bool  # it breaks as given and in every case tried: not a matter of timing
    changed: tuple[str, ...]  # what was changed in the cases that break it
    windows: tuple[Window, ...]  # how far each moved time can go while it still breaks
    unmeasured: int  # values at which it breaks that lie in no window: enough were measured
    instants_only: bool  # it breaks only where two events fall on the same instant
    example: cases.Case  # a case that breaks it, with the least changed
    what: str  # what was changed in the example
    details: tuple[str, ...]  # the checker's findings for the example


@dataclass(frozen=True)
class Plan:
    """What a search is about to run, known once the boundaries of the case are read."""

    placements: int  # ways found to put one handle beside one transition
    cases: int  # distinct cases those and the faults come to
    to_run: int  # of them, not yet run
    runs: int  # runs already made, to read the boundaries
    max_runs: int | None  # the budget, if there is one

    @property
    def covered(self) -> int:
        """How many of the cases still to run the budget leaves room for."""
        if self.max_runs is None:
            return self.to_run
        return min(self.to_run, max(self.max_runs - self.runs, 0))

    def __str__(self) -> str:
        said = (
            f"{self.cases} cases ({self.placements} placements of a time, and the faults); "
            f"{self.to_run} to run, after {self.runs} run(s) to find them"
        )
        if self.covered < self.to_run:
            said += f". The budget of {self.max_runs} runs covers {self.covered} of them"
        return said + ". Measuring where each failure starts and ends takes more runs"


@dataclass(frozen=True)
class NotJudged:
    """The cases a search could not judge for one reason."""

    why: str  # refused, error or invalid
    reason: str  # in the words of the first such case
    cases: int
    example: str  # what that case changed, and where that put it


@dataclass(frozen=True)
class Unread:
    """A run the search made to find where to put things, which could not be judged."""

    run: str  # which, such as "the case as given, without speech 1"
    outcome: Outcome

    @property
    def fails(self) -> bool:
        """A run that raised or is invalid hides the cases it would have placed."""
        return self.outcome.why_not in ("error", "invalid")


def _seconds(us: int) -> str:
    return f"{us / 1_000_000:.3f} s"


# A contract's outcomes in a case, in the order a search counts them.
_OUTCOMES = ("pass", "fail", "not_exercised", "unobservable")

_ENDS = {
    "passes": "passes beyond it",
    "blocked": "no case exists beyond it",
    "reach": "still breaks where the search stopped",
}


def _end(end: str, unjudged: str | None) -> str:
    return f"a run beyond it could not be judged, {unjudged}" if unjudged else _ENDS[end]


# What a search's runs were for, in the order its report lists them.
_PURPOSES = {
    "read": "to find where to put things",
    "cases": "for the cases",
    "measure": "to measure the failures",
    "examples": "to read an example of each",
    "other": "for other reads",
}


@dataclass
class Exploration:
    case: cases.Case
    original: Outcome  # the case as given
    tried: list[Tried]
    impossible: list[Placement]
    offsets_us: tuple[int, ...]
    runner: Runner
    max_runs: int | None = None  # the budget of runs, if the search was given one
    not_run: int = 0  # cases the budget left untried
    cut: dict[Predicate, int] = field(default_factory=dict)  # values it left unmeasured
    unread: list[Unread] = field(default_factory=list)  # runs that read where to put things

    @property
    def unmeasured(self) -> int:
        """How many breaking values the budget left unmeasured, over every rule measured."""
        return sum(self.cut.values())

    @property
    def complete(self) -> bool:
        """False if the budget was spent before every case was run and every window measured."""
        return not self.not_run and not self.unmeasured

    @property
    def runs(self) -> int:
        """The runs this search made, without the reductions of failures kept from it."""
        return self.runner.runs - self.runner.by_purpose.get("reduce", 0)

    def _spent(self) -> bool:
        return self.max_runs is not None and self.runs >= self.max_runs

    def predicates(self) -> list[Predicate]:
        """Every rule the case as given or some candidate broke."""
        tried = {p for t in self.tried for p in t.outcome.failures}
        return sorted(tried.union(self.original.failures))

    def breaking(self, predicate: Predicate) -> list[Tried]:
        return [t for t in self.tried if predicate in t.outcome.failures]

    def everywhere(self, predicate: Predicate) -> bool:
        """Whether the rule breaks in the case as given and in every candidate that ran."""
        ran = [t for t in self.tried if t.outcome.ran]
        return predicate in self.original.failures and all(
            predicate in t.outcome.failures for t in ran
        )

    def windows(
        self, predicate: Predicate, *, most: int = 3, resolution_us: int = 1_000
    ) -> list[Window]:
        """The measured windows in which the rule breaks: every instant, and at most ``most``
        wider windows per variable and base."""
        return self._measured(predicate, most, resolution_us)[0]

    def _measured(
        self, predicate: Predicate, most: int = 3, resolution_us: int = 1_000
    ) -> tuple[list[Window], int]:
        """The windows, and how many breaking values lie in none: ``most`` was reached, or
        the budget of runs was spent. An instant costs four runs and is measured whatever
        ``most`` is."""
        measured: list[tuple[str, Window]] = []
        wide: dict[tuple[str, Variable], int] = {}
        left_out = cut = 0
        moved = [t for t in self.breaking(predicate) if t.candidate.variable is not None]
        for tried in sorted(
            moved, key=lambda t: (t.candidate.base, t.candidate.variable, t.candidate.value_us)
        ):
            variable, at = tried.candidate.variable, tried.candidate.value_us
            assert variable is not None and at is not None
            key = (tried.candidate.base, variable)
            if any(
                b == tried.candidate.base and w.variable == variable and w.lo_us <= at <= w.hi_us
                for b, w in measured
            ):
                continue
            if wide.get(key, 0) >= most:
                left_out += 1
                continue
            try:
                with self.runner.counting("measure"):
                    found = window(
                        self.runner,
                        tried.candidate.case,
                        variable,
                        predicate,
                        resolution_us=resolution_us,
                        spent=self._spent,
                    )
            except Spent:
                cut += 1
                continue
            # A window is located to its resolution, so a breaking value just past the end of one
            # already measured finds the same failure again: one window, counted once.
            same = next(
                (
                    n
                    for n, (b, w) in enumerate(measured)
                    if b == tried.candidate.base
                    and w.variable == variable
                    and found.lo_us <= w.hi_us
                    and w.lo_us <= found.hi_us
                ),
                None,
            )
            if same is not None:
                measured[same] = (tried.candidate.base, _joined(measured[same][1], found))
                continue
            measured.append((tried.candidate.base, found))
            if not found.instant:
                wide[key] = wide.get(key, 0) + 1
        self.cut[predicate] = cut
        return [found for _base, found in measured], left_out + cut

    def coverage(self) -> dict[str, dict[str, Any]]:
        """For each handle, the kinds of transition it was put beside and those it never was."""
        reached: dict[str, set[str]] = {}
        missed: dict[str, dict[str, set[str]]] = {}

        def note(placement: Placement, why: str | None) -> None:
            name = placement.handle.name
            reached.setdefault(name, set())
            missed.setdefault(name, {})
            for kind in placement.transition.kinds:
                if why is None:
                    reached[name].add(kind)
                else:
                    missed[name].setdefault(kind, set()).add(why)

        for placement in self.impossible:
            note(placement, "impossible")
        for tried in self.tried:
            for placement in tried.candidate.placements:
                note(placement, tried.outcome.why_not)
        return {
            name: {
                "reached": sorted(reached[name]),
                "never_reached": {
                    kind: sorted(why)
                    for kind, why in sorted(missed[name].items())
                    if kind not in reached[name]
                },
            }
            for name in sorted(reached)
        }

    @property
    def nothing_judged(self) -> bool:
        """Whether the search judged no case: it tried none, or could judge none it tried."""
        if not self.tried:
            return not self.not_run  # cases the budget left untried make it incomplete instead
        return not any(t.outcome.ran for t in self.tried)

    @property
    def unreadable(self) -> int:
        """How many runs made to find where to put things raised or were invalid. The cases
        each would have placed were never tried, so no healthy search has one."""
        return sum(one.fails for one in self.unread)

    @property
    def errored(self) -> int:
        """How many of the cases tried raised. No healthy search has one."""
        return sum(t.outcome.error is not None for t in self.tried)

    def by_contract(self) -> dict[str, dict[str, int]]:
        """For each contract, how many of the cases judged it passed, failed, had nothing to
        judge (not exercised) or lost the evidence (unobservable)."""
        out: dict[str, dict[str, int]] = {}
        for tried in self.tried:
            for contract, outcome in tried.outcome.outcomes.items():
                counts = out.setdefault(contract, dict.fromkeys(_OUTCOMES, 0))
                counts[outcome] += 1
        return out

    def not_judged(self) -> list[NotJudged]:
        """Why cases could not be judged, the reason with the most cases first. Reasons that
        differ only in their numbers are one reason."""
        groups: dict[tuple[str, str], list[Tried]] = {}
        for tried in self.tried:
            why = tried.outcome.why_not
            if why is not None:
                alike = re.sub(r"\d+", "0", tried.outcome.reason)
                groups.setdefault((why, alike), []).append(tried)
        out = [
            NotJudged(why, same[0].outcome.reason, len(same), self._placed(same[0].candidate))
            for (why, _alike), same in groups.items()
        ]
        return sorted(out, key=lambda group: (-group.cases, group.why, group.reason))

    def breaks(self, *, windows: bool | Iterable[Predicate] = True) -> list[Break]:
        """Every rule the search found broken, with an example of each. ``windows`` says which
        failures have their width measured, which takes further runs: all, none or those named."""
        wanted = None if isinstance(windows, bool) else set(windows)
        out = []
        for predicate in self.predicates():
            breaking = self.breaking(predicate)
            measure = windows is True or (wanted is not None and predicate in wanted)
            measured, unmeasured = self._measured(predicate) if measure else ([], 0)
            unmoved = predicate in self.original.failures or any(
                t.candidate.variable is None for t in breaking
            )

            def distance(tried: Tried) -> tuple[int, int]:
                variable, to = tried.candidate.variable, tried.candidate.value_us
                if variable is None or to is None:
                    return (1 if tried.candidate.base else 0, 0)
                was = cases.value(self.case, variable)
                moved = abs(to - was) if was is not None else to
                return (2 if tried.candidate.base else 1, moved)

            if predicate in self.original.failures:
                example, what = self.case, "nothing: it breaks as written"
            else:
                nearest = min(breaking, key=distance)
                example, what = nearest.candidate.case, self._what(nearest.candidate)
            with self.runner.counting("examples"):
                ledger = self.runner.ledger(example)
            details = details_of(check(ledger), predicate) if ledger is not None else ()
            out.append(
                Break(
                    predicate=predicate,
                    candidates=len(breaking),
                    everywhere=self.everywhere(predicate),
                    changed=tuple(sorted({t.candidate.what for t in breaking})),
                    windows=tuple(measured),
                    unmeasured=unmeasured,
                    # A value left unmeasured may lie in a window: the rule is then counted.
                    instants_only=bool(measured)
                    and not unmoved
                    and not unmeasured
                    and all(w.instant for w in measured),
                    example=example,
                    what=what,
                    details=details,
                )
            )
        return out

    def _what(self, candidate: Candidate) -> str:
        if candidate.variable is None or candidate.value_us is None:
            return candidate.base
        was = cases.value(self.case, candidate.variable)
        written = f"as written {_seconds(was)}" if was is not None else "not declared as written"
        moved = f"{candidate.variable} at {_seconds(candidate.value_us)} ({written})"
        return f"{candidate.base} + {moved}" if candidate.base else moved

    def _placed(self, candidate: Candidate) -> str:
        """What a candidate changed and, where a time was moved, what that put beside what."""
        if not candidate.placements:
            return self._what(candidate)
        first = candidate.placements[0]
        offset, beside = first.offset_us, first.transition.kinds[0]
        side = f"{abs(offset) // 1000} ms {'before' if offset < 0 else 'after'}" if offset else "at"
        return f"{self._what(candidate)}, to put {first.handle.moment} {side} {beside}"

    def decision(self, found: list[Break], unjudged: dict[str, str], allow: set[str]) -> Decision:
        predicates = [one.predicate for one in found if not one.instants_only]
        return decide(
            (str(p) for p in predicates if not p.lost),
            (str(p) for p in predicates if p.lost),
            bool(unjudged) or self.no_verdict,
            allow,
        )

    def describe(
        self,
        found: list[Break] | None = None,
        unjudged: dict[str, str] | None = None,
        *,
        allow: set[str] | None = None,
    ) -> str:
        """What the search found, as text for a person. ``unjudged`` is what ``require`` asked
        of the case as given and did not get."""
        found = self.breaks() if found is None else found
        header = (self.runner.ledger(self.case) or [{"data": {}}])[0]["data"]
        framework = header.get("framework", {})
        on = f" on {framework.get('name')} {framework.get('version')}" if framework else ""
        counts = self.summary()
        of = f" of {counts['candidates'] + self.not_run}" if self.not_run else ""
        decision = self.decision(found, unjudged or {}, allow or set())
        lines = [
            (
                f"{header.get('case', 'the case')}{on}: {counts['candidates']}{of} cases tried, "
                f"{counts['ran']} judged, {len(decision.broken)} contract rule(s) broken"
            )
        ]
        made = self.runner.by_purpose
        purposes = [f"{made[p]} {why}" for p, why in _PURPOSES.items() if made.get(p)]
        lines.append(f"    {self.runs} run(s): {', '.join(purposes)}")
        counted = self.by_contract()
        if counted:
            lines.append("    by contract: pass, fail, nothing to judge, evidence lost")
            for contract, tally in counted.items():
                lines.append(f"    {contract:<4}" + " ".join(f"{n:>5}" for n in tally.values()))
        if not self.complete:
            left = [f"{self.not_run} case(s) not run"] if self.not_run else []
            left += [f"{self.unmeasured} failure(s) not measured"] if self.unmeasured else []
            spent = f"incomplete: the budget of {self.max_runs} runs was spent with "
            lines += ["", f"{spent}{' and '.join(left)}. Nothing is concluded about them"]
        for one in found:
            lines.append("")
            lines.append(
                f"{one.predicate}  in {one.candidates} case(s)"
                + (
                    "  (at single instants only: same-instant order, not counted)"
                    * one.instants_only
                )
            )
            lines += [f"    {detail}" for detail in one.details]
            lines.append(f"    changed: {one.what}")
            others = [c for c in one.changed if c and c != one.what.partition(" at ")[0]]
            if others:
                lines.append(f"    also by changing: {'; '.join(others)}")
            widths = [
                f"    breaks with {w.variable} from {_seconds(w.lo_us)}"
                f" ({_end(w.lo_end, w.lo_unjudged)}) to {_seconds(w.hi_us)}"
                f" ({_end(w.hi_end, w.hi_unjudged)})"
                for w in one.windows
            ]
            lines += list(dict.fromkeys(widths))  # the same window under two faults is one line
            if one.unmeasured:
                more = f"{one.unmeasured} more value(s)"
                lines.append(f"    breaks at {more}, which were not measured")
        skipped = self.not_judged()
        if skipped:
            cut = sum(group.cases for group in skipped)
            result = ""
            if self.nothing_judged:
                result = ", which leaves the search with no result"
            elif self.errored:
                result = f"; the search fails, since {self.errored} raised"
            lines += ["", f"not judged: {cut} of {counts['candidates']} cases{result}"]
            for group in skipped:
                lines.append(f"    {group.cases} {group.why}: {group.reason}")
                lines.append(f"        such as {group.example}")
        elif not self.tried and not self.not_run:
            lines += ["", "no case was tried, which leaves the search with no result"]
        if self.unread:
            fails = "; the search fails" if self.unreadable else ""
            said = (
                f"not read: {len(self.unread)} run(s) made to find where to put things could "
                f"not be judged, so the cases they would place were not tried{fails}"
            )
            lines += ["", said]
            lines += [
                f"    {one.run}: {one.outcome.why_not}: {one.outcome.reason}" for one in self.unread
            ]
        if unjudged:
            lines += ["", "required, with no pass or fail in the case as given:"]
            lines += [f"    {contract} {why}" for contract, why in unjudged.items()]
        return "\n".join(lines)

    def unjudged(self, require: Iterable[str] | str) -> dict[str, str]:
        """Each contract in ``require`` that the case as given has no pass or fail for, and why."""
        ledger = self.runner.ledger(self.case)
        return check(ledger).unjudged(require) if ledger is not None else {}

    def assert_contracts(
        self, *, allow: Iterable[Predicate | str] | str = (), require: Iterable[str] | str = ()
    ) -> None:
        """Raise ``ContractFailure`` if the search found a rule broken, if a case it tried
        raised, if it tried none or could judge none of them, if a run made to find where to
        put things raised or was invalid, if its budget of runs cut it short, or if the case
        as given has no pass or fail for a contract in ``require``. Rules that break only at
        single instants, or are named in ``allow`` as ``CONTRACT/RULE``, are not counted."""
        __tracebackhide__ = True  # pytest then shows the test's own line, not this one
        allowing, unjudged = allowed(allow), self.unjudged(require)
        found = self.breaks()
        if self.decision(found, unjudged, allowing).status:
            raise ContractFailure(self.describe(found, unjudged, allow=allowing))

    @property
    def no_verdict(self) -> bool:
        """Whether the search cannot pass whatever it found: it judged no case, a case raised,
        a run made to find where to put things raised or was invalid, or the budget ran out."""
        return bool(self.nothing_judged or self.errored or self.unreadable or not self.complete)

    def summary(self) -> dict[str, Any]:
        counts = {"ran": 0, "refused": 0, "error": 0, "invalid": 0}
        for tried in self.tried:
            counts[tried.outcome.why_not or "ran"] += 1
        return {
            "candidates": len(self.tried),
            **counts,
            "impossible_placements": len(self.impossible),
            "runs": self.runs,
            "runs_by_purpose": {p: n for p, n in self.runner.by_purpose.items() if p != "reduce"},
            "max_runs": self.max_runs,
            "complete": self.complete,
            "not_run": self.not_run,
            "unmeasured": self.unmeasured,
            "original_breaks": [str(p) for p in self.original.failures],
            "found": {
                str(p): {
                    "candidates": len(self.breaking(p)),
                    "everywhere": self.everywhere(p),
                    "changed": sorted({t.candidate.what for t in self.breaking(p)}),
                }
                for p in self.predicates()
            },
            "errors": sorted({t.outcome.error for t in self.tried if t.outcome.error}),
            "not_judged": [asdict(group) for group in self.not_judged()],
            "by_contract": self.by_contract(),
            "not_read": [
                {"run": one.run, "why": one.outcome.why_not, "reason": one.outcome.reason}
                for one in self.unread
            ],
        }


DEFAULT_VARIABLES = ("speech.position", *cases.DELAY_KINDS, cases.HANGUP)
# How long the caller of a write waits, when a search adds a timeout or a drop.
WAIT_US = 1_000_000

WORKERS_VARIABLE = "EARFUL_WORKERS"


def default_workers() -> int:
    """How many worker processes a search starts by default: one per processor, or what
    ``EARFUL_WORKERS`` gives, where 0 means none. Raises ``ValueError`` for anything else."""
    given = os.environ.get(WORKERS_VARIABLE, "").strip()
    if not given:
        return os.cpu_count() or 1
    try:
        workers = int(given)
    except ValueError:
        workers = -1
    if workers < 0:
        raise ValueError(f"{WORKERS_VARIABLE} is a number of processes, 0 for none; got {given!r}")
    return workers


def check_arguments(
    case: cases.Case, variables: Iterable[str], faults: Iterable[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Check what a search is asked for before anything runs. Returns the variables and the
    faults. Raises ``ValueError`` for an invalid case or an unknown variable or fault."""
    errors = cases.validate(case)
    if errors:
        raise ValueError("not an earful.case/1 document: " + "; ".join(errors))
    kinds = tuple(variables)
    known = (*cases.SPEECH_KINDS, *cases.DELAY_KINDS, cases.HANGUP)
    unknown = sorted(set(kinds) - set(known))
    if unknown:
        raise ValueError(f"unknown variables {unknown}; choose from {list(known)}")
    fault_kinds = tuple(faults)
    unknown = sorted(set(fault_kinds) - set(cases.FAULT_KINDS))
    if unknown:
        raise ValueError(f"unknown faults {unknown}; choose from {list(cases.FAULT_KINDS)}")
    return kinds, fault_kinds


@dataclass(frozen=True)
class Faulted:
    """A case with a fault added to one write of its run."""

    label: str  # what was added, such as "backend.timeout 1, commit first"
    nth: int  # the write
    case: cases.Case
    as_written: bool = False  # a timeout whose write keeps the latency the run gave it


def faulted_versions(
    case: cases.Case, events: list[Event], kinds: Iterable[str], wait_us: int, after: int = 0
) -> list[Faulted]:
    """``case`` with each of ``kinds`` added to each write that its run, ``events``, made
    after write ``after``. A timeout comes in both orders: the write commits before its
    caller gives up, and after."""
    out: list[Faulted] = []
    kinds = tuple(kinds)
    for request in (e["data"] for e in events if e["kind"] == "effect.requested"):
        nth, latency = request["request"], request["latency_us"]
        for of in kinds if nth > after else ():
            with_one = cases.with_fault(case, of, nth, None if of == "backend.error" else wait_us)
            if with_one is None:
                continue  # the write already has a fault
            if of != "backend.timeout":
                out.append(Faulted(f"{of} {nth}", nth, with_one))
            elif latency < wait_us:
                slower = cases.with_value(
                    with_one, Variable("backend.latency", nth), wait_us + 1_000_000
                )
                out.append(Faulted(f"{of} {nth}, commit first", nth, with_one, as_written=True))
                out += [Faulted(f"{of} {nth}, commit later", nth, slower)] if slower else []
            else:
                patient = cases.with_value(with_one, Variable(cases.WAIT, nth), latency + 1_000_000)
                out.append(Faulted(f"{of} {nth}, commit later", nth, with_one, as_written=True))
                out += [Faulted(f"{of} {nth}, commit first", nth, patient)] if patient else []
    return out


def explore(
    case: cases.Case,
    run: Run,
    *,
    variables: Iterable[str] = DEFAULT_VARIABLES,
    faults: Iterable[str] = cases.FAULT_KINDS,
    pairs: bool = False,
    profiles: int = 0,
    speeches: Iterable[int] | None = None,
    moments: Iterable[str] | None = None,
    offsets_us: Iterable[int] = OFFSETS_US,
    words: bool = True,
    delay_limit_us: int = DELAY_LIMIT_US,
    wait_us: int = WAIT_US,
    stretch_us: int = STRETCH_US,
    each: Each = map,
    max_runs: int | None = None,
    planned: Callable[[Plan], None] | None = None,
) -> Exploration:
    """Search ``case`` for the timings and faults at which it breaks a contract.

    Moves each of ``variables`` beside every transition and adds each of ``faults`` to each
    backend write. ``pairs`` also tries two changes at once, and ``profiles`` moves the
    speeches again under that many seeded redraws of the provider delays. ``planned`` is
    told what is about to run. Once ``max_runs`` runs are made no further case is run and
    no further window measured, and the result is not ``complete``.
    Raises ``ValueError`` if ``case`` is not earful.case/1 or cannot be run as given, or for
    an unknown variable, fault, speech or moment."""
    kinds, fault_kinds = check_arguments(case, variables, faults)
    wanted_speeches, wanted_moments = _wanted(case, speeches, moments, profiles, max_runs)
    for name, value, least in (
        ("delay_limit_us", delay_limit_us, 1),
        ("wait_us", wait_us, 0),  # 0 gives up at once
        ("stretch_us", stretch_us, 0),  # 0 makes nothing slow
    ):
        if type(value) is not int or value < least:
            raise ValueError(f"{name} is a number of microseconds, at least {least}; got {value!r}")
    runner = Runner(run)
    runner.purpose = "read"
    events = runner.ledger(case)
    original = runner.outcome(case)
    if events is None:
        raise ValueError(f"the case as given cannot be searched: {original.reason}")
    placer = _Placer(
        case,
        runner,
        kinds,
        tuple(offsets_us),
        words=words,
        delay_limit_us=delay_limit_us,
        each=each,
        wanted_speeches=wanted_speeches,
        wanted_moments=wanted_moments,
    )
    delay_kinds = [k for k in kinds if k in cases.DELAY_KINDS]
    placer.place(case, delay_handles(events, delay_kinds), events, "")
    placer.move_speeches(case, events, "")
    placer.move_hangup(case, events, "")
    faulted, under = placer.under_faults(events, fault_kinds, wait_us)
    if pairs:
        placer.in_pairs(events, faulted, under, fault_kinds, wait_us, stretch_us)
    placer.under_profiles(events, profiles)
    return _run_found(placer, original, max_runs, planned)


def _wanted(
    case: cases.Case,
    speeches: Iterable[int] | None,
    moments: Iterable[str] | None,
    profiles: int,
    max_runs: int | None,
) -> tuple[set[int] | None, set[str] | None]:
    """The speeches and moments a search is limited to. Raises ``ValueError`` for an
    unknown one, a budget below one run or a number of profiles that is not one."""
    if max_runs is not None and max_runs < 1:
        raise ValueError(f"max_runs is a number of runs, at least 1; got {max_runs}")
    if type(profiles) is not int or profiles < 0:
        raise ValueError(f"profiles is a number of profiles, 0 for none; got {profiles!r}")
    wanted_moments = None if moments is None else set(moments)
    known = sorted({moment for moved in _MOVED_BY.values() for moment in moved})
    unknown = sorted(map(repr, (wanted_moments or set()) - set(known)))
    if unknown:
        raise ValueError(f"unknown moments {', '.join(unknown)}; choose from {known}")
    wanted_speeches = None if speeches is None else set(speeches)
    count = len(case["speeches"])
    unknown = [repr(s) for s in wanted_speeches or () if type(s) is not int or not 0 <= s < count]
    if unknown:
        raise ValueError(
            f"no speech {', '.join(sorted(unknown))}: the case has {count}, numbered from 0"
        )
    return wanted_speeches, wanted_moments


class _Placer:
    """Reads runs of a case and its variants, and collects a candidate for each place a
    handle can be put beside a transition."""

    def __init__(
        self,
        case: cases.Case,
        runner: Runner,
        kinds: tuple[str, ...],
        offsets: tuple[int, ...],
        *,
        words: bool,
        delay_limit_us: int,
        each: Each,
        wanted_speeches: set[int] | None,
        wanted_moments: set[str] | None,
    ) -> None:
        self.case, self.runner, self.kinds, self.offsets = case, runner, kinds, offsets
        self.words, self.delay_limit_us, self.each = words, delay_limit_us, each
        self.wanted_speeches, self.wanted_moments = wanted_speeches, wanted_moments
        self.speech_kinds = [k for k in kinds if k in cases.SPEECH_KINDS]
        self.found: dict[str, Candidate] = {}
        self.impossible: list[Placement] = []
        self.unread: dict[str, Unread] = {}

    def read(self, wanted: list[tuple[str, cases.Case]]) -> list[list[Event] | None]:
        """The ledgers of runs made to find where to put things, each named. One that cannot
        be judged is noted."""
        produced = self.runner.ledgers([one for _name, one in wanted], self.each)
        for (name, one), ledger in zip(wanted, produced):
            if ledger is None:
                self.unread.setdefault(cases.canonical(one), Unread(name, self.runner.outcome(one)))
        return produced

    def keep(self, candidate: Candidate) -> None:
        key = cases.canonical(candidate.case)
        if key == cases.canonical(self.case):
            return
        if key in self.found:
            self.found[key].placements += candidate.placements
        else:
            self.found[key] = candidate

    def place(
        self,
        on: cases.Case,
        handles: list[Handle],
        against: list[Event],
        base: str,
        coarse: bool = False,
        apart: bool = False,
    ) -> None:
        """A coarse placing is at each transition itself, the ends of played words left out.
        ``apart`` says ``against`` is a run without the handles."""
        tried, cannot = candidates(
            on,
            handles,
            transitions(against, words=self.words and not coarse),
            offsets_us=(0,) if coarse else self.offsets,
            delay_limit_us=self.delay_limit_us,
            base=base,
            apart=apart,
        )
        self.impossible.extend(cannot)
        for candidate in tried:
            self.keep(candidate)

    def move_speeches(
        self, on: cases.Case, produced: list[Event], base: str, coarse: bool = False
    ) -> None:
        """A speech races with the run it would interrupt: the one without it."""
        wanted_speeches, wanted_moments = self.wanted_speeches, self.wanted_moments
        moved = [
            index
            for index in (range(len(on["speeches"])) if self.speech_kinds else ())
            if wanted_speeches is None or index in wanted_speeches
        ]
        of = base or "the case as given"
        without = self.read(
            [(f"{of}, without speech {i}", cases.without_speech(on, i)) for i in moved]
        )
        for index, alone in zip(moved, without):
            handles = speech_handles(on, index, produced, self.speech_kinds)
            if wanted_moments is not None:
                handles = [h for h in handles if h.moment in wanted_moments]
            if alone is not None:  # else it is noted as not read
                self.place(on, handles, alone, base, coarse, apart=True)

    def move_hangup(
        self, on: cases.Case, produced: list[Event], base: str, coarse: bool = False
    ) -> None:
        """A hangup cuts short the run in which the user stays."""
        if cases.HANGUP not in self.kinds:
            return
        stays: list[Event] | None = produced
        if on["hangup_us"] is not None:
            of = base or "the case as given"
            stays = self.read([(f"{of}, without the hangup", cases.without_hangup(on))])[0]
        if stays is not None:
            self.place(on, [hangup_handle(on, stays)], stays, base, coarse, apart=True)

    def under_faults(
        self, events: list[Event], fault_kinds: tuple[str, ...], wait_us: int
    ) -> tuple[list[Faulted], dict[str, list[Event]]]:
        """Each fault on each write, and the times its run makes worth moving: when the caller
        gave up, and when the write commits. Returns the faulted cases and their runs."""
        faulted = faulted_versions(self.case, events, fault_kinds, wait_us)
        for one in faulted:
            self.keep(Candidate(one.case, None, None, base=one.label))
        under: dict[str, list[Event]] = {}  # label -> the run with that fault in place
        with_faults = self.read([(one.label, one.case) for one in faulted])
        for one, produced in zip(faulted, with_faults):
            if produced is None:
                continue
            under[one.label] = produced
            gave_up = next(
                (
                    e
                    for e in produced
                    if e["kind"] == "effect.timed_out" and e["data"]["request"] == one.nth
                ),
                None,
            )
            if gave_up is not None:
                handle = Handle(
                    Variable(cases.WAIT, one.nth),
                    "gave_up",
                    gave_up["t_us"],
                    gave_up["data"]["after_us"],
                )
                self.place(one.case, [handle], produced, one.label)
            if one.as_written:
                commits = [
                    h
                    for h in delay_handles(produced, ["backend.latency"])
                    if h.variable.index == one.nth
                ]
                self.place(one.case, commits, produced, one.label)
        return faulted, under

    def in_pairs(
        self,
        events: list[Event],
        faulted: list[Faulted],
        under: dict[str, list[Event]],
        fault_kinds: tuple[str, ...],
        wait_us: int,
        stretch_us: int,
    ) -> None:
        """Two changes at once: a time moved under a fault, two faults, and a time moved with
        a slow model or backend."""
        for one in faulted:
            if one.label in under:
                self.move_speeches(one.case, under[one.label], one.label)
                self.move_hangup(one.case, under[one.label], one.label)
        for one in faulted:
            # A second fault goes on a later write of the run with the first: a retry is one.
            with_first = under.get(one.label, [])
            for two in faulted_versions(one.case, with_first, fault_kinds, wait_us, after=one.nth):
                self.keep(Candidate(two.case, None, None, base=f"{one.label} + {two.label}"))
        to = min(stretch_us, self.delay_limit_us)
        slow: list[tuple[str, cases.Case]] = []
        for delay in delay_handles(events, STRETCHED):
            longer = cases.with_value(self.case, delay.variable, to)
            if delay.value_us < to and longer is not None:
                label = f"{delay.variable}, stretched"
                slow.append((label, longer))
                self.keep(Candidate(longer, None, None, base=label))
        slowed = self.read(slow)
        for (label, longer), produced in zip(slow, slowed):
            if produced is not None:
                self.move_speeches(longer, produced, label, coarse=True)
                self.move_hangup(longer, produced, label, coarse=True)

    def under_profiles(self, events: list[Event], profiles: int) -> None:
        """The speeches moved again under seeded redraws of every provider delay."""
        case = self.case
        applied = delay_handles(events, cases.DELAY_KINDS)
        redrawn: list[cases.Case] = []
        for number in range(1, profiles + 1):
            rng = random.Random(f"{case['seed']}:{number}")
            profile = case
            for handle in applied:
                most = min(self.delay_limit_us, max(2 * handle.value_us, 1_000_000))
                drawn = rng.randrange(0, most + 1, 10_000)
                changed = cases.with_value(profile, handle.variable, drawn)
                profile = changed if changed is not None else profile
            redrawn.append(profile)
        named = [(f"profile {number}", profile) for number, profile in enumerate(redrawn, 1)]
        for number, (profile, produced) in enumerate(zip(redrawn, self.read(named)), 1):
            if produced is not None:
                self.keep(Candidate(profile, None, None, base=f"profile {number}"))
                self.move_speeches(profile, produced, f"profile {number}")


def _run_found(
    placer: _Placer,
    original: Outcome,
    max_runs: int | None,
    planned: Callable[[Plan], None] | None,
) -> Exploration:
    """Run the candidates the budget leaves room for, after saying what is about to run."""
    runner = placer.runner
    todo = list(placer.found.values())
    fresh = [c for c in todo if not runner.seen(c.case)]
    plan = Plan(
        placements=sum(len(c.placements) for c in todo),
        cases=len(todo),
        to_run=len(fresh),
        runs=runner.runs,
        max_runs=max_runs,
    )
    if planned is not None:
        planned(plan)
    # The runs that read the boundaries are made whatever the budget: there is no search without.
    dropped = {id(c) for c in fresh[plan.covered :]}
    todo = [c for c in todo if id(c) not in dropped]
    with runner.counting("cases"):
        outcomes = runner.many([c.case for c in todo], placer.each)
    runner.purpose = "other"
    tried = [Tried(c, o) for c, o in zip(todo, outcomes)]
    return Exploration(
        placer.case,
        original,
        tried,
        placer.impossible,
        placer.offsets,
        runner,
        max_runs,
        not_run=len(dropped),
        unread=list(placer.unread.values()),
    )


__all__ = [
    "DEFAULT_VARIABLES",
    "DELAY_LIMIT_US",
    "OFFSETS_US",
    "STRETCHED",
    "STRETCH_US",
    "WAIT_US",
    "WORKERS_VARIABLE",
    "Break",
    "Candidate",
    "CaseRefused",
    "Each",
    "Exploration",
    "Faulted",
    "Handle",
    "NotJudged",
    "Outcome",
    "Placement",
    "Plan",
    "Runner",
    "Spent",
    "Transition",
    "Tried",
    "Unread",
    "Window",
    "anchors",
    "attempt",
    "candidates",
    "check_arguments",
    "default_workers",
    "delay_handles",
    "evaluate",
    "explore",
    "faulted_versions",
    "hangup_handle",
    "judged",
    "speech_handles",
    "transitions",
    "window",
]
