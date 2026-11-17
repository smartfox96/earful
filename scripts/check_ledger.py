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
    if as_json:
        print(json.dumps({"ledger": path.name, **report.to_json()}))
        failed = failed or not report.valid or bool(report.failures)
        continue
    print(f"== {path.name}")
    if not report.valid:
        failed = True
        for reason in report.invalid:
            print(f"   invalid: {reason}")
        continue
    for name, result in report.results.items():
        print(f"   {name}  {result.outcome}")
        for v in result.verdicts:
            if v.outcome != "pass" or measures:
                print(f"       {v.outcome}: {json.dumps(v.instance)} {v.summary}")
            for f in v.findings:
                print(f"           {f.rule}: {f.detail}")
            if measures and v.measures:
                print(f"           {json.dumps(v.measures)}")
            if v.flags:
                print(f"           flags: {', '.join(v.flags)}")
        for note in result.notes:
            print(f"       note: {note}")
    failed = failed or bool(report.failures)
sys.exit(1 if failed else 0)
