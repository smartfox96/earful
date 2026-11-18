"""What a test calls: run a scenario, search it, keep a failure, replay one."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterable, Iterator
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any

from ..core import bundle, crosscheck, search
from ..core import case as cases
from ..core.predicate import Predicate
from ..core.shrink import nearest
from . import scenario
from .harness import Case, RunResult, run_case
from .scenario import Scenario, Source


class WorkersUnavailable(RuntimeError):
    """Worker processes could not be started, or one died before it ran a case."""


@contextlib.contextmanager
def _quiet_framework() -> Iterator[None]:
    # A search makes hundreds of runs, and the framework logs a traceback per tool error.
    logger = logging.getLogger("livekit")
    before = logger.propagate
    logger.propagate = False
    try:
        yield
    finally:
        logger.propagate = before


# At module level so that it can be sent to a worker process.
def _run_quietly(data: cases.Case) -> list[dict[str, Any]]:
    with _quiet_framework():
        return scenario.run(data)


def run(source: Source, *, case: cases.Case | None = None, seed: int = 0) -> RunResult:
    """Run a scenario once and return its ledger, with the verdicts on ``report``.
    ``case`` is case data (earful.case/1), such as one a search found. Raises ``InvalidRun``
    if the run is not valid evidence, ``ValueError`` if the scenario or case is refused."""
    found = Scenario.of(source)
    if case is None:
        return run_case(found.make(), seed=seed)
    return run_case(found.build(case), seed=case["seed"])


def explore(
    source: Source,
    *,
    case: cases.Case | None = None,
    seed: int = 0,
    workers: int = 0,
    variables: Iterable[str] = search.DEFAULT_VARIABLES,
    faults: Iterable[str] = cases.FAULT_KINDS,
    pairs: bool = False,
    profiles: int = 0,
    speeches: Iterable[int] | None = None,
    moments: Iterable[str] | None = None,
    words: bool = True,
) -> search.Exploration:
    """Search a scenario for the timings and faults at which it breaks a contract.
    ``workers`` above 0 runs cases in that many processes and needs an importable scenario.
    Raises ``ValueError`` for a bad argument, ``WorkersUnavailable`` if workers cannot run."""
    found = Scenario.of(source)
    if workers < 0:
        raise ValueError(f"workers is a number of processes, 0 for this one; got {workers}")
    if workers:
        scenario.importable(found.reference, "search in worker processes")
    start = found.default(seed) if case is None else case
    # Checked here too, before any worker process is started.
    variables, faults = search.check_arguments(start, variables, faults)
    options: dict[str, Any] = {
        "variables": variables,
        "faults": faults,
        "pairs": pairs,
        "profiles": profiles,
        "speeches": speeches,
        "moments": moments,
        "words": words,
    }
    if workers == 0:

        def quietly(data: cases.Case) -> list[dict[str, Any]]:
            with _quiet_framework():
                return found.run(data)

        return search.explore(start, quietly, **options)
    # A fresh pool per search, so workers run the scenario's code as it is now.
    try:
        pool = scenario.pool(workers)
    except (OSError, NotImplementedError, ImportError) as exc:
        # A sandbox or a locked-down container that may not start processes.
        raise WorkersUnavailable(
            f"worker processes cannot be started here ({type(exc).__name__}: {exc}). "
            "workers=0 runs the search in this process"
        ) from exc
    try:
        with pool:
            return search.explore(start, _run_quietly, each=pool.map, **options)
    except BrokenProcessPool as exc:
        raise WorkersUnavailable(
            "a worker process died before it ran a case. A worker is a fresh interpreter "
            "that imports this process's main module, so a script that searches with "
            'workers must do so under `if __name__ == "__main__":`, and cannot be run '
            "from standard input or an interactive session. workers=0 runs the search "
            "in this process"
        ) from exc


def cross_check(
    source: Source,
    *,
    case: cases.Case | None = None,
    seed: int = 0,
    runs: int = 3,
    tolerance: crosscheck.Tolerance | None = None,
) -> crosscheck.CrossCheck:
    """Compare one virtual-clock run of a scenario with ``runs`` real-clock runs.
    A real-clock run takes as long as the conversation it scripts. Raises ``InvalidRun``
    if the virtual run is not valid evidence, ``ValueError`` if the case is refused."""
    found = Scenario.of(source)
    if runs < 1:
        raise ValueError(f"a cross-check makes at least one real-clock run; got {runs}")

    def built() -> tuple[Case, int]:
        return (found.make(), seed) if case is None else (found.build(case), case["seed"])

    scripted, with_seed = built()
    virtual = run_case(scripted, seed=with_seed).events
    real = []
    for _ in range(runs):
        scripted, with_seed = built()
        result = run_case(scripted, seed=with_seed, virtual=False, require_valid=False)
        real.append(result.events)
    return crosscheck.cross_check(virtual, real, tolerance)


def keep(
    found: search.Exploration,
    rule: Predicate | str,
    directory: Path | str,
    *,
    repeats: int = 3,
) -> bundle.Bundle:
    """Reduce a case that breaks ``rule`` and write it to ``directory`` as a bundle.
    Raises ``ValueError`` if the scenario is not importable or no case breaks the rule."""
    predicate = Predicate.parse(rule) if isinstance(rule, str) else rule
    reference = scenario.importable(found.case["scenario"], "keep a failure as a bundle")
    breaking = [tried.candidate.case for tried in found.breaking(predicate)]
    if not breaking and predicate in found.original.failures:
        breaking = [found.case]  # it breaks as given, and nowhere the search went
    if not breaking:
        raise ValueError(f"the search found no case that breaks {predicate}")
    author = scenario.default(reference, found.case["seed"])
    directory = Path(directory)
    bundle.capture(
        directory,
        nearest(author, breaking),
        found.runner,
        predicate,
        author=author,
        repeats=repeats,
        environment=scenario.environment(),
        scenario_digest=scenario.fingerprint(reference),
    )
    return bundle.load(directory)


def replay(directory: Path | str, *, repeats: int = 3) -> bundle.Replay:
    """Run a bundle's reduced case again and say whether it still breaks its rule.
    This imports and runs the scenario the bundle names: replay only bundles you trust."""
    kept = bundle.load(Path(directory))
    return bundle.replay(kept, scenario.run, repeats=repeats, fingerprint=scenario.fingerprint)


__all__ = ["WorkersUnavailable", "cross_check", "explore", "keep", "replay", "run"]
