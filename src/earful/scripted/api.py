"""What a test calls: run a scenario, search it, keep a failure, replay one."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterable
from concurrent.futures.process import BrokenProcessPool
from contextlib import AbstractContextManager
from pathlib import Path
from types import ModuleType
from typing import Any, Generic, Protocol, TypeVar

from ..core import bundle, crosscheck, search
from ..core import case as cases
from ..core.predicate import Predicate
from ..core.shrink import nearest

CaseT = TypeVar("CaseT")
ResultT = TypeVar("ResultT", bound="Result")
CaseT_contra = TypeVar("CaseT_contra", contravariant=True)
ResultT_co = TypeVar("ResultT_co", covariant=True)


class Result(Protocol):
    @property
    def events(self) -> list[dict[str, Any]]: ...


class RunCase(Protocol[CaseT_contra, ResultT_co]):
    def __call__(
        self, case: CaseT_contra, seed: int = 0, *, virtual: bool = True, require_valid: bool = True
    ) -> ResultT_co: ...


class WorkersUnavailable(RuntimeError):
    """Worker processes could not be started, or one died before it ran a case."""


class AdapterAPI(Generic[CaseT, ResultT]):
    """Run, search and replay with one adapter's case builder and runner."""

    def __init__(
        self,
        scenario: ModuleType,
        run_case: RunCase[CaseT, ResultT],
        run_in_worker: Callable[[cases.Case], list[dict[str, Any]]],
        quiet: Callable[[], AbstractContextManager[Any]] = contextlib.nullcontext,
    ) -> None:
        self.scenario = scenario
        self.run_case = run_case
        self.run_in_worker = run_in_worker
        self.quiet = quiet

    def run(
        self,
        source: str | CaseT | Callable[[], CaseT],
        *,
        case: cases.Case | None = None,
        seed: int = 0,
    ) -> ResultT:
        """Run a scenario once and return its ledger, with the verdicts on ``report``.
        ``case`` is case data (earful.case/1), such as one a search found. Raises ``InvalidRun``
        if the run is not valid evidence, ``ValueError`` if the scenario or case is refused."""
        found = self.scenario.Scenario.of(source)
        if case is None:
            return self.run_case(found.make(), seed=seed)
        return self.run_case(found.build(case), seed=case["seed"])

    def explore(
        self,
        source: str | CaseT | Callable[[], CaseT],
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
        found = self.scenario.Scenario.of(source)
        if workers < 0:
            raise ValueError(f"workers is a number of processes, 0 for this one; got {workers}")
        if workers:
            self.scenario.importable(found.reference, "search in worker processes")
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
        try:
            with pool:
                return search.explore(start, self.run_in_worker, each=pool.map, **options)
        except BrokenProcessPool as exc:
            raise WorkersUnavailable(
                "a worker process died before it ran a case. A worker is a fresh interpreter "
                "that imports this process's main module, so a script that searches with "
                'workers must do so under `if __name__ == "__main__":`, and cannot be run '
                "from standard input or an interactive session. workers=0 runs the search "
                "in this process"
            ) from exc

    def cross_check(
        self,
        source: str | CaseT | Callable[[], CaseT],
        *,
        case: cases.Case | None = None,
        seed: int = 0,
        runs: int = 3,
        tolerance: crosscheck.Tolerance | None = None,
    ) -> crosscheck.CrossCheck:
        """Compare one virtual-clock run of a scenario with ``runs`` real-clock runs.
        A real-clock run takes as long as the conversation it scripts. Raises ``InvalidRun``
        if the virtual run is not valid evidence, ``ValueError`` if the case is refused."""
        found = self.scenario.Scenario.of(source)
        if runs < 1:
            raise ValueError(f"a cross-check makes at least one real-clock run; got {runs}")

        def built() -> tuple[CaseT, int]:
            return (found.make(), seed) if case is None else (found.build(case), case["seed"])

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
        """Reduce a case that breaks ``rule`` and write it to ``directory`` as a bundle.
        Raises ``ValueError`` if the scenario is not importable or no case breaks the rule."""
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
        """Run a bundle's reduced case again and say whether it still breaks its rule.
        This imports and runs the scenario the bundle names: replay only bundles you trust."""
        kept = directory if isinstance(directory, bundle.Bundle) else bundle.load(Path(directory))
        return bundle.replay(
            kept, self.scenario.run, repeats=repeats, fingerprint=self.scenario.fingerprint
        )
