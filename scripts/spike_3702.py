"""Historical case livekit/agents#3702, judged by the general contract checker.

Runs the booking-then-interruption case and prints what contracts A2 (one logical effect)
and M2 (turn integrity) conclude. Nothing here is specific to the defect: the same checker
judges every release.

    python scripts/spike_3702.py [OUT_DIR]
"""

import json
import sys
from pathlib import Path

from earful.core.checker import check
from earful.core.ledger import write_jsonl
from earful.livekit.cases import CASES
from earful.livekit.harness import run_case

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
