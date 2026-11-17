"""Measure the figures that docs/gate1-core.md states, on the release this runs with.

    .venvs/lk-1.8.12/bin/python scripts/gate1_figures.py

Prints one JSON document. Each key is a section of that page:

    deliberate_break   "A deliberate break, found from a scenario that passes as written"
    default_search     the size of a search with every default variable
    known_defects      "The two known defects are found on the releases that have them"
    faults             "Faults"
    hangup             "Hangup"

The scenarios are those of tests/breaks.py and earful.livekit.cases. Times are in
microseconds. Run it on each release to fill a table that has a row per release. Each
section reports the rules its table on the page names, and no other.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import livekit.agents as lk

from earful.core import case as cases
from earful.core.case import Variable
from earful.core.predicate import Predicate
from earful.core.search import Exploration, explore
from earful.core.shrink import nearest, shrink
from earful.livekit import scenario

SLOW_TO_STOP = Predicate("L2", "rule a")
BOOKED_TWICE = Predicate("A2", "rule")
CONFIRMED_BUT_NOT_DONE = Predicate("A3", "rule")
LOST_RESULT = Predicate("M2", "rule d")
OVER_A_NEW_TURN = Predicate("L2", "rule b")
STOP = Variable("speech.position", 1)


def windows(found: Exploration, predicate: Predicate) -> list[dict[str, Any]]:
    return [
        {
            "variable": str(w.variable),
            "from_us": w.lo_us,
            "to_us": w.hi_us,
            "width_us": w.width_us,
            "ends": [w.lo_end, w.hi_end],
            "instant": w.instant,
        }
        for w in found.windows(predicate)
    ]


def broken(found: Exploration, *rules: Predicate) -> dict[str, Any]:
    """For each of ``rules``, the rules the page's table names: how many candidates broke
    it, under which fault, and how wide the failure is."""
    return {
        str(p): {
            "candidates": len(found.breaking(p)),
            "under": sorted({t.candidate.base for t in found.breaking(p)}),
            "windows": windows(found, p) if found.breaking(p) else [],
        }
        for p in rules
    }


def size(found: Exploration) -> dict[str, Any]:
    summary = found.summary()
    return {
        key: summary[key]
        for key in ("candidates", "ran", "refused", "error", "invalid", "impossible_placements")
    }


def main() -> int:
    out: dict[str, Any] = {"livekit-agents": lk.__version__}
    with scenario.pool() as pool:

        def search(reference: str | cases.Case, **options: Any) -> Exploration:
            author = scenario.default(reference) if isinstance(reference, str) else reference
            return explore(author, scenario.run, each=pool.map, **options)

        # "Stop." moved so that its interruption point falls on each boundary of the reply.
        stop = {
            "variables": ["speech.position"],
            "speeches": [1],
            "moments": ["interrupt"],
            "faults": [],
        }
        out["deliberate_break"] = {
            name: {
                "candidates": len(found.tried),
                "breaking": len(found.breaking(SLOW_TO_STOP)),
                "windows": windows(found, SLOW_TO_STOP),
            }
            for name in (
                "slow_to_stop",
                "slow_to_stop_for_30ms",
                "slow_to_stop_on_an_instant",
                "host",  # the control
            )
            for found in [search(f"breaks:{name}", **stop)]
        }

        started = time.perf_counter()
        default = search("earful.livekit.cases:book_then_interrupt", faults=[])
        out["default_search"] = {
            **size(default),
            "seconds": round(time.perf_counter() - started, 1),
        }

        # Each reference case from a start that passes everywhere: its second speech moved
        # to long after everything, then moved back to every boundary.
        out["known_defects"] = {}
        for name, rules in (
            ("book_then_interrupt", (BOOKED_TWICE, LOST_RESULT)),
            ("speak_over_new_turn", (OVER_A_NEW_TURN,)),
        ):
            late = cases.with_value(
                scenario.default(f"earful.livekit.cases:{name}"), STOP, 30_000_000
            )
            assert late is not None
            found = search(late, variables=["speech.position"], speeches=[1], faults=[])
            out["known_defects"][name] = {
                "candidates": len(found.tried),
                "found": broken(found, *rules),
            }

        out["faults"] = {}
        for name, rule in (
            ("confirms_a_failed_booking", CONFIRMED_BUT_NOT_DONE),
            ("reads_the_error", CONFIRMED_BUT_NOT_DONE),
            ("retries_without_a_key", BOOKED_TWICE),
            ("retries_with_a_key", BOOKED_TWICE),
        ):
            found = search(f"breaks:{name}", variables=())
            out["faults"][name] = {
                "candidates": len(found.tried),
                "judged": sum(t.outcome.ran for t in found.tried),
                "found": broken(found, rule),
            }

        out["hangup"] = {}
        for name in ("books_again_if_the_user_left", "books_again_with_a_key"):
            author = scenario.default(f"breaks:{name}")
            found = search(author, variables=["user.hangup"], faults=[])
            row: dict[str, Any] = {
                "candidates": len(found.tried),
                "judged": sum(t.outcome.ran for t in found.tried),
                "booked_twice": len(found.breaking(BOOKED_TWICE)),
                "windows": windows(found, BOOKED_TWICE),
            }
            if found.breaking(BOOKED_TWICE):
                start = nearest(author, [t.candidate.case for t in found.breaking(BOOKED_TWICE)])
                reduced = shrink(start, found.runner, BOOKED_TWICE, author=author)
                row["reduced"] = {
                    "runs": reduced.runs,
                    "hangup_us": reduced.case["hangup_us"],
                    "differences": reduced.differences,
                }
            out["hangup"][name] = row

    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
