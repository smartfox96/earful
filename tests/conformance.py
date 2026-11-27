"""What every adapter does, each behaviour with one check over what its runs showed."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from shapes import corpus
from shapes.corpus import Expect, KnownDefect, Seen, defect_marks

RAISED = "raised by the application under test"  # what a scenario's own code raises
APART = 20.0  # real seconds for a behaviour whose failure is a run that does not end


@dataclass(frozen=True)
class Behaviour:
    """``check`` says what is wrong with the runs of one scenario, or None."""

    name: str
    check: Callable[[list[Seen]], str | None]
    runs: int = 1
    apart: float | None = None  # real seconds: each run in a process of its own, stopped then


@dataclass(frozen=True)
class Scenario:
    """A behaviour on one framework. ``case`` is a module's function: a run apart calls it."""

    case: Callable[[], Any]
    defect: Expect | None = None
    defect_on: tuple[str, ...] = ()


def _write_settles(seen: list[Seen]) -> str | None:
    (run,) = seen
    if run.ended != "judged":
        return f"the run is not judged: {run}"
    requested = {e["data"]["request"] for e in run.events if e["kind"] == "effect.requested"}
    settled = {
        e["data"]["request"]
        for e in run.events
        if e["kind"] in ("effect.committed", "effect.failed", "effect.deduplicated")
    }
    if not requested:
        return "the scenario made no backend write"
    left = sorted(requested - settled)
    return f"writes {left} have no outcome in the ledger" if left else None


def _raise_recorded(seen: list[Seen]) -> str | None:
    (run,) = seen
    if run.ended == "invalid" and RAISED in run.said:
        return None
    if any(RAISED in json.dumps(e["data"]) for e in run.events):
        return None
    return f"the exception is in neither the reasons nor the ledger: {run}"


def _ends_judged(seen: list[Seen]) -> str | None:
    (run,) = seen
    return None if run.ended == "judged" else f"{run}"


def _ends_invalid(seen: list[Seen]) -> str | None:
    (run,) = seen
    return None if run.ended == "invalid" else f"{run}"


def _tool_choice_kept(seen: list[Seen]) -> str | None:
    (run,) = seen
    if run.ended == "invalid" and "tool_choice" in run.said:
        return None
    return f"the scripted model called a tool the request did not allow, and the run is {run}"


def _one_ledger(seen: list[Seen]) -> str | None:
    if any(run.ended != "judged" for run in seen):
        return f"a run is not judged: {', '.join(map(str, seen))}"
    hashes = {run.hash for run in seen}
    return None if len(hashes) == 1 else f"the runs give {len(hashes)} ledgers"


def _heard_after_change(seen: list[Seen]) -> str | None:
    (run,) = seen
    if run.ended != "judged":
        return f"the run is not judged: {run}"
    speeches = run.events[0]["data"]["scenario"]["speeches"]
    finals = {e["data"]["speech"] for e in run.events if e["kind"] == "user.transcript_final"}
    missing = [s["speech"] for s in speeches if s["transcript"] and s["speech"] not in finals]
    return f"speeches {missing} have no transcript in the ledger" if missing else None


WRITE_SETTLES = Behaviour("a write in flight at the end settles", _write_settles)
RAISE_RECORDED = Behaviour("an exception in application code is recorded", _raise_recorded)
CLOSED_ENDS = Behaviour("a closed session ends the run", _ends_judged, apart=APART)
STUCK_CEILING = Behaviour("a stuck pipeline hits the ceiling", _ends_invalid, apart=APART)
TOOL_CHOICE = Behaviour("tool_choice is honoured", _tool_choice_kept)
ONE_LEDGER = Behaviour("two runs give one ledger", _one_ledger, runs=2)
HEARD = Behaviour("the user is still heard after the active agent changes", _heard_after_change)
BEHAVIOURS = (
    WRITE_SETTLES,
    RAISE_RECORDED,
    CLOSED_ENDS,
    STUCK_CEILING,
    TOOL_CHOICE,
    ONE_LEDGER,
    HEARD,
)


def params(scenarios: dict[Behaviour, Scenario], release: str) -> list[Any]:
    """One test parameter per behaviour, marked where its scenario declares a defect."""
    return [
        pytest.param(
            behaviour,
            id=behaviour.name.replace(" ", "-"),
            marks=[
                *defect_marks(scenarios[behaviour].defect, scenarios[behaviour].defect_on, release),
                *([pytest.mark.slow] if behaviour.apart else []),
            ],
        )
        for behaviour in BEHAVIOURS
        if behaviour in scenarios
    ]


def observe(
    behaviour: Behaviour, scenario: Scenario, adapter: str, run_case: Callable[..., Any]
) -> list[Seen]:
    """The runs ``behaviour`` needs of ``scenario``. ``adapter`` names ``run_case``."""

    def once() -> Seen:
        if behaviour.apart is None:
            return corpus.observe(run_case, scenario.case())
        case = scenario.case
        return corpus.observe_apart(case.__module__, case.__name__, adapter, behaviour.apart)

    return [once() for _ in range(behaviour.runs)]


def judge(behaviour: Behaviour, scenario: Scenario, seen: list[Seen]) -> None:
    """Return if the behaviour holds; raise KnownDefect if every run shows the declared defect."""
    __tracebackhide__ = True
    wrong = behaviour.check(seen)
    if wrong is None:
        return
    if scenario.defect is not None and all(scenario.defect.matches(run) for run in seen):
        raise KnownDefect(f"{behaviour.name}: {wrong}")
    known = f"; the defect it declares: {scenario.defect}" if scenario.defect else ""
    raise AssertionError(f"{behaviour.name}: {wrong}{known}")
