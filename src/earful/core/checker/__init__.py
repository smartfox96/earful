"""The contract checker: decides the seven contracts of docs/contracts.md from a ledger."""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from typing import Any

from ..ledger import session_scope
from ..schema import validity
from .contracts import DECLARED
from .policy import PolicyError, parse_all
from .report import (
    CONTRACTS,
    FAIL,
    NOT_EXERCISED,
    PASS,
    UNOBSERVABLE,
    ContractFailure,
    ContractResult,
    Finding,
    Report,
    Verdict,
    split,
)
from .view import Reading, View, readings

_CHECKS: dict[str, Callable[[View, Any], ContractResult]] = {
    name: import_module(f"{__name__}.{name.lower()}").check for name in DECLARED
}


def check(events: list[dict[str, Any]]) -> Report:
    """Check every contract against one run's ledger; an invalid run yields no results.

    Raises ``PolicyError`` for a declared policy value its contract does not define.
    """
    reasons = validity(events)
    run = events[0].get("run") if events and isinstance(events[0], dict) else None
    data = events[0].get("data") if events and isinstance(events[0], dict) else None
    scope = session_scope(data) if isinstance(data, dict) else ""
    if reasons:
        return Report(run=run, valid=False, invalid=tuple(reasons), results={}, session_scope=scope)
    reading, possible, results = _judge(events)
    return Report(
        run=run,
        valid=True,
        invalid=(),
        results=results,
        reading=reading,
        readings=possible,
        session_scope=scope,
    )


def _judge(events: list[dict[str, Any]]) -> tuple[Reading, int, dict[str, ContractResult]]:
    policies = parse_all(events[0]["data"]["contracts"])
    # Every reading is checked; the one with fewest failed, then fewest unobservable, is reported.
    possible = readings(events)
    checked: list[dict[str, ContractResult]] = []
    best: tuple[tuple[int, int], Reading, dict[str, ContractResult]] | None = None
    for reading in possible:
        view = View(events, reading)
        results = {name: check(view, policies[name]) for name, check in _CHECKS.items()}
        checked.append(results)
        verdicts = [v for result in results.values() for v in result.verdicts]
        score = (
            sum(v.outcome == FAIL for v in verdicts),
            sum(v.outcome == UNOBSERVABLE for v in verdicts),
        )
        if best is None or score < best[0]:
            best = (score, reading, results)
    assert best is not None
    _score, reading, results = best
    return reading, len(possible), split(results, checked)


__all__ = [
    "CONTRACTS",
    "FAIL",
    "NOT_EXERCISED",
    "PASS",
    "UNOBSERVABLE",
    "ContractFailure",
    "ContractResult",
    "Finding",
    "PolicyError",
    "Report",
    "Verdict",
    "View",
    "check",
]
