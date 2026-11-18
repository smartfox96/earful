"""What it means for two runs to fail the same way: the same contract and rule, such as
``L2/rule b``. The instance is left out, since searching and shrinking renumber speeches."""

from __future__ import annotations

from dataclasses import dataclass

from .checker import FAIL, Report


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
        contract, _, rule = text.partition("/")
        if not contract or not rule:
            raise ValueError(f"a predicate is CONTRACT/RULE, such as 'L2/rule b'; got {text!r}")
        return cls(contract, rule)


def failures(report: Report) -> set[Predicate]:
    """Every contract rule ``report`` says was broken. Empty for an invalid run."""
    return {
        Predicate(verdict.contract, finding.rule)
        for verdict in report.failures
        if verdict.outcome == FAIL
        for finding in verdict.findings
    }


__all__ = ["Predicate", "failures"]
