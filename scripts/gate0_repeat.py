"""Gate 0 repeatability check.

Runs one case many times and compares normalized ledger hashes:

  * N runs in this process, each on a fresh virtual-time loop;
  * M runs in fresh subprocesses, each with a different PYTHONHASHSEED, so set and dict
    iteration order, object addresses and import order all vary between them.

Exit code 0 only if every hash is identical.

    python scripts/gate0_repeat.py [case] [--in-process N] [--subprocess M] [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from earful.core.ledger import first_difference, normalize, write_jsonl
from earful.livekit.cases import CASES
from earful.livekit.harness import run_case, versions


def _one(case_name: str) -> dict:
    result = run_case(CASES[case_name](), seed=0)
    return {
        "hash": result.hash,
        "events": len(result.ledger.events),
        "stamp_errors": result.stamp_errors,
        "executor_calls": result.executor_calls,
        "warnings": result.framework_warnings,
        "normalized": normalize(result.ledger.events),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("case", nargs="?", default="barge_in_mid_word")
    parser.add_argument("--in-process", type=int, default=100)
    parser.add_argument("--subprocess", type=int, default=20)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--one", action="store_true", help="internal: one run, JSON on stdout")
    args = parser.parse_args()

    if args.one:
        print(json.dumps(_one(args.case)))
        return 0

    started = time.perf_counter()
    reference = run_case(CASES[args.case](), seed=0)
    ref_lines = normalize(reference.ledger.events)
    hashes: Counter[str] = Counter()
    problems: list[str] = []

    def record(label: str, run: dict) -> None:
        hashes[run["hash"]] += 1
        if run["hash"] != reference.hash:
            diff = first_difference(ref_lines, run["normalized"])
            problems.append(f"{label}: hash differs; first difference {diff}")
        if run["stamp_errors"]:
            problems.append(f"{label}: {run['stamp_errors']} audio stamp errors")
        if run["executor_calls"]:
            problems.append(f"{label}: {run['executor_calls']} executor calls left the virtual clock")

    for i in range(args.in_process):
        record(f"in-process #{i}", _one(args.case))
    in_process_seconds = time.perf_counter() - started

    for i in range(args.subprocess):
        env = dict(os.environ, PYTHONHASHSEED=str(1000 + i * 7919))
        proc = subprocess.run(
            [sys.executable, __file__, args.case, "--one"],
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
        )
        if proc.returncode != 0:
            problems.append(f"subprocess #{i}: exit {proc.returncode}: {proc.stderr[-400:]}")
            continue
        record(f"subprocess #{i}", json.loads(proc.stdout.strip().splitlines()[-1]))

    summary = {
        "case": args.case,
        "versions": versions(),
        "reference_hash": reference.hash,
        "events_per_run": len(reference.ledger.events),
        "in_process_runs": args.in_process,
        "subprocess_runs": args.subprocess,
        "distinct_hashes": len(hashes),
        "hash_counts": dict(hashes),
        "identical": len(hashes) == 1 and not problems,
        "problems": problems[:20],
        "framework_warnings": reference.framework_warnings,
        "clock_model_fields_patched": len(reference.clock.model_fields),
        "clock_modules_skipped": reference.clock.skipped_modules,
        "in_process_wall_seconds": round(in_process_seconds, 2),
        "virtual_seconds_per_run": reference.ledger.events[-1]["t_us"] / 1e6,
    }
    print(json.dumps(summary, indent=2))

    if args.out:
        tag = f"lk-{summary['versions']['livekit-agents']}"
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / f"{tag}.{args.case}.summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        write_jsonl(
            reference.ledger.events,
            args.out / f"{tag}.{args.case}.ledger.normalized.jsonl",
            normalized=True,
        )
    return 0 if summary["identical"] else 1


if __name__ == "__main__":
    sys.exit(main())
