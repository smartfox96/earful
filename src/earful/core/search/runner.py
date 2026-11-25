"""Evaluate each case once and retain its ledger and verdicts."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from .. import case as cases
from ..checker import check
from ..ledger import ledger_hash
from ..predicate import Predicate, failures

Event = dict[str, Any]
Run = Callable[[cases.Case], list[Event]]


class CaseRefused(ValueError):
    """What a ``Run`` raises for a case its adapter will not run, before it runs anything."""


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
    session_scope: str = ""

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
        return Outcome(
            case,
            invalid=report.invalid,
            hash=ledger_hash(events),
            session_scope=report.session_scope,
        )
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
        session_scope=report.session_scope,
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
