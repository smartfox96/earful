"""Historical case livekit/agents#5509, judged by the general contract checker.

python scripts/spike_5509.py [OUT_DIR] [--no-pause]

Exits 1 if the verdict is not the one this release should give, or if 20 runs differ.
"""

import json
import sys
from pathlib import Path

from earful.core.checker import FAIL, PASS, check
from earful.core.ledger import write_jsonl
from earful.livekit.cases import CASES
from earful.livekit.harness import run_case

FIXED_IN = (1, 5, 7)  # the first release without the defect: docs/compatibility.md

can_pause = "--no-pause" not in sys.argv
out_dir = next((a for a in sys.argv[1:] if not a.startswith("--")), None)


def make():
    case = CASES["speak_over_new_turn"]()
    case.sink_can_pause = can_pause
    return case


runs = [run_case(make(), seed=0) for _ in range(20)]
result = runs[0]
report = check(result.ledger.events)
l2 = report.results["L2"]
summary = {
    "versions": result.versions,
    "sink_can_pause": can_pause,
    "outcomes": report.outcomes(),
    "verdict_interruption": l2.outcome,
    "interruption_failures": [
        {"speech": v.instance["speech"], "findings": [f"{f.rule}: {f.detail}" for f in v.findings]}
        for v in l2.failures
    ],
    "repeat_runs": len(runs),
    "distinct_hashes": len({r.hash for r in runs}),
    "framework_warnings": result.framework_warnings,
}
print(json.dumps(summary, indent=2))
if out_dir:
    release = result.versions["livekit-agents"]
    tag = f"lk-{release}.speak_over_new_turn{'' if can_pause else '.no_pause'}"
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{tag}.summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (out / f"{tag}.report.json").write_text(json.dumps(report.to_json(), indent=2) + "\n")
    write_jsonl(result.ledger.events, out / f"{tag}.ledger.normalized.jsonl", normalized=True)

installed = result.versions["livekit-agents"]
repaired = tuple(int(part) for part in installed.split(".")[:3]) >= FIXED_IN
# The repair pauses the output, so an output that cannot pause fails on every release.
expected = PASS if repaired and can_pause else FAIL
wrong = []
if l2.outcome != expected:
    wrong.append(f"L2 is {l2.outcome} on livekit-agents {installed}, where it should be {expected}")
if summary["distinct_hashes"] != 1:
    wrong.append(f"{len(runs)} runs gave {summary['distinct_hashes']} different ledgers")
if wrong:
    sys.exit("#5509: " + "; ".join(wrong))
