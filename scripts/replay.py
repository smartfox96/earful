"""Run a failure bundle's reduced case again.

    python scripts/replay.py BUNDLE_DIR [--repeats N] [--expect reproduced|clean]

Prints whether the reduced case still breaks the rule the bundle froze, whether the runs
repeat, whether the ledger is the very one recorded, and whether the scenario's code has
changed since the bundle was made. With --expect the exit code says whether that is what
happened: `reproduced` on the release the bundle was made on, `clean` on a release that
repaired the defect.

Replaying imports and runs the scenario the bundle names. Replay only bundles you trust.
"""
import json
import sys
from pathlib import Path

from earful.core import bundle
from earful.livekit import scenario


def main(argv: list[str]) -> int:
    args, repeats, expect = [], 3, None
    it = iter(argv)
    for arg in it:
        if arg == "--repeats":
            repeats = int(next(it))
        elif arg == "--expect":
            expect = next(it)
        else:
            args.append(arg)
    if len(args) != 1 or expect not in (None, "reproduced", "clean"):
        print(__doc__)
        return 2

    kept = bundle.load(Path(args[0]))
    result = bundle.replay(kept, scenario.run, repeats=repeats, fingerprint=scenario.fingerprint)
    print(json.dumps({
        "predicate": str(kept.predicate),
        "made_on": kept.manifest["framework"],
        "replayed_on": result.framework,
        "reproduced": result.reproduced,
        "repeatable": result.repeatable,
        "identical_to_recorded": result.identical,
        "scenario_code_changed": result.scenario_changed,
        "invalid": list(result.invalid),
    }, indent=2))
    if result.invalid or not result.repeatable:
        return 1
    if expect is None:
        return 0
    return 0 if result.reproduced == (expect == "reproduced") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
