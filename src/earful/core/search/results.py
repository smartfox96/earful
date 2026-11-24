"""The plan, cases and findings of a search."""

from __future__ import annotations

from dataclasses import dataclass

from .. import case as cases
from ..predicate import Predicate
from .runner import Outcome
from .space import Candidate
from .windows import Window


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
