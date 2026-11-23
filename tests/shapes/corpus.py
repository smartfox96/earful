"""What a run showed, what a shape expects of it, and how a shape is run. No framework here."""

from __future__ import annotations

import dataclasses
import importlib
import json
import pkgutil
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

TESTS = Path(__file__).resolve().parents[1]
OUTCOMES = ("pass", "fail", "unobservable", "not_exercised")
_SEEN = "seen: "  # the line a run in a process of its own prints its result on
_CONTRACT = re.compile(r"[AML][0-9]")


class KnownDefect(AssertionError):
    """The run shows what the shape's DEFECT says Earful does."""


@dataclass(frozen=True)
class Seen:
    """What one run showed. ``ended`` is judged, invalid, refused, raised or hung."""

    ended: str
    outcomes: dict[str, str] = field(default_factory=dict)
    broken: tuple[str, ...] = ()
    lost: tuple[str, ...] = ()
    said: str = ""  # the reasons of an invalid run, a refusal or an error
    seconds: float = 0.0  # real seconds
    hash: str = ""
    events: tuple[dict[str, Any], ...] = ()

    def verdicts(self) -> tuple[Any, ...]:
        """What a variation of the scenario must leave as it is."""
        return (self.ended, self.outcomes, self.broken, self.lost)

    def __str__(self) -> str:
        if self.ended == "hung":
            return f"did not end within {self.seconds:g} s"
        if self.ended != "judged":
            return f"{self.ended}: {self.said}"
        shown = [f"{c} {o}" for c, o in self.outcomes.items() if o not in ("pass", "not_exercised")]
        text = "judged: " + (", ".join(shown) or "nothing fails")
        if self.broken:
            text += f"; breaks {', '.join(self.broken)}"
        if self.lost:
            text += f"; lost {', '.join(self.lost)}"
        return text


@dataclass(frozen=True)
class Expect:
    """What a run should show, or what it shows while Earful is wrong."""

    ended: str  # as Seen.ended, or "ends": any end within ``within`` real seconds
    contracts: tuple[tuple[str, str], ...] = ()
    said: str = ""  # a part of the message
    within: float = 0.0
    then: Expect | None = None

    def matches(self, seen: Seen) -> bool:
        if self.ended == "ends":
            return seen.ended != "hung" and (self.then is None or self.then.matches(seen))
        if seen.ended != self.ended:
            return False
        if self.ended == "judged":
            return _judged_as(dict(self.contracts), seen)
        return self.said in seen.said

    def __str__(self) -> str:
        if self.ended == "ends":
            return f"ends within {self.within:g} s" + (f", {self.then}" if self.then else "")
        if self.ended == "hung":
            return "does not end"
        if self.ended == "judged":
            named = ", ".join(f"{c} {o}" for c, o in self.contracts)
            return f"judged: {named or 'nothing fails'}" + (", nothing else fails" * bool(named))
        return f"{self.ended}: {self.said}"


def judged(**contracts: str) -> Expect:
    """Outcomes by contract, or the rules one breaks (``M2="rule c"``); no other one fails."""
    for contract in contracts:
        assert _CONTRACT.fullmatch(contract), f"no contract is called {contract}"
    return Expect("judged", tuple(sorted(contracts.items())))


def invalid(reason: str) -> Expect:
    return Expect("invalid", said=reason)


def refused(message: str) -> Expect:
    return Expect("refused", said=message)


def raises(error: str) -> Expect:
    """The run raises: ``error`` is a part of ``Type: message``."""
    return Expect("raised", said=error)


def hangs() -> Expect:
    return Expect("hung")


def ends_within(seconds: float, then: Expect | None = None) -> Expect:
    """The run ends within ``seconds`` real seconds: the shape may hang, so it runs apart."""
    return Expect("ends", within=seconds, then=then)


def _judged_as(expected: dict[str, str], seen: Seen) -> bool:
    if not expected.keys() <= seen.outcomes.keys():
        return False
    for contract, outcome in seen.outcomes.items():
        want = expected.get(contract)
        if want is None:
            if outcome not in ("pass", "not_exercised"):
                return False
        elif want in OUTCOMES:
            if outcome != want:
                return False
        else:
            rules = {f"{contract}/{rule.strip()}" for rule in want.split(",")}
            mine = {rule for rule in seen.broken if rule.startswith(f"{contract}/")}
            if outcome != "fail" or mine != rules:
                return False
    return True


def judge(should: Expect, defect: Expect | None, seen: Seen) -> None:
    """Return on ``should``, raise KnownDefect on ``defect`` and AssertionError otherwise."""
    __tracebackhide__ = True
    if should.matches(seen):
        return
    if defect is not None and defect.matches(seen):
        raise KnownDefect(f"{defect}")
    known = f"; the defect it declares: {defect}" if defect else ""
    raise AssertionError(f"should: {should}{known}; got {seen}")


def defect_marks(
    defect: Expect | None, on: Iterable[str], release: str
) -> list[pytest.MarkDecorator]:
    """A strict expected failure if ``defect`` shows on ``release``: ``on`` is empty or has it."""
    on = tuple(on)
    if defect is None or (on and release not in on):
        return []
    return [pytest.mark.xfail(strict=True, raises=KnownDefect, reason=f"Earful: {defect}")]


