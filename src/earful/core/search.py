"""The boundary scheduler: put each moment a scenario controls just before, at and just
after every moment the system under test changes state, and check each run.

A timing defect lives at a boundary: the user interrupts as the first audio arrives, a
tool returns as the turn is being committed. Stepping one thing through time in fixed
increments finds such a place only if a step happens to land on it. This module asks the
ledger where the boundaries are and goes to them.

A case has **variables** (``earful.core.case``): where a speech sits, when it ends, how
long its transcript takes, how long a model call, a first audio or a backend write takes.
Moving a variable moves one or more **handles**, the moments at which it acts on the
system: a speech's position moves the VAD's report of it, its interruption point, its
transcript and the end of its turn; a backend latency moves the commit.

1. Run the case. Every event of the run is a **transition**; so are the moments a
   scripted delay runs out and the end of each played word. For a speech, the run without
   that speech is used instead: the timeline it would interrupt.
2. For each handle, each transition and each offset (a little before, exactly at, a
   little after), change the handle's variable so that the handle falls there. Run and
   check.
3. Where a contract breaks, measure the **window**: how far the variable can move while
   the run still breaks the same rule. A window with no width is a failure that turns on
   which of two same-instant events ran first, and says nothing about the system.

The search uses everything in the ledger to decide where to look, the framework's own
events included. What it finds is decided by the checker alone.

Framework-independent: this module must never import livekit. The adapter supplies
``run``, a function from a case (earful.case/1) to the ledger of running it.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from . import case as cases
from .case import Variable
from .checker import ContractFailure, check
from .checker.report import allowed
from .ledger import ledger_hash
from .predicate import Predicate, failures

Event = dict[str, Any]
Run = Callable[[cases.Case], list[Event]]

# A little before and a little after: one 10 ms audio frame, the finest regular step at
# which a framework is given audio and a sink plays it.
OFFSETS_US = (-10_000, 0, 10_000)

# The longest a search will make a provider take. A model that answers after a minute
# breaks things too, but that is a different scenario, not a nearby ordering of this one.
DELAY_LIMIT_US = 10_000_000

# How long a search makes a backend write or a model generation take when it pairs a slow
# one with a speech or the hangup: longer than the default interruption threshold, so
# that a speech can interrupt while it is under way.
STRETCH_US = 1_500_000
STRETCHED = ("backend.latency", "llm.duration")

_NOT_TRANSITIONS = {"run.start", "run.end", "user.origin", "history.final", "fw.close"}


# --------------------------------------------------------------------------- running


@dataclass
class Outcome:
    """What one run of a case came to."""

    case: cases.Case
    failures: tuple[Predicate, ...] = ()
    outcomes: dict[str, str] = field(default_factory=dict)  # contract -> outcome
    hash: str | None = None
    invalid: tuple[str, ...] = ()  # the run is not valid evidence
    refused: str | None = None  # the adapter would not run the case
    error: str | None = None  # the run raised

    @property
    def ran(self) -> bool:
        return self.refused is None and self.error is None and not self.invalid

    @property
    def why_not(self) -> str | None:
        """Why nothing can be concluded from it, in a word."""
        if self.refused is not None:
            return "refused"
        if self.error is not None:
            return "error"
        return "invalid" if self.invalid else None


def judged(case: cases.Case, events: list[Event]) -> Outcome:
    """What the ledger of running ``case`` comes to. A run that is not valid evidence has
    no failures: nothing can be concluded from it."""
    report = check(events)
    if not report.valid:
        return Outcome(case, invalid=report.invalid, hash=ledger_hash(events))
    return Outcome(case, tuple(sorted(failures(report))), report.outcomes(), ledger_hash(events))


def attempt(run: Run, case: cases.Case) -> tuple[Outcome, list[Event] | None]:
    """Run a case and check it. Returns the outcome, and the ledger if the run can be
    judged. A case the adapter refuses (``ValueError``) and a run that raises anything
    else are recorded, not raised: one bad run must not end a search."""
    try:
        events = run(case)
    except ValueError as exc:
        return Outcome(case, refused=str(exc)), None
    except Exception as exc:  # noqa: BLE001
        return Outcome(case, error=f"{type(exc).__name__}: {exc}"), None
    outcome = judged(case, events)
    return outcome, events if outcome.ran else None


def evaluate(run: Run, case: cases.Case) -> Outcome:
    """Run a case and check it: the outcome of ``attempt``, without the ledger."""
    return attempt(run, case)[0]


# How a batch of runs is carried out: ``map`` one after another, a process pool's ``map``
# side by side.
Each = Callable[..., Iterable[Any]]


class Runner:
    """Runs cases for a search or a reduction: each case once, however often it is asked
    for, and counted."""

    def __init__(self, run: Run) -> None:
        self.run = run
        self.runs = 0
        self._seen: dict[str, Outcome] = {}
        self._ledgers: dict[str, list[Event] | None] = {}

    def outcome(self, case: cases.Case) -> Outcome:
        return self.many([case])[0]

    def many(self, wanted: list[cases.Case], each: Each = map) -> list[Outcome]:
        """The outcome of each case. ``each`` is how the runs are carried out."""
        keys = [cases.canonical(case) for case in wanted]
        todo = {key: case for key, case in zip(keys, wanted) if key not in self._seen}
        for key, outcome in zip(todo, each(partial(evaluate, self.run), list(todo.values()))):
            self._seen[key] = outcome
            self.runs += 1
        return [self._seen[key] for key in keys]

    def ledger(self, case: cases.Case) -> list[Event] | None:
        return self.ledgers([case])[0]

    def ledgers(self, wanted: list[cases.Case], each: Each = map) -> list[list[Event] | None]:
        """The ledger of each case, or None for a case whose run cannot be judged. A
        search places candidates against these runs, so their ledgers are kept. Their
        outcomes are kept too: a case run for its ledger is not run again for its verdict."""
        keys = [cases.canonical(case) for case in wanted]
        todo = {key: case for key, case in zip(keys, wanted) if key not in self._ledgers}
        attempts = each(partial(attempt, self.run), list(todo.values()))
        for key, (outcome, events) in zip(todo, attempts):
            self._ledgers[key] = events
            self._seen.setdefault(key, outcome)
            self.runs += 1
        return [self._ledgers[key] for key in keys]

    def breaks(self, case: cases.Case, predicate: Predicate) -> bool:
        return predicate in self.outcome(case).failures


# --------------------------------------------------------------------------- where to look


@dataclass(frozen=True)
class Transition:
    """An instant at which something in the run changed, and what did."""

    t_us: int
    kinds: tuple[str, ...]


def transitions(events: list[Event], *, words: bool = True) -> list[Transition]:
    """Every instant of a run that something else could race with, in time order.

    Each recorded event is one. So are the moments the ledger states and no event marks:
    the model's first token and its last, the voice's first audio, the middle of a word,
    the end of every played word (unless ``words`` is false) and the moment a user turn
    can be committed.
    """
    header = events[0]["data"]["scenario"]
    end_of_turn = max(
        header["providers"]["vad"]["min_silence_us"], header["session"]["min_endpointing_delay_us"]
    )
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


def anchors(header: dict[str, Any], speech: dict[str, Any]) -> dict[str, int]:
    """The moments at which a speech acts on the system, as microseconds after its start.

    ``header`` is the ``scenario`` of a ledger's ``run.start``: the delays come from what
    the run declared, never from what the framework did. They are those of the speech on
    its own; beside another speech the VAD may hear the two as one.
    """
    vad, session = header["providers"]["vad"], header["session"]
    length = speech["end_us"] - speech["start_us"]
    out = {"vad_start": vad["min_speech_us"]}
    if length >= session["min_interruption_duration_us"]:
        out["interrupt"] = session["min_interruption_duration_us"]
    if speech["transcript"]:
        out["transcript"] = length + speech["stt_delay_us"]
    out["vad_end"] = length + vad["min_silence_us"]
    out["turn_end"] = length + max(vad["min_silence_us"], session["min_endpointing_delay_us"])
    return out


# Which of a speech's moments each of its variables moves.
_MOVED_BY = {
    "speech.position": ("vad_start", "interrupt", "transcript", "vad_end", "turn_end"),
    "speech.end": ("transcript", "vad_end", "turn_end"),
    "speech.stt_delay": ("transcript",),
}


@dataclass(frozen=True)
class Handle:
    """One moment a case controls: which variable moves it, when it falls now, and what
    the variable is now."""

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
    """The handles of one speech. ``events`` is any ledger of the scenario: it supplies
    the declared delays and the origin."""
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
    """The handles of every provider delay the run applied, declared by the case or
    chosen by the scenario's code: the ledger records each one."""
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
    """The moment the user hangs up. For a user who stays it is placed from the origin,
    so that moving it declares a hangup. ``events`` is any ledger of the scenario."""
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
    # What was done to the case before anything was moved: a fault, a stretched delay, a
    # profile.
    base: str = ""

    @property
    def what(self) -> str:
        """What was changed, for a person: ``backend.error 1``, ``speech.position 1``,
        ``backend.drop 1 + speech.position 1``."""
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
) -> tuple[list[Candidate], list[Placement]]:
    """Every case in which one of ``handles`` falls before, at or after one of
    ``targets``, and the placements for which no such case exists (a delay cannot be
    negative or longer than ``delay_limit_us``, a speech cannot overlap or pass another).
    Cases that coincide are one candidate."""
    found: dict[str, Candidate] = {}
    impossible: list[Placement] = []
    offsets = tuple(offsets_us)
    for handle in handles:
        for transition in targets:
            if transition.t_us == handle.t_us:
                continue  # where it already is
            for offset in offsets:
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


