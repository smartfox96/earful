"""The contract checker: decides the seven contracts of docs/contracts.md from a ledger.

    report = check(events)
    report.outcomes()   # {"A1": "not_exercised", "A2": "fail", ...}
    report.failures     # every failed verdict, with the events it rests on

The checker reads only the ledger. It never imports the scenario or the framework under
test, and it does not use the framework's own account of what happened: ``fw.*`` events
decide nothing.

Framework-independent: this package must never import livekit.
"""

from __future__ import annotations

from typing import Any

from ..schema import validity
from . import a1, a2, a3, l1, l2, m1, m2
from .policy import POLICIES, PolicyError, parse_all
from .report import (
    CONTRACTS,
    FAIL,
    NOT_EXERCISED,
    PASS,
    UNOBSERVABLE,
    ContractResult,
    Finding,
    Report,
    Verdict,
)
from .view import Reading, View, readings

_CHECKS = {"A1": a1.check, "A2": a2.check, "A3": a3.check, "M1": m1.check, "M2": m2.check, "L1": l1.check, "L2": l2.check}


def check(events: list[dict[str, Any]]) -> Report:
    """Check every contract against one run's ledger.

    An invalid run yields no verdicts: the report says why and its results are empty.
    Policy values come from the ledger's header; a scenario that declares one a contract
    does not define raises ``PolicyError``.
    """
    reasons = validity(events)
    run = events[0].get("run") if events and isinstance(events[0], dict) else None
    if reasons:
        return Report(run=run, valid=False, invalid=tuple(reasons), results={})
    policies = parse_all(events[0]["data"]["contracts"])
    # Where the ledger cannot say which of several identical replies a text was, every
    # reading is checked and the one most favourable to the system is reported: fewest
    # failed verdicts, then fewest unobservable. A run fails only if every reading does.
    possible = readings(events)
    best: tuple[tuple[int, int], Reading, dict[str, ContractResult]] | None = None
    for reading in possible:
        view = View(events, reading)
        results = {name: _CHECKS[name](view, policies[name]) for name in POLICIES}
        verdicts = [v for result in results.values() for v in result.verdicts]
        score = (
            sum(v.outcome == FAIL for v in verdicts),
            sum(v.outcome == UNOBSERVABLE for v in verdicts),
        )
        if best is None or score < best[0]:
            best = (score, reading, results)
    assert best is not None
    _score, reading, results = best
    return Report(run=run, valid=True, invalid=(), results=results, reading=reading, readings=len(possible))


__all__ = [
    "CONTRACTS",
    "FAIL",
    "NOT_EXERCISED",
    "PASS",
    "UNOBSERVABLE",
    "ContractResult",
    "Finding",
    "PolicyError",
    "Report",
    "Verdict",
    "View",
    "check",
]
