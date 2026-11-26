"""Measure the interval over which a case breaks one rule."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .. import case as cases
from ..case import Variable
from ..checker import FAIL, PASS
from ..predicate import Predicate
from .runner import Runner


@dataclass
class Window:
    """How far a variable can move while the run still breaks a rule. Each end says why
    probing stopped: ``passes`` (a run beyond it was judged and does not break the rule),
    ``blocked`` (no case beyond it exists or can be judged) or ``reach`` (the search gave up).
    ``under`` names what was changed before the variable was moved, such as a fault."""

    variable: Variable
    lo_us: int
    hi_us: int
    lo_end: str
    hi_end: str
    resolution_us: int
    # Why the run beyond a blocked end could not be judged, where one exists.
    lo_unjudged: str | None = None
    hi_unjudged: str | None = None
    under: str = ""

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
            **({"under": self.under} if self.under else {}),
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
        if predicate in outcome.failures:
            return True
        judged = outcome.outcomes.get(predicate.contract)
        if judged not in (PASS, FAIL):
            # The run ended before the rule could be judged, or lost what it needed.
            codes = ", ".join(outcome.missing.get(predicate.contract, ())) or "no instance"
            return f"{predicate.contract} {str(judged).replace('_', ' ')} ({codes})"
        return False

    def end(direction: int, resolution: int, reach: int) -> _End:
        """The farthest breaking value in one direction, why the search stopped, and why the
        run beyond could not be judged."""
        good, step = at, resolution
        while True:
            step = min(step, reach)
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
        one.under,
    )