# --------------------------------------------------------------------------- windows


@dataclass
class Window:
    """How far a variable can move while the run still breaks a rule: every value from
    ``lo_us`` to ``hi_us`` does, as far as was probed.

    Each end says why the search for it stopped. ``passes``: a run just beyond it does
    not break the rule; this is an edge of the failure. ``blocked``: no case exists
    beyond it (a delay at zero, a speech against another) or none could be judged; the
    failure holds as far as the scenario can go. ``reach``: the search gave up; the
    failure may hold further. ``resolution_us`` is how finely each end was located.
    """

    variable: Variable
    lo_us: int
    hi_us: int
    lo_end: str
    hi_end: str
    resolution_us: int

    @property
    def width_us(self) -> int:
        return self.hi_us - self.lo_us

    @property
    def instant(self) -> bool:
        """It breaks at one value and not a microsecond either side: a matter of which of
        two same-instant events ran first, and nothing more."""
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
        }


def window(
    runner: Runner,
    case: cases.Case,
    variable: Variable,
    predicate: Predicate,
    *,
    resolution_us: int = 1_000,
    reach_us: int = 30_000_000,
) -> Window:
    """The window around ``case`` along ``variable``: the values, reached from the
    case's own without a passing run between, at which running it still breaks
    ``predicate``. ``case`` must break it.

    Each end is found by doubling the step until a run does not break the rule and then
    halving back, to within ``resolution_us``. A window narrower than that is located
    again to the microsecond.
    """
    at = cases.value(case, variable)
    if at is None or not runner.breaks(case, predicate):
        raise ValueError(f"the case does not break {predicate} with {variable} declared")

    def breaks(to: int) -> bool | None:
        """True or false; None where there is no case, or no run that can be judged."""
        moved = cases.with_value(case, variable, to)
        if moved is None:
            return None
        outcome = runner.outcome(moved)
        return predicate in outcome.failures if outcome.ran else None

    def end(direction: int, resolution: int, reach: int) -> tuple[int, str]:
        """The farthest breaking value in one direction, and why the search stopped."""
        good, step = at, resolution
        while True:
            probe = at + direction * step
            result = breaks(probe)
            if not result:
                bad, why = probe, "passes" if result is False else "blocked"
                break
            good = probe
            if step >= reach:
                return good, "reach"
            step *= 2
        while abs(bad - good) > resolution:
            middle = (good + bad) // 2
            result = breaks(middle)
            if result:
                good = middle
            else:
                bad, why = middle, "passes" if result is False else "blocked"
        return good, why

    (lo, lo_end), (hi, hi_end) = end(-1, resolution_us, reach_us), end(1, resolution_us, reach_us)
    if lo == hi and resolution_us > 1:
        (lo, lo_end), (hi, hi_end) = end(-1, 1, resolution_us), end(1, 1, resolution_us)
        resolution_us = 1
    return Window(variable, lo, hi, lo_end, hi_end, resolution_us)


