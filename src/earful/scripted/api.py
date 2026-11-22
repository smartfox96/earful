"""What a test calls: run a scenario, search it, keep a failure, replay one."""

from __future__ import annotations

import contextlib
import functools
import warnings
from collections.abc import Callable, Iterable
from concurrent.futures.process import BrokenProcessPool
from contextlib import AbstractContextManager
from pathlib import Path
from types import ModuleType
from typing import Any, Generic, ParamSpec, Protocol, TypeVar

from ..core import bundle, crosscheck, search
from ..core import case as cases
from ..core.predicate import Predicate
from ..core.shrink import nearest
from .scenario import INLINE

# Named as an adapter names its own classes: help() prints these names.
Case = TypeVar("Case")
RunResult = TypeVar("RunResult", bound="Result")
CaseT_contra = TypeVar("CaseT_contra", contravariant=True)
ResultT_co = TypeVar("ResultT_co", covariant=True)
P = ParamSpec("P")
R = TypeVar("R")


class Result(Protocol):
    @property
    def events(self) -> list[dict[str, Any]]: ...


class RunCase(Protocol[CaseT_contra, ResultT_co]):
    def __call__(
        self, case: CaseT_contra, seed: int = 0, *, virtual: bool = True, require_valid: bool = True
    ) -> ResultT_co: ...


class WorkersUnavailable(RuntimeError):
    """Worker processes could not be started, or one died before it ran a case."""


def seeded(data: dict[str, Any] | None, seed: int | None) -> int:
    """The seed of a run: ``seed``, or the case's own when a case is given."""
    if data is None:
        return 0 if seed is None else seed
    if seed is not None and seed != data["seed"]:
        raise ValueError(
            f"the case holds seed {data['seed']} and seed={seed} was asked for; leave the seed "
            "out to run the case as it was found, or change the case"
        )
    return int(data["seed"])


def same_scenario(reference: str | None, data: dict[str, Any] | None) -> None:
    """Raise ``ValueError`` if ``data`` is a case made for another scenario than ``reference``."""
    if data is None or reference is None or data.get("scenario") in (None, INLINE, reference):
        return
    raise ValueError(
        f"the case was made for the scenario {data['scenario']!r}, and {reference!r} was given; "
        "run it with the scenario it names"
    )


def function(method: Callable[P, R], module: str) -> Callable[P, R]:
    """A bound method as a function of ``module``, which is how ``help()`` then shows it."""

    @functools.wraps(method)
    def call(*args: P.args, **kwargs: P.kwargs) -> R:
        return method(*args, **kwargs)

    call.__module__ = module
    call.__qualname__ = call.__name__
    return call


