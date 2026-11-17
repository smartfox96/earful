"""Check the contracts against ledger files and print the verdicts.

    python scripts/check_ledger.py LEDGER.jsonl [...] [--json] [--measures]

Exit code 1 if any contract fails or any ledger is not a valid run.
"""

import json
import sys
from pathlib import Path

from earful.core.checker import check
from earful.core.ledger import read_jsonl

paths = [Path(a) for a in sys.argv[1:] if not a.startswith("--")]
as_json, measures = "--json" in sys.argv, "--measures" in sys.argv
failed = False
for path in paths:
    report = check(read_jsonl(path))
    failed = failed or not report.valid or bool(report.failures)
    if as_json:
        print(json.dumps({"ledger": path.name, **report.to_json()}))
        continue
    print(f"== {path.name}")
    for line in report.lines(measures=measures):
        print(f"   {line}")
sys.exit(1 if failed else 0)
