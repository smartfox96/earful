"""The pytest plugin: the ``earful`` fixture, and the evidence of a failing test.
Pytest loads it wherever Earful is installed, so the adapter is imported only when used."""

from __future__ import annotations

import contextlib
import json
import re
import shutil
from collections.abc import Callable, Generator, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from . import _adapters
from .core import bundle, html
from .core.ledger import write_jsonl

if TYPE_CHECKING:
    from .core.predicate import Predicate
    from .core.search import Exploration, Plan
    from .livekit.harness import RunResult as LiveKitResult
    from .livekit.scenario import Scenario as LiveKitScenario
    from .livekit.scenario import Source as LiveKitSource
    from .pipecat.harness import RunResult as PipecatResult
    from .pipecat.scenario import Scenario as PipecatScenario
    from .pipecat.scenario import Source as PipecatSource

    Source = LiveKitSource | PipecatSource | LiveKitScenario | PipecatScenario

_KEY = pytest.StashKey["Earful"]()


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("earful", "voice-agent regression tests")
    group.addoption(
        "--earful-out",
        default=".earful",
        metavar="DIR",
        help="where the ledgers and cases of failing tests are written (default: .earful)",
    )
    group.addoption(
        "--earful-workers",
        type=_workers,
        default=None,
        metavar="N",
        help="worker processes for a search that does not say how many; 0 runs it in "
        "the test's process (default: auto, one per processor for a scenario that can "
        "be imported, and the test's process otherwise)",
    )


def _workers(text: str) -> int | None:
    if text == "auto":
        return None
    try:
        workers = int(text)
    except ValueError:
        workers = -1
    if workers < 0:
        raise pytest.UsageError(f"--earful-workers takes a number of processes or auto; got {text}")
    return workers


