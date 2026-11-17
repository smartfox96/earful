"""Run one case once and print its ledger in readable form."""

import json
import sys

from earful.livekit.cases import CASES
from earful.livekit.harness import run_case

name = sys.argv[1] if len(sys.argv) > 1 else "barge_in_mid_word"
result = run_case(CASES[name](), seed=0)
for e in result.ledger.events:
    data = json.dumps(e["data"], sort_keys=True)
    if len(data) > 230:
        data = data[:230] + " ..."
    print(f"{e['seq']:>3} {e['t_us'] / 1e6:>10.6f} {e['source']:<9} {e['kind']:<28} {data}")
print()
print("hash           ", result.hash)
print("versions       ", result.versions)
print("stamp errors   ", result.stamp_errors)
print("executor calls ", result.executor_calls)
print("fw warnings    ", result.framework_warnings)
print(
    "clock patched  ",
    len(result.clock.model_fields),
    "model fields; not patched",
    result.clock.unpatched,
)