@dataclass(frozen=True)
class Shape:
    """A module of shapes/<framework>/: an application, the case that runs it, and SHOULD."""

    name: str
    module: str
    about: str
    should: Expect
    defect: Expect | None = None
    defect_on: tuple[str, ...] = ()  # the releases DEFECT shows on; empty for every one
    defect_varied: dict[str, Expect] = field(default_factory=dict)
    not_varied: dict[str, str] = field(default_factory=dict)  # variation -> why it does not apply

    @property
    def apart(self) -> float | None:
        """Real seconds a shape that may hang has, in a process of its own."""
        return self.should.within if self.should.ended == "ends" else None


def load(framework: str) -> list[Shape]:
    """Every shape of ``framework``, by module name."""
    package = importlib.import_module(f"shapes.{framework}")
    shapes = []
    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda i: i.name):
        name = f"shapes.{framework}.{info.name}"
        module = importlib.import_module(name)
        shapes.append(
            Shape(
                name=info.name,
                module=name,
                about=(module.__doc__ or "").strip(),
                should=module.SHOULD,
                defect=getattr(module, "DEFECT", None),
                defect_on=tuple(getattr(module, "DEFECT_ON", ())),
                defect_varied=dict(getattr(module, "DEFECT_VARIED", {})),
                not_varied=dict(getattr(module, "NOT_VARIED", {})),
            )
        )
    return shapes


def check_declared(shapes: list[Shape], variations: Iterable[str]) -> None:
    """Each shape says what the application does, and what it declares fits together."""
    assert shapes, "no shapes"
    for shape in shapes:
        name = shape.name
        assert shape.about, f"{name}: the module's docstring says what the application does"
        assert not re.search(r"\b[A-Z]{2}-[0-9]", shape.about), f"{name}: name the behaviour"
        assert shape.defect != shape.should, f"{name}: DEFECT is the same as SHOULD"
        hangs = shape.defect is not None and shape.defect.ended == "hung"
        assert shape.apart or not hangs, f"{name}: a defect that hangs needs ends_within"
        assert shape.defect or not shape.defect_on, f"{name}: DEFECT_ON without DEFECT"
        unknown = (shape.defect_varied.keys() | shape.not_varied.keys()) - set(variations)
        assert not unknown, f"{name}: no variation is called {sorted(unknown)}"
        assert all(shape.not_varied.values()), f"{name}: NOT_VARIED says why"


def params(shapes: list[Shape], release: str) -> list[Any]:
    """One test parameter per shape. One that may hang is slow: it can take its whole bound."""
    return [
        pytest.param(
            shape,
            id=shape.name,
            marks=[
                *defect_marks(shape.defect, shape.defect_on, release),
                *([pytest.mark.slow] if shape.apart else []),
            ],
        )
        for shape in shapes
    ]


def observe(run_case: Callable[..., Any], case: Any, *, seed: int = 0) -> Seen:
    """Run ``case`` in this process."""
    started = time.perf_counter()
    try:
        result = run_case(case, seed, require_valid=False)
    except ValueError as exc:  # both adapters refuse a case with one, before it runs
        return Seen("refused", said=str(exc), seconds=time.perf_counter() - started)
    except Exception as exc:  # noqa: BLE001 - an error of the run is what it showed
        said = f"{type(exc).__name__}: {exc}"
        return Seen("raised", said=said, seconds=time.perf_counter() - started)
    seconds = time.perf_counter() - started
    events = tuple(result.ledger.events)
    if result.invalid or not result.report.valid:
        said = "; ".join(result.invalid or result.report.invalid)
        return Seen("invalid", said=said, seconds=seconds, hash=result.hash, events=events)
    report = result.report
    return Seen(
        "judged",
        outcomes=report.outcomes(),
        broken=tuple(report.broken()),
        lost=tuple(report.lost()),
        seconds=seconds,
        hash=result.hash,
        events=events,
    )


def observe_apart(module: str, function: str, adapter: str, seconds: float) -> Seen:
    """Run ``module.function()``'s case with ``adapter`` ("package:name") in another process."""
    command = [sys.executable, "-c", "from shapes import corpus; corpus.main()"]
    started = time.perf_counter()
    try:
        done = subprocess.run(
            [*command, module, function, adapter],
            cwd=TESTS,  # so that "shapes" and the test modules import
            capture_output=True,
            text=True,
            timeout=seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Seen("hung", seconds=seconds)
    lines = [line for line in done.stdout.splitlines() if line.startswith(_SEEN)]
    if not lines:
        last = (done.stderr.strip().splitlines() or ["nothing on stderr"])[-1]
        said = f"the process ended with status {done.returncode}: {last}"
        return Seen("raised", said=said, seconds=time.perf_counter() - started)
    data = json.loads(lines[-1].removeprefix(_SEEN))
    data |= {key: tuple(data[key]) for key in ("broken", "lost", "events")}
    return Seen(**data | {"seconds": time.perf_counter() - started})


def main() -> None:
    """In a process of its own: run the case of ``module.function()`` and print what it showed."""
    module, function, adapter = sys.argv[1:4]
    package, name = adapter.split(":")
    run_case = getattr(importlib.import_module(package), name)
    case = getattr(importlib.import_module(module), function)()
    print(_SEEN + json.dumps(dataclasses.asdict(observe(run_case, case))), flush=True)


def run(shape: Shape, adapter: str, run_case: Callable[..., Any]) -> Seen:
    """Run ``shape`` once, apart if it may hang."""
    if shape.apart is not None:
        return observe_apart(shape.module, "case", adapter, shape.apart)
    return observe(run_case, importlib.import_module(shape.module).case())