class Earful:
    """What a test is given as ``earful``. It remembers what the test ran."""

    def __init__(self, directory: Path, workers: int | None, *, spread: bool = False) -> None:
        self.directory = directory  # where this test's evidence goes if it fails
        self.workers = workers  # None: as many as suits each search
        self.spread = spread  # this test run already uses the processors (pytest-xdist)
        self.runs: list[LiveKitResult | PipecatResult] = []
        self.searches: list[Exploration] = []

    def run(
        self, scenario: Source, *, data: dict[str, Any] | None = None, seed: int | None = None
    ) -> LiveKitResult | PipecatResult:
        """Run a scenario with its framework adapter."""
        adapter, source = _adapters.select(scenario)

        try:
            result = adapter.run(source, data=data, seed=seed)
        except adapter.InvalidRun as exc:
            self.runs.append(exc.result)
            raise
        self.runs.append(result)
        return result

    def explore(
        self,
        scenario: Source,
        *,
        data: dict[str, Any] | None = None,
        seed: int | None = None,
        workers: int | None = None,
        variables: Iterable[str] | None = None,
        faults: Iterable[str] | None = None,
        pairs: bool = False,
        profiles: int = 0,
        speeches: Iterable[int] | None = None,
        moments: Iterable[str] | None = None,
        words: bool = True,
        max_runs: int | None = None,
        planned: Callable[[Plan], None] | None = None,
        delay_limit_us: int | None = None,
        wait_us: int | None = None,
        stretch_us: int | None = None,
    ) -> Exploration:
        """Search a scenario with the adapter's ``explore``; what is left out is its default.
        Where neither ``workers`` nor the command line says how many, the adapter chooses,
        unless pytest-xdist spread the test run, which searches in-process."""
        adapter, source = _adapters.select(scenario)
        options: dict[str, Any] = {
            "data": data,
            "seed": seed,
            "pairs": pairs,
            "profiles": profiles,
            "speeches": speeches,
            "moments": moments,
            "words": words,
            "max_runs": max_runs,
            "planned": planned,
        }
        given = {
            "variables": variables,
            "faults": faults,
            "delay_limit_us": delay_limit_us,
            "wait_us": wait_us,
            "stretch_us": stretch_us,
        }
        options.update({name: value for name, value in given.items() if value is not None})
        if workers is None:
            workers = self.workers  # what the command line says
        if workers is None and self.spread:
            workers = 0  # pytest-xdist already runs a test on each processor
        found = adapter.explore(source, workers=workers, **options)
        self.searches.append(found)
        return found

    def keep(
        self,
        found: Exploration,
        rule: Predicate | str,
        directory: Path | str | None = None,
        *,
        repeats: int = 3,
    ) -> bundle.Bundle:
        """Keep a failure as a bundle. The default directory is
        under this test's evidence directory, named after the rule."""
        from .scripted.scenario import importable

        reference = importable(found.case["scenario"], "keep a failure as a bundle")
        adapter, _ = _adapters.select(reference)
        if directory is None:
            directory = self.directory / ("bundle." + _slug(str(rule)))
        return adapter.keep(found, rule, directory, repeats=repeats)

    def replay(self, directory: Path | str, *, repeats: int = 3) -> bundle.Replay:
        """Run a bundle's reduced case with its recorded framework."""
        kept = bundle.load(Path(directory))
        adapter = _adapters.by_framework(kept.manifest["framework"]["name"])
        return adapter.replay(kept, repeats=repeats)

    def evidence(self) -> list[str]:
        """Write what the test ran to its directory and return a line for each file."""
        lines: list[str] = []

        def write(name: str, events: list[dict[str, Any]]) -> None:
            ledger, report = (
                self.directory / f"{name}.ledger.jsonl",
                self.directory / f"{name}.html",
            )
            write_jsonl(events, ledger, normalized=True)
            report.write_text(html.render(events, title=name), encoding="utf-8")
            lines.append(f"    ledger  {_shown(ledger)}")
            lines.append(f"    report  {_shown(report)}")

        for number, result in enumerate(self.runs, 1):
            broke = ", ".join(result.report.broken()) or (
                "nothing" if result.report.valid else "not valid evidence"
            )
            lines.append(f"run {number} ({result.case}) breaks {broke}")
            write(f"run-{number}.{_slug(result.case)}", result.events)
        for number, found in enumerate(self.searches, 1):
            header = (found.runner.ledger(found.case) or [{}])[0].get("data", {})
            case = header.get("case", "case")
            for one in found.breaks():
                name = f"search-{number}.{_slug(case)}.{_slug(str(one.predicate))}"
                saved = self.directory / f"{name}.case.json"
                saved.parent.mkdir(parents=True, exist_ok=True)
                saved.write_text(json.dumps(one.example, indent=2, sort_keys=True) + "\n")
                lines.append(f"search {number} ({case}) breaks {one.predicate}: {one.what}")
                lines.append(f"    case    {_shown(saved)}")
                ledger = found.runner.ledger(one.example)
                if ledger is not None:
                    write(name, ledger)
        return lines


def _shown(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-") or "unnamed"


@pytest.fixture
def earful(request: pytest.FixtureRequest) -> Earful:
    """Runs and searches voice-agent scenarios, and keeps the evidence if the test fails."""
    config = request.config
    out = Path(config.getoption("--earful-out"))
    if not out.is_absolute():
        out = config.rootpath / out
    directory = out / _slug(request.node.nodeid)
    # What an earlier run of this test left is not evidence of this one.
    shutil.rmtree(directory, ignore_errors=True)
    with contextlib.suppress(OSError):
        out.rmdir()  # only if nothing else is in it
    helper = Earful(
        directory,
        config.getoption("--earful-workers"),
        spread=hasattr(config, "workerinput"),  # set in a worker process of pytest-xdist
    )
    request.node.stash[_KEY] = helper
    return helper


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, Any, None]:
    outcome = yield
    report = outcome.get_result()
    helper = item.stash.get(_KEY, None)
    if helper is None or report.when != "call" or not report.failed:
        return
    try:
        lines = helper.evidence()
    except Exception as exc:  # noqa: BLE001 - never turn a failing test into a crash
        lines = [f"the evidence could not be written: {type(exc).__name__}: {exc}"]
    if lines:
        report.sections.append(("earful", "\n".join(lines)))
