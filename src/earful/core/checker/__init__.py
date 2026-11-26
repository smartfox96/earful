"""The contract checker: decides the seven contracts of docs/contracts.md from a ledger."""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from typing import Any

from ..ledger import session_scope
from ..schema import validity
from .contracts import DECLARED
from .l1 import open_deadline
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
    pending, unapplied = _left(events)
    errors = tuple(
        f"Tool {info['name']} raised {info['error']}: {info['message']}"
        for event in events
        if isinstance(event, dict) and event.get("kind") == "tool.raised"
        for info in [event.get("data")]
        if isinstance(info, dict)
        and all(isinstance(info.get(key), str) for key in ("name", "error", "message"))
    )
    if reasons:
        return Report(
            run=run,
            valid=False,
            invalid=tuple(reasons),
            results={},
            session_scope=scope,
            tool_errors=errors,
            pending=pending,
            unapplied=unapplied,
        )
    reading, possible, results = _judge(events)
    return Report(
        run=run,
        valid=True,
        invalid=(),
        results=results,
        reading=reading,
        readings=possible,
        tool_errors=errors,
        session_scope=scope,
        pending=pending,
        unapplied=unapplied,
    )


def _left(events: list[dict[str, Any]]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """What run.end lists as left running, and as declared but never applied."""
    end = events[-1] if events and isinstance(events[-1], dict) else {}
    data = end.get("data") if end.get("kind") == "run.end" else None
    if not isinstance(data, dict):
        return (), ()
    pending, unapplied = data.get("pending", []), data.get("unapplied", [])
    return (
        tuple(p for p in pending if isinstance(p, str)) if isinstance(pending, list) else (),
        tuple(u for u in unapplied if isinstance(u, str)) if isinstance(unapplied, list) else (),
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
    "open_deadline",
]
