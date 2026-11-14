"""One-day spike: does scripted timing alone expose livekit/agents#3702?

Prints, per model call, whether the context shown to the model contained the tool result,
and how many durable reservations the backend holds for the one booking intent.
"""
import json
import sys
from pathlib import Path

from earful.core.ledger import write_jsonl
from earful.livekit.cases import BOOKING_INTENT, CASES
from earful.livekit.harness import run_case

runs = [run_case(CASES["book_then_interrupt"](), seed=0) for _ in range(20)]
result = runs[0]
ledger = result.ledger

commits = [e for e in ledger.of("backend", "effect.committed") if e["data"]["intent"] == BOOKING_INTENT]
calls = []
for req in ledger.of("llm", "llm.request"):
    resp = next(e for e in ledger.of("llm", "llm.response") if e["data"]["call"] == req["data"]["call"])
    commits_before = sum(1 for c in commits if c["seq"] < req["seq"])
    calls.append({
        "call": req["data"]["call"],
        "t": req["t_us"] / 1e6,
        "commits_before_this_call": commits_before,
        "context_has_tool_result": any(i["type"] == "function_call_output" for i in req["data"]["items"]),
        "model_called_tool": bool(resp["data"]["tool_calls"]),
    })

memory_gap = [c for c in calls if c["commits_before_this_call"] > 0 and not c["context_has_tool_result"]]
summary = {
    "versions": result.versions,
    "durable_reservations_for_one_intent": len(commits),
    "commit_times": [c["t_us"] / 1e6 for c in commits],
    "model_calls": calls,
    "memory_gap_calls": [c["call"] for c in memory_gap],
    "verdict_one_logical_effect": "FAIL" if len(commits) > 1 else "pass",
    "verdict_effect_visible_to_model": "FAIL" if memory_gap else "pass",
    "repeat_runs": len(runs),
    "distinct_hashes": len({r.hash for r in runs}),
    "framework_warnings": result.framework_warnings,
    "stamp_errors": result.stamp_errors,
}
print(json.dumps(summary, indent=2))

if len(sys.argv) > 1:
    out = Path(sys.argv[1])
    tag = f"lk-{result.versions['livekit-agents']}.book_then_interrupt"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{tag}.summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    write_jsonl(ledger.events, out / f"{tag}.ledger.normalized.jsonl", normalized=True)
