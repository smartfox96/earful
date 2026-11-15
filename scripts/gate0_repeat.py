"""Gate 0 repeatability check.

Runs each case many times and compares normalized ledger hashes:

  * N runs in this process, each on a fresh virtual-time loop;
  * M runs in fresh subprocesses, each with a different PYTHONHASHSEED, so set and dict
    iteration order, object addresses and import order all vary between them.

With --check DIR, the hash must also equal the SHA-256 of the ledger committed in DIR for
the same release and case. That is the cross-platform and cross-Python assertion: the
committed ledgers were recorded on one machine and every other machine must reproduce them.

Exit code 0 only if every hash is identical, for every case.

    python scripts/gate0_repeat.py [case ...] [--in-process N] [--subprocess M]
                                   [--out DIR] [--check DIR]

With no case named, all cases run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
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
    parser.add_argument("cases", nargs="*", metavar="case", help="default: every case")
    parser.add_argument("--in-process", type=int, default=100)
    parser.add_argument("--subprocess", type=int, default=20)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--check", type=Path, default=None, help="directory of committed ledgers")
    parser.add_argument("--one", action="store_true", help="internal: one run, JSON on stdout")
    args = parser.parse_args()

    unknown = [c for c in args.cases if c not in CASES]
    if unknown:
        parser.error(f"unknown case(s) {unknown}; choose from {sorted(CASES)}")

    if args.one:
        print(json.dumps(_one(args.cases[0])))
        return 0

    ok = True
    for case in args.cases or sorted(CASES):
        ok = _repeat(case, args) and ok
    return 0 if ok else 1


def _repeat(case: str, args: argparse.Namespace) -> bool:
    started = time.perf_counter()
    reference = run_case(CASES[case](), seed=0)
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
        record(f"in-process #{i}", _one(case))
    in_process_seconds = time.perf_counter() - started

    def spawn(i: int) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ, PYTHONHASHSEED=str(1000 + i * 7919))
        return subprocess.run(
            [sys.executable, __file__, case, "--one"],
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
        )

    # Separate processes share nothing, so running them side by side changes only the wait.
    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
        for i, proc in enumerate(pool.map(spawn, range(args.subprocess))):
            if proc.returncode != 0:
                problems.append(f"subprocess #{i}: exit {proc.returncode}: {proc.stderr[-400:]}")
                continue
            record(f"subprocess #{i}", json.loads(proc.stdout.strip().splitlines()[-1]))

    tag = f"lk-{versions()['livekit-agents']}"
    committed_matches = None
    if args.check:
        committed = args.check / f"{tag}.{case}.ledger.normalized.jsonl"
        if not committed.exists():
            committed_matches = False
            problems.append(f"no committed ledger to check against: {committed}")
        else:
            committed_hash = hashlib.sha256(committed.read_bytes()).hexdigest()
            committed_matches = committed_hash == reference.hash
            if not committed_matches:
                diff = first_difference(committed.read_text("ascii").splitlines(), ref_lines)
                problems.append(
                    f"hash {reference.hash} differs from committed {committed_hash}; "
                    f"first difference {diff}"
                )

    summary = {
        "case": case,
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
        "virtual_seconds_per_run": reference.ledger.events[-1]["t_us"] / 1e6,
    }
    # Wall time and the --check outcome are printed but kept out of the written evidence,
    # so that reproducing a result rewrites the evidence files byte for byte.
    printed = {
        **summary,
        "matches_committed": committed_matches,
        "in_process_wall_seconds": round(in_process_seconds, 2),
    }
    print(json.dumps(printed, indent=2))

    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / f"{tag}.{case}.repeat.json").write_text(json.dumps(summary, indent=2) + "\n")
        write_jsonl(
            reference.ledger.events,
            args.out / f"{tag}.{case}.ledger.normalized.jsonl",
            normalized=True,
        )
    return bool(summary["identical"])


if __name__ == "__main__":
    sys.exit(main())
