"""The rules and evidence that decide whether a run passes."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .checker.report import Report


@dataclass(frozen=True)
class Decision:
    broken: tuple[str, ...]
    lost: tuple[str, ...]
    unjudged: bool

    @property
    def status(self) -> int:
        if self.broken:
            return 1
        return 3 if self.lost or self.unjudged else 0


def decide(broken: Iterable[str], lost: Iterable[str], unjudged: bool, allow: set[str]) -> Decision:
    return Decision(tuple(sorted(set(broken) - allow)), tuple(sorted(set(lost) - allow)), unjudged)


def reports_outcome(reports: Iterable[Report], allow: set[str], require: list[str]) -> Decision:
    broken, lost = set[str](), set[str]()
    unjudged = False
    for report in reports:
        broken.update(report.broken())
        lost.update(report.lost())
        unjudged |= not report.valid or bool(report.unjudged(require))
    return decide(broken, lost, unjudged, allow)
