"""Print a ledger file in readable form. Optional second argument: start time in seconds."""
import json
import sys

start = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
for line in open(sys.argv[1]):
    e = json.loads(line)
    if e["t_us"] / 1e6 < start:
        continue
    data = json.dumps(e["data"], sort_keys=True)
    print(f'{e["seq"]:>3} {e["t_us"] / 1e6:>10.6f} {e["source"]:<9} {e["kind"]:<27} {data[:175]}')