class AdapterAPI(Generic[Case, RunResult]):
    """Run, search and replay with one adapter's case builder and runner."""

    def __init__(
        self,
        scenario: ModuleType,
        run_case: RunCase[Case, RunResult],
        run_in_worker: Callable[[dict[str, Any]], list[dict[str, Any]]],
        quiet: Callable[[], AbstractContextManager[Any]] = contextlib.nullcontext,
    ) -> None:
        self.scenario = scenario
        self.run_case = run_case
        self.run_in_worker = run_in_worker
        self.quiet = quiet

    def run(
        self,
        source: str | Case | Callable[[], Case],
        *,
        data: dict[str, Any] | None = None,
        seed: int | None = None,
    ) -> RunResult:
        """Run a scenario once and return its ledger, with the verdicts on ``report``.
        ``data`` is a case as data (earful.case/1), such as one a search found, and holds its
        own seed. Warns of a declared delay or fault the run never reached. Raises
        ``InvalidRun`` if the run is not valid evidence, ``ValueError`` if either is refused."""
        found = self.scenario.Scenario.of(source)
        same_scenario(found.reference, data)
        if data is None:
            result = self.run_case(found.make(), seed=seeded(None, seed))
        else:
            result = self.run_case(found.build(data), seed=seeded(data, seed))
        unused = cases.unapplied(result.events)
        if unused:
            warnings.warn(f"declared but never applied: {'; '.join(unused)}", stacklevel=2)
        return result

    def explore(
        self,
        source: str | Case | Callable[[], Case],
        *,
        data: dict[str, Any] | None = None,
        seed: int | None = None,
        workers: int | None = None,
        variables: Iterable[str] = search.DEFAULT_VARIABLES,
        faults: Iterable[str] = cases.FAULT_KINDS,
        pairs: bool = False,
        profiles: int = 0,
        speeches: Iterable[int] | None = None,
        moments: Iterable[str] | None = None,
        words: bool = True,
        max_runs: int | None = None,
        planned: Callable[[search.Plan], None] | None = None,
        delay_limit_us: int = search.DELAY_LIMIT_US,
        wait_us: int = search.WAIT_US,
        stretch_us: int = search.STRETCH_US,
    ) -> search.Exploration:
        """Search a scenario for the timings and faults at which it breaks a contract.
        ``workers`` above 0 runs cases in that many processes and needs an importable scenario;
        0 runs them in this process. Left out, an importable scenario gets one per processor,
        or this process where workers cannot start.
        ``max_runs`` stops the search at that many runs, with a result that is not ``complete``.
        ``planned`` is told how many cases are about to run.
        Raises ``ValueError`` for a bad argument, ``WorkersUnavailable`` if workers cannot run."""
        found = self.scenario.Scenario.of(source)
        if workers is None:
            passed: dict[str, Any] = {
                "data": data,
                "seed": seed,
                "variables": variables,
                "faults": faults,
                "pairs": pairs,
                "profiles": profiles,
                "speeches": speeches,
                "moments": moments,
                "words": words,
                "max_runs": max_runs,
                "planned": planned,
                "delay_limit_us": delay_limit_us,
                "wait_us": wait_us,
                "stretch_us": stretch_us,
            }
            auto = self._suited(found.reference)
            if auto:
                with contextlib.suppress(WorkersUnavailable):
                    return self.explore(source, workers=auto, **passed)
            return self.explore(source, workers=0, **passed)
        if workers < 0:
            raise ValueError(f"workers is a number of processes, 0 for this one; got {workers}")
        if workers:
            self.scenario.importable(found.reference, "search in worker processes")
        start = found.default(seeded(None, seed)) if data is None else data
        # Checked here too, before any worker process is started.
        variables, faults = search.check_arguments(start, variables, faults)
        seeded(data, seed)
        same_scenario(found.reference, data)
        options: dict[str, Any] = {
            "variables": variables,
            "faults": faults,
            "pairs": pairs,
            "profiles": profiles,
            "speeches": speeches,
            "moments": moments,
            "words": words,
            "max_runs": max_runs,
            "planned": planned,
            "delay_limit_us": delay_limit_us,
            "wait_us": wait_us,
            "stretch_us": stretch_us,
        }
        if workers == 0:

            def quietly(data: dict[str, Any]) -> list[dict[str, Any]]:
                with self.quiet():
                    return found.run(data)

            return search.explore(start, quietly, **options)
        # A fresh pool per search, so workers run the scenario's code as it is now.
        try:
            pool = self.scenario.pool(workers)
        except (OSError, NotImplementedError, ImportError) as exc:
            # A sandbox or a locked-down container that may not start processes.
            raise WorkersUnavailable(
                f"worker processes cannot be started here ({type(exc).__name__}: {exc}). "
                "workers=0 runs the search in this process"
            ) from exc
        ran = 0  # cases a worker has run

        def counted(run: Callable[[Any], Any], wanted: list[Any]) -> Iterable[Any]:
            nonlocal ran
            for result in pool.map(run, wanted):
                ran += 1
                yield result

        try:
            with pool:
                return search.explore(start, self.run_in_worker, each=counted, **options)
        except BrokenProcessPool as exc:
            if ran:
                raise WorkersUnavailable(
                    f"a worker process died after the workers had run {ran} cases. Something "
                    "in the case it was running ended the process: memory ran out, or native "
                    "code crashed. workers=0 runs the search in this process, where that case "
                    "can be seen"
                ) from exc
            raise WorkersUnavailable(
                "a worker process died before any had run a case. A worker is a fresh "
                "interpreter that imports this process's main module, so a script that searches "
                'with workers must do so under `if __name__ == "__main__":`, and cannot be run '
                "from standard input or an interactive session. workers=0 runs the search "
                "in this process"
            ) from exc

    def _suited(self, reference: str | None) -> int:
        """Workers for a search that names no number: one per processor for a scenario that
        can be imported, and none where one worker would be no faster than this process."""
        try:
            self.scenario.importable(reference, "search in worker processes")
        except ValueError:
            return 0
        workers = search.default_workers()
        return workers if workers > 1 else 0

    def cross_check(
        self,
        source: str | Case | Callable[[], Case],
        *,
        data: dict[str, Any] | None = None,
        seed: int | None = None,
        runs: int = 3,
        tolerance: crosscheck.Tolerance | None = None,
    ) -> crosscheck.CrossCheck:
        """Compare one virtual-clock run of a scenario with ``runs`` real-clock runs.
        A real-clock run takes as long as the conversation it scripts. Raises ``InvalidRun``
        if the virtual run is not valid evidence, ``ValueError`` if the case is refused."""
        found = self.scenario.Scenario.of(source)
        if runs < 1:
            raise ValueError(f"a cross-check makes at least one real-clock run; got {runs}")
        same_scenario(found.reference, data)

        def built() -> tuple[Case, int]:
            made = found.make() if data is None else found.build(data)
            return made, seeded(data, seed)

        scripted, with_seed = built()
        virtual = self.run_case(scripted, seed=with_seed).events
        real = []
        for _ in range(runs):
            scripted, with_seed = built()
            result = self.run_case(scripted, seed=with_seed, virtual=False, require_valid=False)
            real.append(result.events)
        return crosscheck.cross_check(virtual, real, tolerance)

    def keep(
        self,
        found: search.Exploration,
        rule: Predicate | str,
        directory: Path | str,
        *,
        repeats: int = 3,
    ) -> bundle.Bundle:
        """Reduce a case that breaks ``rule`` and write it to ``directory`` as a bundle. Raises
        ``ValueError`` if the scenario is not importable or no case breaks the rule every time."""
        predicate = Predicate.parse(rule) if isinstance(rule, str) else rule
        reference = self.scenario.importable(found.case["scenario"], "keep a failure as a bundle")
        breaking = [tried.candidate.case for tried in found.breaking(predicate)]
        if not breaking and predicate in found.original.failures:
            breaking = [found.case]  # it breaks as given, and nowhere the search went
        if not breaking:
            raise ValueError(f"the search found no case that breaks {predicate}")
        author = self.scenario.default(reference, found.case["seed"])
        directory = Path(directory)
        bundle.capture(
            directory,
            nearest(author, breaking),
            found.runner,
            predicate,
            author=author,
            repeats=repeats,
            environment=self.scenario.environment(),
            scenario_digest=self.scenario.fingerprint(reference),
        )
        return bundle.load(directory)

    def replay(self, directory: Path | str | bundle.Bundle, *, repeats: int = 3) -> bundle.Replay:
        """Run a bundle's reduced case again: ``reproduced`` if every run breaks its rule, ``clean``
        if its contract passes in every run. It imports and runs the bundle's scenario: replay
        only bundles you trust."""
        kept = directory if isinstance(directory, bundle.Bundle) else bundle.load(Path(directory))
        return bundle.replay(
            kept, self.scenario.run, repeats=repeats, fingerprint=self.scenario.fingerprint
        )
