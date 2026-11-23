"""Variations of a scenario that leave its verdicts as they are, applied to every shape."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from shapes.corpus import KnownDefect, Seen, Shape

from earful.scripted.case import Delay

SHIFTED = "every time shifted"
LONGER = "a declared delay longer"
HISTORY = "a starting history"
TWO_PARTS = "a reply voiced in two parts"
NAMES = (SHIFTED, LONGER, HISTORY, TWO_PARTS)

SHIFT = 1.25  # seconds added to every time the scenario declares
LONGER_BY = 2.0  # seconds added to every model call and every backend write


@dataclass(frozen=True)
class Variation:
    """How a case is varied, from the case and what its run showed, and what must hold."""

    name: str
    vary: Callable[[Any, Seen], Any] | None  # None where it does not apply, for ``why``
    holds: Callable[[Seen, Seen], str | None]  # what changed that must not have, or None
    why: str = ""


def same_verdicts(original: Seen, varied: Seen) -> str | None:
    if original.verdicts() == varied.verdicts():
        return None
    return f"{original} became {varied}"


def no_fail_passes(original: Seen, varied: Seen) -> str | None:
    """A contract that failed still fails, or lost its evidence: it never passes."""
    if original.ended != "judged":
        return None
    if varied.ended != "judged":
        return f"{original} became {varied}"
    better = [
        contract
        for contract, outcome in original.outcomes.items()
        if outcome == "fail" and varied.outcomes.get(contract) not in ("fail", "unobservable")
    ]
    if not better:
        return None
    return ", ".join(f"{c} failed and became {varied.outcomes.get(c)}" for c in better)


def _shifted(case: Any, original: Seen) -> Any:
    speeches = [
        dataclasses.replace(s, start=s.start + SHIFT, end=s.end + SHIFT) for s in case.speeches
    ]
    hangup = None if case.hangup is None else case.hangup + SHIFT
    return dataclasses.replace(case, speeches=speeches, hangup=hangup)


def _longer(case: Any, original: Seen) -> Any:
    """Every model call and backend write of the run, declared ``LONGER_BY`` longer."""
    longer: dict[tuple[str, int], float] = {}
    for event in original.events:
        data = event["data"]
        if event["kind"] == "llm.response":
            longer["llm.duration", data["call"]] = data["duration_us"] / 1e6 + LONGER_BY
        elif event["kind"] == "effect.requested":
            longer["backend.latency", data["request"]] = data["latency_us"] / 1e6 + LONGER_BY
    kept = [d for d in case.delays if (d.of, d.nth) not in longer]
    made = [Delay(of, nth, seconds) for (of, nth), seconds in sorted(longer.items())]
    return dataclasses.replace(case, delays=kept + made)


SHIFT_EVERY_TIME = Variation(SHIFTED, _shifted, same_verdicts)
LONGER_DELAYS = Variation(LONGER, _longer, no_fail_passes)


def earlier_exchange(case: Any, original: Seen) -> tuple[str, str]:
    """An earlier call's turn and reply in this run's words: the hardest history to tell apart."""
    user = next((s.transcript for s in case.speeches if s.transcript), "Hello.")
    replies = [e["data"]["text"] for e in original.events if e["kind"] == "llm.response"]
    return user, next((text for text in replies if text), "Hello, how can I help?")


def params(shapes: list[Shape], variations: list[Variation]) -> list[Any]:
    """A test parameter per shape and variation that fits it; a shape that may hang has none."""
    out = []
    for shape in shapes:
        for variation in variations:
            if variation.vary is None or shape.apart or variation.name in shape.not_varied:
                continue
            marks = []
            if variation.name in shape.defect_varied:
                reason = f"Earful, {variation.name}: {shape.defect_varied[variation.name]}"
                marks.append(pytest.mark.xfail(strict=True, raises=KnownDefect, reason=reason))
            named = f"{shape.name}-{variation.name.replace(' ', '-')}"
            out.append(pytest.param(shape, variation, id=named, marks=marks))
    return out


def judge(shape: Shape, variation: Variation, original: Seen, varied: Seen) -> None:
    """Return if nothing changed that must not; raise KnownDefect if the shape declares it."""
    __tracebackhide__ = True
    changed = variation.holds(original, varied)
    if changed is None:
        return
    declared = shape.defect_varied.get(variation.name)
    if declared is not None and declared.matches(varied):
        raise KnownDefect(f"{variation.name}: {changed}")
    raise AssertionError(f"{variation.name}: {changed}")
