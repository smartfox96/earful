"""Negative control: the same case on the real clock, nothing patched.

If the ledger hash were insensitive to timing, these runs would hash identically too and
the virtual-time result would prove nothing.
"""
import json
import sys

from earful.core.ledger import first_difference, normalize
from earful.livekit.cases import CASES
from earful.livekit.harness import run_case

runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
results = [run_case(CASES["barge_in_mid_word"](), seed=0, virtual=False) for _ in range(runs)]
lines = [normalize(r.ledger.events) for r in results]
diff = first_difference(lines[0], lines[1])
print(json.dumps({
    "runs": runs,
    "distinct_hashes": len({r.hash for r in results}),
    "events": [len(r.ledger.events) for r in results],
    "kinds_identical": len({tuple((e["source"], e["kind"]) for e in r.ledger.events) for r in results}) == 1,
    "first_difference_line": diff[0] if diff else None,
    "run_a": diff[1][:200] if diff else None,
    "run_b": diff[2][:200] if diff else None,
}, indent=2))
