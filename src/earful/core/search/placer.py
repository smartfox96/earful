"""Plan placements, run them within the budget and collect their outcomes."""

from __future__ import annotations

import os
import random
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .. import case as cases
from ..case import Variable
from .coverage import Exploration, Plan, Tried, Unread
from .runner import Each, Event, Outcome, Run, Runner
from .space import (
    _MOVED_BY,
    DELAY_LIMIT_US,
    OFFSETS_US,
    STRETCH_US,
    STRETCHED,
    Candidate,
    Handle,
    Placement,
    candidates,
    delay_handles,
    hangup_handle,
    speech_handles,
    transitions,
)

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
