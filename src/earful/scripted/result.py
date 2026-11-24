"""The evidence recorded by one run and the contracts it satisfies."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from functools import cached_property
from typing import Any

from ..core.checker import Report, check
from ..core.ledger import Ledger
from ..core.predicate import Predicate


@dataclass
class RunResult:
    case: str
    seed: int
    ledger: Ledger
    hash: str
    invalid: list[str]
    harness_errors: list[str]

    @property
    def events(self) -> list[dict[str, Any]]:
        return self.ledger.events

    @cached_property
    def report(self) -> Report:
        """The verdict of every contract for this run."""
        return check(self.ledger.events)

    def assert_contracts(
        self, *, allow: Iterable[Predicate | str] | str = (), require: Iterable[str] | str = ()
    ) -> None:
        """Raise ``ContractFailure``, an ``AssertionError``, if a contract rule broke.
        ``allow`` names rules that may break, as ``CONTRACT/RULE``. ``require`` names
        contracts, such as ``A2``, that must get a pass or a fail."""
        __tracebackhide__ = True  # pytest then shows the test's own line, not this one
        framework = self.ledger.events[0]["data"]["framework"]
        self.report.assert_contracts(
            allow=allow,
            require=require,
            what=f"{self.case} (seed {self.seed}, {framework['name']} {framework['version']})",
        )
