"""Search a scenario for timing failures, and reduce what is found to a case worth keeping.

Moves each time the scenario controls (where a speech sits; how long a model call, a first
audio or a backend write takes; when the user hangs up) so that it falls just before, at
and just after every moment the system changes state. Checks every run and prints which
contract rules broke, how wide the window is in which each breaks (a rule that breaks only
at single instants turns on same-instant order and is marked `instants_only`), and which
boundaries were never reached. For each rule named with --keep, a failing case is reduced
to the smallest one that fails the same way, settled inside its window, run again to
confirm it repeats, and written as a bundle.

    python scripts/search.py SCENARIO [options]

    python scripts/search.py earful.livekit.cases:book_then_interrupt --keep A2/rule --out out

SCENARIO is module:function. Running it imports and calls that function.

    --case FILE          start from a saved case (earful.case/1) instead of the scenario as
                         written
    --variables A,B      kinds of time to move (default: speech.position, every provider
                         delay and user.hangup; speech.end and speech.stt_delay can be added)
    --faults A,B         faults to add to each backend write (default: backend.error,
                         backend.timeout, backend.drop; "none" for no faults)
    --pairs              also move every speech and the hangup with each fault in place, and
                         with each write and each generation stretched to 1.5 s, and add
                         faults to two writes at once
    --profiles N         also move every speech under N seeded provider-timing profiles
    --no-words           do not treat the end of every played word as a boundary
    --no-windows         do not measure the window of every rule found (those kept are still
                         measured)
    --keep CONTRACT/RULE reduce a case that breaks this rule and write a bundle (repeatable)
    --out DIR            where bundles go
"""

import json
import sys
from pathlib import Path

from earful.core import bundle, search
from earful.core import case as cases
from earful.core.predicate import Predicate
from earful.core.shrink import nearest
from earful.livekit import scenario


def main(argv: list[str]) -> int:
    args, keep, out, case_file, variables = [], [], None, None, None
    words, windows = True, True
    faults: tuple[str, ...] = cases.FAULT_KINDS
    pairs, profiles = False, 0
    it = iter(argv)
    for arg in it:
        if arg == "--keep":
            keep.append(Predicate.parse(next(it)))
        elif arg == "--out":
            out = Path(next(it))
        elif arg == "--case":
            case_file = Path(next(it))
        elif arg == "--variables":
            variables = tuple(next(it).split(","))
        elif arg == "--faults":
            named = next(it)
            faults = () if named == "none" else tuple(named.split(","))
        elif arg == "--pairs":
            pairs = True
        elif arg == "--profiles":
            profiles = int(next(it))
        elif arg == "--no-words":
            words = False
        elif arg == "--no-windows":
            windows = False
        else:
            args.append(arg)
    if len(args) != 1 or (keep and out is None):
        print(__doc__)
        return 2

    reference = args[0]
    author = scenario.default(reference)
    case = json.loads(case_file.read_text()) if case_file else author

    with scenario.pool() as pool:
        found = search.explore(
            case,
            scenario.run,
            variables=variables or search.DEFAULT_VARIABLES,
            faults=faults,
            pairs=pairs,
            profiles=profiles,
            words=words,
            each=pool.map,
        )
    summary = {
        "scenario": reference,
        **scenario.environment(),
        **found.summary(),
        "coverage": found.coverage(),
        "bundles": {},
    }
    for predicate in found.predicates():
        if windows or predicate in keep:
            measured = found.windows(predicate)
            entry = summary["found"][str(predicate)]
            entry["windows"] = [w.to_json() for w in measured]
            # Breaks at single instants only: a matter of same-instant order, not a finding.
            # A break that needed no time moved has no window and is not one of these.
            unmoved = any(t.candidate.variable is None for t in found.breaking(predicate))
            entry["instants_only"] = not unmoved and all(w.instant for w in measured)

    for predicate in keep:
        breaking = found.breaking(predicate)
        if not breaking:
            continue
        rule = f"{predicate.contract}-{predicate.rule}".replace(" ", "-")
        assert out is not None
        directory = out / f"lk-{summary['livekit-agents']}.{reference.rpartition(':')[2]}.{rule}"
        manifest = bundle.capture(
            directory,
            nearest(author, [tried.candidate.case for tried in breaking]),
            found.runner,
            predicate,
            author=author,
            environment=scenario.environment(),
            scenario_digest=scenario.fingerprint(reference),
        )
        summary["bundles"][str(predicate)] = {
            "directory": str(directory),
            "speeches": [manifest["original"]["speeches"], manifest["minimal"]["speeches"]],
            "delays": [manifest["original"]["delays"], manifest["minimal"]["delays"]],
            "faults": [manifest["original"]["faults"], manifest["minimal"]["faults"]],
            "reduction": manifest["reduction"],
            "windows": manifest["windows"],
            "replay": manifest["replay"],
        }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
