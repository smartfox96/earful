"""Does the boundary search find what stepping through time finds?

.venvs/lk-1.8.12/bin/python scripts/search_vs_sweep.py [family ...] [--step 0.05]
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import livekit.agents as lk
from contracts_sweep import FAMILIES

from earful.core import case as cases
from earful.core.checker import check
from earful.core.predicate import failures
from earful.core.search import explore
from earful.livekit import scenario
from earful.livekit.harness import InvalidRun, run_case

VARIABLES = ("speech.position", *cases.DELAY_KINDS)


def swept(job: tuple[str, float]) -> list[str] | None:
    """The rules one run of the sweep broke, or None for a run that is not valid evidence."""
    family, at = job
    try:
        result = run_case(FAMILIES[family][2](at))
    except InvalidRun:
        return None
    return sorted(str(rule) for rule in failures(check(result.ledger.events)))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("families", nargs="*", metavar="family", help="default: every shape")
    parser.add_argument("--step", type=float, default=0.05, help="the sweep's step, in seconds")
    args = parser.parse_args(argv)
    unknown = sorted(set(args.families) - set(FAMILIES))
    if unknown:
        parser.error(f"unknown families {unknown}; choose from {sorted(FAMILIES)}")

    shapes: dict[str, dict[str, Any]] = {}
    with scenario.pool() as pool:
        for family in args.families or list(FAMILIES):
            first, last, _build = FAMILIES[family]
            steps = round((last - first) / args.step) + 1
            jobs = [(family, round(first + k * args.step, 3)) for k in range(steps)]
            runs = list(pool.map(swept, jobs, chunksize=4))
            by_sweep = {rule for run in runs if run is not None for rule in run}

            row: dict[str, Any] = {
                "sweep_runs": len(runs),
                "sweep_invalid": sum(run is None for run in runs),
                "sweep_rules": len(by_sweep),
            }
            try:
                found = explore(
                    scenario.default(f"contracts_sweep:at_the_end.{family}"),
                    scenario.run,
                    variables=VARIABLES,
                    faults=[],
                    each=pool.map,
                )
            except ValueError as exc:
                row.update(search_candidates=0, missed=sorted(by_sweep), not_searched=str(exc))
            else:
                by_search = {str(rule) for rule in (*found.predicates(), *found.original.failures)}
                row.update(
                    search_candidates=len(found.tried),
                    search_not_judged=sum(not t.outcome.ran for t in found.tried),
                    missed=sorted(by_sweep - by_search),
                )
            shapes[family] = row

    missed = {family: row["missed"] for family, row in shapes.items() if row["missed"]}
    print(
        json.dumps(
            {
                "livekit-agents": lk.__version__,
                "step_s": args.step,
                "shapes": shapes,
                "sweep_runs": sum(row["sweep_runs"] for row in shapes.values()),
                "search_candidates": sum(row["search_candidates"] for row in shapes.values()),
                "shapes_where_the_sweep_found_a_rule": sum(
                    bool(row["sweep_rules"]) for row in shapes.values()
                ),
                "missed": missed,
            },
            indent=2,
        )
    )
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
