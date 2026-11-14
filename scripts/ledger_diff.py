"""Readable diff of two ledger files (use the .normalized.jsonl form)."""
import difflib
import json
import sys


def render(path: str) -> list[str]:
    out = []
    for line in open(path):
        e = json.loads(line)
        data = json.dumps(e["data"], sort_keys=True)
        out.append(f'{e["t_us"] / 1e6:>10.6f} {e["source"]:<9} {e["kind"]:<27} {data[:170]}')
    return out


a, b = sys.argv[1], sys.argv[2]
diff = list(difflib.unified_diff(render(a), render(b), a.split("/")[-1], b.split("/")[-1], n=0, lineterm=""))
print("\n".join(diff) if diff else "identical")