# --------------------------------------------------------------------------- the search


@dataclass
class Tried:
    """One candidate, run."""

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
    # It breaks only where two events fall on the same instant: a matter of which ran
    # first, which says nothing about the system.
    instants_only: bool
    example: cases.Case  # a case that breaks it, with the least changed
    what: str  # what was changed in the example
    details: tuple[str, ...]  # the checker's findings for the example


def _seconds(us: int) -> str:
    return f"{us / 1_000_000:.3f} s"


# Why the search for one end of a window stopped, for a person.
_ENDS = {
    "passes": "passes beyond it",
    "blocked": "no case exists beyond it",
    "reach": "still breaks where the search stopped",
}


@dataclass
class Exploration:
    case: cases.Case
    original: Outcome  # the case as given
    tried: list[Tried]
    impossible: list[Placement]
    offsets_us: tuple[int, ...]
    runner: Runner

    def predicates(self) -> list[Predicate]:
        """Every rule some candidate broke."""
        return sorted({p for t in self.tried for p in t.outcome.failures})

    def breaking(self, predicate: Predicate) -> list[Tried]:
        return [t for t in self.tried if predicate in t.outcome.failures]

    def everywhere(self, predicate: Predicate) -> bool:
        """Whether the rule breaks in the case as given and in every candidate that ran:
        a failure that is not about timing at all."""
        ran = [t for t in self.tried if t.outcome.ran]
        return predicate in self.original.failures and all(
            predicate in t.outcome.failures for t in ran
        )

    def windows(
        self, predicate: Predicate, *, most: int = 3, resolution_us: int = 1_000
    ) -> list[Window]:
        """The windows in which the rule breaks, measured: at most ``most`` per variable
        and base, each around a breaking candidate that lies in none measured before it.
        A candidate that only added a fault moved nothing and has no window."""
        out: list[Window] = []
        measured: list[tuple[str, Window]] = []
        per_variable: dict[tuple[str, Variable], int] = {}
        moved = [t for t in self.breaking(predicate) if t.candidate.variable is not None]
        for tried in sorted(
            moved, key=lambda t: (t.candidate.base, t.candidate.variable, t.candidate.value_us)
        ):
            variable, at = tried.candidate.variable, tried.candidate.value_us
            assert variable is not None and at is not None
            key = (tried.candidate.base, variable)
            if per_variable.get(key, 0) >= most:
                continue
            if any(
                b == tried.candidate.base and w.variable == variable and w.lo_us <= at <= w.hi_us
                for b, w in measured
            ):
                continue
            if cases.value(tried.candidate.case, variable) is None:
                continue
            found = window(
                self.runner, tried.candidate.case, variable, predicate, resolution_us=resolution_us
            )
            measured.append((tried.candidate.base, found))
            out.append(found)
            per_variable[key] = per_variable.get(key, 0) + 1
        return out

    def coverage(self) -> dict[str, dict[str, Any]]:
        """For each handle, which kinds of transition it was put beside in a run that can
        be judged, and which it never was and why: no such case exists, the adapter
        refused it, the run raised, or the run was not valid evidence."""
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

    def breaks(self, *, windows: bool | Iterable[Predicate] = True) -> list[Break]:
        """Every rule the search found broken, with an example of each. ``windows``
        measures how wide each failure is, which takes further runs: for every rule, for
        none, or for the rules named. A failure that is not measured is not known to be
        at single instants only."""
        wanted = None if isinstance(windows, bool) else set(windows)
        out = []
        for predicate in self.predicates():
            breaking = self.breaking(predicate)
            measure = windows is True or (wanted is not None and predicate in wanted)
            measured = tuple(self.windows(predicate)) if measure else ()
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
            ledger = self.runner.ledger(example)
            details = tuple(
                finding.detail
                for verdict in (check(ledger).failures if ledger is not None else ())
                if verdict.contract == predicate.contract
                for finding in verdict.findings
                if finding.rule == predicate.rule
            )
            out.append(
                Break(
                    predicate=predicate,
                    candidates=len(breaking),
                    everywhere=self.everywhere(predicate),
                    changed=tuple(sorted({t.candidate.what for t in breaking})),
                    windows=measured,
                    instants_only=bool(measured)
                    and not unmoved
                    and all(w.instant for w in measured),
                    example=example,
                    what=what,
                    details=details,
                )
            )
        return out

    def _what(self, candidate: Candidate) -> str:
        """What a candidate changed, with the time it moved and where that time was."""
        if candidate.variable is None or candidate.value_us is None:
            return candidate.base
        was = cases.value(self.case, candidate.variable)
        written = f"as written {_seconds(was)}" if was is not None else "not declared as written"
        moved = f"{candidate.variable} at {_seconds(candidate.value_us)} ({written})"
        return f"{candidate.base} + {moved}" if candidate.base else moved

    def describe(self, found: list[Break] | None = None) -> str:
        """What the search found, as text for a person."""
        found = self.breaks() if found is None else found
        header = (self.runner.ledger(self.case) or [{"data": {}}])[0]["data"]
        framework = header.get("framework", {})
        on = f" on {framework.get('name')} {framework.get('version')}" if framework else ""
        counts = self.summary()
        lines = [
            (
                f"{header.get('case', 'the case')}{on}: {counts['candidates']} cases tried, "
                f"{counts['ran']} judged, {len(found)} contract rule(s) broken"
            )
        ]
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
                lines.append(f"    also by changing: {', '.join(others)}")
            widths = [
                f"    breaks with {w.variable} from {_seconds(w.lo_us)} ({_ENDS[w.lo_end]})"
                f" to {_seconds(w.hi_us)} ({_ENDS[w.hi_end]})"
                for w in one.windows
            ]
            lines += list(dict.fromkeys(widths))  # the same window under two faults is one line
        unjudged = {k: counts[k] for k in ("refused", "error", "invalid") if counts[k]}
        if unjudged:
            lines.append("")
            lines.append(
                "not judged: " + ", ".join(f"{n} {why}" for why, n in sorted(unjudged.items()))
            )
        return "\n".join(lines)

    def assert_contracts(self, *, allow: Iterable[Any] = ()) -> None:
        """Raise ``ContractFailure``, an ``AssertionError``, if the search found a rule
        broken. A rule that breaks only at single instants is not counted. ``allow``
        names rules that may break, as ``CONTRACT/RULE``: a known defect of a release
        under test."""
        __tracebackhide__ = True  # pytest then shows the test's own line, not this one
        found = self.breaks()
        counted = [
            one
            for one in found
            if not one.instants_only and str(one.predicate) not in allowed(allow)
        ]
        if counted:
            raise ContractFailure(self.describe(found))

    def summary(self) -> dict[str, Any]:
        counts = {"ran": 0, "refused": 0, "error": 0, "invalid": 0}
        for tried in self.tried:
            counts[tried.outcome.why_not or "ran"] += 1
        return {
            "candidates": len(self.tried),
            **counts,
            "impossible_placements": len(self.impossible),
            "runs": self.runner.runs,
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
        }


