"""What a test calls: run a scenario, search it, keep a failure, replay one.

    from earful.livekit.api import explore, run

    def test_a_booking_is_made_once():
        run(case).assert_contracts()        # the scenario as written
        explore(case).assert_contracts()    # and at every timing and fault near it

A scenario is given as a ``Case``, as a function that returns one, or by the
``module:function`` name of such a function (``earful.livekit.scenario``). A ``Case`` or a
function defined inside a test runs in the process of the test. Keeping a failure as a
bundle, and searching in worker processes, need a scenario that can be imported: both
refuse one that cannot, before anything runs, and say what to change.
"""

from __future__ import annotations

import atexit
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any

from ..core import bundle, search
from ..core import case as cases
from ..core.predicate import Predicate
from ..core.shrink import nearest
from . import scenario
from .harness import RunResult, run_case
from .scenario import Scenario, Source

_pools: dict[int, ProcessPoolExecutor] = {}


def _pool(workers: int) -> ProcessPoolExecutor:
    """One pool of worker processes for each size asked for, kept for the life of the
    process: starting one means starting interpreters that import the framework."""
    if workers not in _pools:
        _pools[workers] = scenario.pool(workers)
    return _pools[workers]


@atexit.register
def _shutdown() -> None:
    while _pools:
        _pools.popitem()[1].shutdown(cancel_futures=True)


def run(source: Source, *, case: cases.Case | None = None, seed: int = 0) -> RunResult:
    """Run a scenario once and return its ledger, with the verdicts on ``report``.

    ``case`` is the data of a case of this scenario (earful.case/1), such as one a search
    found; without it the scenario runs as written. Raises ``InvalidRun`` if the run may
    not be used as evidence, and ``ValueError`` if the scenario or the case is refused.
    """
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

    The options are those of ``earful.core.search.explore``. ``case`` starts the search
    from the data of a case of this scenario, in place of the scenario as written.

    ``workers`` is how many worker processes run the cases, side by side. The default,
    0, runs them one after another in this process. Worker processes build the scenario
    from its name, so a scenario that is not importable raises ``ValueError`` before
    anything runs. Each worker is a fresh interpreter that imports the main module of
    this process: a script that searches with workers needs the usual
    ``if __name__ == "__main__":`` guard.
    """
    found = Scenario.of(source)
    if workers < 0:
        raise ValueError(f"workers is a number of processes, 0 for this one; got {workers}")
    if workers:
        scenario.importable(found.reference, "search in worker processes")
    start = found.default(seed) if case is None else case
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
        return search.explore(start, found.run, **options)
    try:
        return search.explore(start, scenario.run, each=_pool(workers).map, **options)
    except BrokenProcessPool as exc:
        _pools.pop(workers, None)
        raise RuntimeError(
            "a worker process died before it ran a case. A worker is a fresh interpreter "
            "that imports this process's main module, so a script that searches with "
            'workers must do so under `if __name__ == "__main__":`, and cannot be run '
            "from standard input or an interactive session. workers=0 runs the search "
            "in this process"
        ) from exc


def keep(
    found: search.Exploration,
    rule: Predicate | str,
    directory: Path | str,
    *,
    repeats: int = 3,
) -> bundle.Bundle:
    """Reduce a case that breaks ``rule`` to the smallest that still does, check that it
    repeats, and write it to ``directory`` as a bundle (earful.bundle/1).

    Raises ``ValueError``, before anything runs, if the scenario is not importable or the
    search found no case that breaks the rule.
    """
    predicate = Predicate.parse(rule) if isinstance(rule, str) else rule
    reference = scenario.importable(found.case["scenario"], "keep a failure as a bundle")
    breaking = [tried.candidate.case for tried in found.breaking(predicate)]
    if predicate in found.original.failures:
        breaking.append(found.case)
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
    """Run a bundle's reduced case again and say whether it still breaks its rule,
    whether the runs repeat, and whether the ledger is the one recorded. Replaying
    imports and runs the scenario the bundle names: replay only bundles you trust."""
    kept = bundle.load(Path(directory))
    return bundle.replay(kept, scenario.run, repeats=repeats, fingerprint=scenario.fingerprint)


__all__ = ["explore", "keep", "replay", "run"]
