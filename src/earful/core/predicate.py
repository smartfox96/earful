"""What it means for two runs to fail the same way: the same contract and rule, such as
``L2/rule b``. The instance is left out, since searching and shrinking renumber speeches."""

from __future__ import annotations

from dataclasses import dataclass

from .checker import FAIL, UNOBSERVABLE, Report
from .checker.report import parse_rule


@dataclass(frozen=True, order=True)
class Predicate:
    """A contract rule that a run breaks."""

    contract: str
    rule: str

    def holds(self, report: Report) -> bool:
        """Whether ``report`` has a failed verdict of this contract that names this rule."""
        return self in failures(report)

    def __str__(self) -> str:
        return f"{self.contract}/{self.rule}"

    @classmethod
    def parse(cls, text: str) -> Predicate:
        """Raises ``ValueError`` for a rule no contract has."""
        return cls(*parse_rule(text))


def failures(report: Report) -> set[Predicate]:
    """Every contract rule ``report`` says was broken, and every code of evidence it lost,
    which a run fails on as on a rule. Empty for an invalid run."""
    broken = {
        Predicate(verdict.contract, finding.rule)
        for verdict in report.failures
        if verdict.outcome == FAIL
        for finding in verdict.findings
    }
    return broken | {Predicate.parse(name) for name in report.lost()}


def details_of(report: Report, predicate: Predicate) -> tuple[str, ...]:
    """What each finding by which ``report`` breaks ``predicate`` says, for a rule or a code
    of evidence lost."""
    result = report.results.get(predicate.contract)
    return tuple(
        finding.detail
        for verdict in (result.verdicts if result is not None else ())
        if verdict.outcome in (FAIL, UNOBSERVABLE)
        for finding in verdict.findings
        if predicate.rule in (finding.rule, finding.code)
    )


__all__ = ["Predicate", "details_of", "failures"]
