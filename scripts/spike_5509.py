"""Historical case livekit/agents#5509, judged by the general contract checker.

python scripts/spike_5509.py [OUT_DIR] [--no-pause]
"""

import json
import sys
from pathlib import Path

from earful.core.checker import check
from earful.core.ledger import write_jsonl
from earful.livekit.cases import CASES
from earful.livekit.harness import run_case

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