DEFAULT_VARIABLES = ("speech.position", *cases.DELAY_KINDS, cases.HANGUP)
# How long the caller of a write waits, when a search adds a timeout or a drop.
WAIT_US = 1_000_000


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
) -> Exploration:
    """Search ``case`` for the timings and faults at which it breaks a contract.

    **One thing at a time.** Each variable named by ``variables`` is moved so that its
    handles fall before, at and after every transition: by default where each speech
    sits, every provider delay, and the moment the user hangs up: the user leaves, and
    the session is closed, at every transition after their last speech. ``speech.end``
    and ``speech.stt_delay`` can be added.
    Each fault named by ``faults`` is added to each backend write the run made: an error,
    a timeout, a drop. For a timeout the write is tried committing before the caller
    gives up and after. The moment the caller gives up is then moved to every transition,
    and so is the commit of a write that timed out.

    **Two things at a time**, if asked for. With ``pairs``, every speech (and the hangup,
    if it is among the variables) is moved again with each fault in place, and faults are
    added to two writes at once. Each backend write and each model generation the run
    made is also stretched to ``stretch_us``, one per version of the case, and every
    speech and the hangup are moved again with that delay in place: to each transition
    itself, the ends of played words left out. A delay that is already that long is not
    stretched. With
    ``profiles``, every speech is moved again under that many provider-timing profiles:
    every delay the run applied, redrawn at random from a seed, so that the same search
    gives the same profiles.

    ``speeches`` limits the speeches moved (by index) and ``moments`` the speech handles
    used (``vad_start``, ``interrupt``, ``transcript``, ``vad_end``, ``turn_end``).
    ``words`` adds the end of every played word to the transitions. No provider delay or
    wait is made longer than ``delay_limit_us``, and ``wait_us`` is how long a caller
    waits where a search adds a timeout or a drop. ``each`` is how the runs are carried
    out: ``map`` one after another, a process pool's ``map`` side by side, provided
    ``run`` can be sent to another process.

    Raises ``ValueError`` if ``case`` is not earful.case/1 or cannot be run as given.
    """
    errors = cases.validate(case)
    if errors:
        raise ValueError("not an earful.case/1 document: " + "; ".join(errors))
    kinds = tuple(variables)
    unknown = sorted(set(kinds) - set(cases.SPEECH_KINDS) - set(cases.DELAY_KINDS) - {cases.HANGUP})
    if unknown:
        raise ValueError(f"unknown variables {unknown}")
    fault_kinds = tuple(faults)
    unknown = sorted(set(fault_kinds) - set(cases.FAULT_KINDS))
    if unknown:
        raise ValueError(f"unknown faults {unknown}")
    runner = Runner(run)
    offsets = tuple(offsets_us)
    wanted_moments = None if moments is None else set(moments)
    wanted_speeches = None if speeches is None else set(speeches)
    delay_kinds = [k for k in kinds if k in cases.DELAY_KINDS]
    speech_kinds = [k for k in kinds if k in cases.SPEECH_KINDS]

    events = runner.ledger(case)
    original = runner.outcome(case)
    if events is None:
        why = original.refused or original.error or "; ".join(original.invalid)
        raise ValueError(f"the case as given cannot be searched: {why}")
    found: dict[str, Candidate] = {}
    impossible: list[Placement] = []

    def keep(candidate: Candidate) -> None:
        key = cases.canonical(candidate.case)
        if key == cases.canonical(case):
            return
        if key in found:
            found[key].placements += candidate.placements
        else:
            found[key] = candidate

    def place(
        on: cases.Case, handles: list[Handle], against: list[Event], base: str, coarse: bool = False
    ) -> None:
        """Put each handle beside every transition of the run ``against``. A coarse
        placing is at each transition itself, the ends of played words left out."""
        tried, cannot = candidates(
            on,
            handles,
            transitions(against, words=words and not coarse),
            offsets_us=(0,) if coarse else offsets,
            delay_limit_us=delay_limit_us,
            base=base,
        )
        impossible.extend(cannot)
        for candidate in tried:
            keep(candidate)

    def move_speeches(
        on: cases.Case, produced: list[Event], base: str, coarse: bool = False
    ) -> None:
        """A speech races with the run it would interrupt: the one without it."""
        moved = [
            index
            for index in (range(len(on["speeches"])) if speech_kinds else ())
            if wanted_speeches is None or index in wanted_speeches
        ]
        without = runner.ledgers([cases.without_speech(on, index) for index in moved], each)
        for index, alone in zip(moved, without):
            handles = speech_handles(on, index, produced, speech_kinds)
            if wanted_moments is not None:
                handles = [h for h in handles if h.moment in wanted_moments]
            if alone is not None:  # else the scenario cannot run without this speech
                place(on, handles, alone, base, coarse)

    def move_hangup(on: cases.Case, produced: list[Event], base: str, coarse: bool = False) -> None:
        """A hangup cuts short the run in which the user stays."""
        if cases.HANGUP not in kinds:
            return
        stays = produced if on["hangup_us"] is None else runner.ledger(cases.without_hangup(on))
        if stays is not None:
            place(on, [hangup_handle(on, stays)], stays, base, coarse)

    # One thing at a time: each delay against the run as it is, each speech against the
    # run without it, the hangup against the run in which the user stays.
    place(case, delay_handles(events, delay_kinds), events, "")
    move_speeches(case, events, "")
    move_hangup(case, events, "")

    # One fault at a time, on each write the run made.
    faulted: list[tuple[str, int, cases.Case]] = []  # (label, the write, the case with the fault)
    as_written: set[str] = set()  # the labels of timeouts whose write keeps the scenario's latency
    requests = [e["data"] for e in events if e["kind"] == "effect.requested"]
    for request in requests:
        nth = request["request"]
        for of in fault_kinds:
            with_one = cases.with_fault(case, of, nth, None if of == "backend.error" else wait_us)
            if with_one is None:
                continue  # the write already has a fault
            versions: list[tuple[str, cases.Case | None]] = [(f"{of} {nth}", with_one)]
            if of == "backend.timeout":
                # The write commits at its own latency: before the caller gives up, or
                # after. One order is the scenario's. The other is made by letting the
                # write take 1 s longer than the wait, or the caller wait 1 s longer than
                # the write.
                latency = request["latency_us"]
                if latency < wait_us:
                    other = cases.with_value(
                        with_one, Variable("backend.latency", nth), wait_us + 1_000_000
                    )
                    versions = [
                        (f"{of} {nth}, commit first", with_one),
                        (f"{of} {nth}, commit later", other),
                    ]
                else:
                    other = cases.with_value(
                        with_one, Variable(cases.WAIT, nth), latency + 1_000_000
                    )
                    versions = [
                        (f"{of} {nth}, commit later", with_one),
                        (f"{of} {nth}, commit first", other),
                    ]
                as_written.add(versions[0][0])
            for label, version in versions:
                if version is not None:
                    faulted.append((label, nth, version))
                    keep(Candidate(version, None, None, base=label))
    # ... and the moment the caller gives up, to every transition.
    under: dict[str, list[Event]] = {}
    with_faults = runner.ledgers([version for _label, _nth, version in faulted], each)
    for (label, nth, version), produced in zip(faulted, with_faults):
        if produced is None:
            continue
        under[label] = produced
        gave_up = next(
            (
                e
                for e in produced
                if e["kind"] == "effect.timed_out" and e["data"]["request"] == nth
            ),
            None,
        )
        if gave_up is not None:
            handle = Handle(
                Variable(cases.WAIT, nth), "gave_up", gave_up["t_us"], gave_up["data"]["after_us"]
            )
            place(version, [handle], produced, label)
        if label in as_written:
            # ... and the commit of a write whose caller gives up, to every transition.
            commits = [
                h for h in delay_handles(produced, ["backend.latency"]) if h.variable.index == nth
            ]
            place(version, commits, produced, label)

    if pairs:
        # A fault and a speech: every speech again, and the hangup, with each fault in place.
        for label, _nth, version in faulted:
            if label in under:
                move_speeches(version, under[label], label)
                move_hangup(version, under[label], label)
        # Two faults: on two different writes.
        for i, (label, nth, version) in enumerate(faulted):
            for other_label, other_nth, other in faulted[i + 1 :]:
                if other_nth == nth:
                    continue
                extra = next(f for f in other["faults"] if f["nth"] == other_nth)
                both = cases.with_fault(version, extra["of"], other_nth, extra["us"])
                # A timeout that commits later is a fault and a latency: carry both.
                commit = Variable("backend.latency", other_nth)
                later = cases.value(other, commit)
                if both is not None and later is not None and later != cases.value(case, commit):
                    both = cases.with_value(both, commit, later)
                if both is not None:
                    keep(Candidate(both, None, None, base=f"{label} + {other_label}"))
        # A slow provider and a speech: each write and each generation the run made is
        # stretched, one per version, and every speech and the hangup are moved against
        # the run of that version. A delay already that long is left as it is.
        to = min(stretch_us, delay_limit_us)
        slow: list[tuple[str, cases.Case]] = []
        for delay in delay_handles(events, STRETCHED):
            longer = cases.with_value(case, delay.variable, to)
            if delay.value_us < to and longer is not None:
                label = f"{delay.variable}, stretched"
                slow.append((label, longer))
                keep(Candidate(longer, None, None, base=label))
        slowed = runner.ledgers([longer for _label, longer in slow], each)
        for (label, longer), produced in zip(slow, slowed):
            if produced is not None:
                move_speeches(longer, produced, label, coarse=True)
                move_hangup(longer, produced, label, coarse=True)

    # Every speech again, under provider delays redrawn from a seed.
    applied = delay_handles(events, cases.DELAY_KINDS)
    redrawn: list[cases.Case] = []
    for number in range(1, profiles + 1):
        rng = random.Random(f"{case['seed']}:{number}")
        profile = case
        for handle in applied:
            most = min(delay_limit_us, max(2 * handle.value_us, 1_000_000))
            changed = cases.with_value(profile, handle.variable, rng.randrange(0, most + 1, 10_000))
            profile = changed if changed is not None else profile
        redrawn.append(profile)
    for number, (profile, produced) in enumerate(zip(redrawn, runner.ledgers(redrawn, each)), 1):
        if produced is not None:
            keep(Candidate(profile, None, None, base=f"profile {number}"))
            move_speeches(profile, produced, f"profile {number}")

    todo = list(found.values())
    outcomes = runner.many([c.case for c in todo], each)
    tried = [Tried(c, o) for c, o in zip(todo, outcomes)]
    return Exploration(case, original, tried, impossible, offsets, runner)


__all__ = [
    "DEFAULT_VARIABLES",
    "DELAY_LIMIT_US",
    "OFFSETS_US",
    "STRETCHED",
    "STRETCH_US",
    "WAIT_US",
    "Break",
    "Candidate",
    "Each",
    "Exploration",
    "Handle",
    "Outcome",
    "Placement",
    "Runner",
    "Transition",
    "Tried",
    "Window",
    "anchors",
    "attempt",
    "candidates",
    "delay_handles",
    "evaluate",
    "explore",
    "hangup_handle",
    "judged",
    "speech_handles",
    "transitions",
    "window",
]
