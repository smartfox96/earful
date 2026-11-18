"""Historical case livekit/agents#3702, judged by the general contract checker.

python scripts/spike_3702.py [OUT_DIR]

Exits 1 if a verdict is not the one this release should give, or if 20 runs differ.
"""

import json
import sys
from pathlib import Path

from earful.core.checker import FAIL, PASS, check
from earful.core.ledger import write_jsonl
from earful.livekit.cases import CASES
from earful.livekit.harness import run_case

FIXED_IN = (1, 6, 6)  # the first release without the defect: docs/compatibility.md

runs = [run_case(CASES["book_then_interrupt"](), seed=0) for _ in range(20)]
result = runs[0]
report = check(result.ledger.events)
a2, m2 = report.results["A2"], report.results["M2"]
summary = {
    "versions": result.versions,
    "outcomes": report.outcomes(),
    "durable_reservations_for_one_intent": a2.verdicts[0].measures["committed"],
    "verdict_one_logical_effect": a2.outcome,
    "verdict_turn_integrity": m2.outcome,
    "turn_integrity_failures": [
        {
            "context": v.instance["context"],
            "findings": [f"{f.rule}: {f.detail}" for f in v.findings],
        }
        for v in m2.failures
    ],
    "repeat_runs": len(runs),
    "distinct_hashes": len({r.hash for r in runs}),
    "framework_warnings": result.framework_warnings,
}
print(json.dumps(summary, indent=2))

if len(sys.argv) > 1:
    out = Path(sys.argv[1])
    tag = f"lk-{result.versions['livekit-agents']}.book_then_interrupt"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{tag}.summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (out / f"{tag}.report.json").write_text(json.dumps(report.to_json(), indent=2) + "\n")
    write_jsonl(result.ledger.events, out / f"{tag}.ledger.normalized.jsonl", normalized=True)

installed = result.versions["livekit-agents"]
expected = PASS if tuple(int(part) for part in installed.split(".")[:3]) >= FIXED_IN else FAIL
wrong = [
    f"{name} is {got.outcome} on livekit-agents {installed}, where it should be {expected}"
    for name, got in (("A2", a2), ("M2", m2))
    if got.outcome != expected
]
if summary["distinct_hashes"] != 1:
    wrong.append(f"{len(runs)} runs gave {summary['distinct_hashes']} different ledgers")
if wrong:
    sys.exit("#3702: " + "; ".join(wrong))
