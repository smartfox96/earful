"""Break the code on purpose, one place at a time, and check that a test fails each time.

Each mutant in tests/mutants.py replaces one piece of source text with a broken version.
The mutant is applied in a copy of the repository and the tests are run there. Run it with
the interpreter of a pinned environment:

    .venvs/lk-1.8.12/bin/python scripts/mutate.py              every mutant
    .venvs/lk-1.8.12/bin/python scripts/mutate.py --verify     only check that each still applies
    .venvs/lk-1.8.12/bin/python scripts/mutate.py --only L1    mutants whose name contains "L1"

A mutant is

    caught     if a test fails. The first failing test is printed.
    survived   if every test passes: the break is one no test notices.
    invalid    if its text is not found exactly once, if the broken file does not compile,
               or if pytest stops for a reason other than a failing test.

The unbroken code is run first, as a control: it must pass, or a failure says nothing.
A mutant that names tests to try first is run against those, and against the whole suite
only if they pass, so a wrong hint costs time and never hides a survivor.

The exit status is 0 if the control passes and every mutant is caught, and 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from mutants import MUTANTS, Mutant  # tests/ is put on the path above

_LEFT_OUT = shutil.ignore_patterns(
    ".git", ".venvs", "__pycache__", ".*_cache", ".hypothesis", "out", "evidence"
)
_PYTEST = ["-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"]
_FAILED = re.compile(r"^(?:FAILED|ERROR) (\S+::\S+)", re.MULTILINE)


@dataclass
class Result:
    name: str
    outcome: str  # caught, survived or invalid; passed or failed for the control
    detail: str  # the first failing test, or why the mutant is invalid


def problem(mutant: Mutant, root: Path = ROOT) -> str | None:
    """Why ``mutant`` cannot be applied to the code under ``root``, or None if it can."""
    path = root / mutant.path
    if not path.is_file():
        return f"{mutant.path} does not exist"
    text = path.read_text()
    found = text.count(mutant.old)
    if found != 1:
        return f"its text is found {found} times in {mutant.path}"
    if mutant.old == mutant.new:
        return "it changes nothing"
    try:
        compile(text.replace(mutant.old, mutant.new), mutant.path, "exec")
    except SyntaxError as exc:
        return f"the broken file does not compile: {exc.msg} (line {exc.lineno})"
    return None


def _pytest(copy: Path, *selection: str) -> tuple[int, str]:
    """Run the tests of the copy against the code of the copy."""
    env = {**os.environ, "PYTHONPATH": f"{copy / 'src'}{os.pathsep}{copy / 'tests'}"}
    done = subprocess.run(
        [sys.executable, *_PYTEST, *selection],
        cwd=copy,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode, done.stdout


def _judge(code: int, output: str) -> tuple[str, str] | None:
    """The outcome of one pytest run: caught, invalid, or None if every test passed."""
    if code == 0:
        return None
    if code == 1:
        failed = _FAILED.search(output)
        return "caught", failed.group(1) if failed else "a test failed"
    last = output.strip().splitlines()[-1] if output.strip() else "no output"
    return "invalid", f"pytest exited with status {code}: {last}"


def run(mutant: Mutant | None, scratch: Path, number: int) -> Result:
    """Apply ``mutant`` in a fresh copy of the repository and run the tests there.
    ``None`` is the control: nothing is broken, and every test must pass."""
    name = mutant.name if mutant else "control: nothing is broken"
    if mutant is not None and (why := problem(mutant)) is not None:
        return Result(name, "invalid", why)
    copy = scratch / f"copy-{number}"
    shutil.copytree(ROOT, copy, ignore=_LEFT_OUT)
    try:
        if mutant is not None:
            target = copy / mutant.path
            target.write_text(target.read_text().replace(mutant.old, mutant.new))
            if mutant.first:
                judged = _judge(*_pytest(copy, "-k", mutant.first))
                if judged is not None and judged[0] == "caught":
                    return Result(name, *judged)
        judged = _judge(*_pytest(copy))
        if mutant is None:
            return Result(name, "failed" if judged else "passed", judged[1] if judged else "")
        return Result(name, *(judged or ("survived", "every test passes")))
    finally:
        shutil.rmtree(copy, ignore_errors=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--verify", action="store_true", help="only check that each applies")
    parser.add_argument("--only", action="append", default=[], metavar="TEXT")
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    parser.add_argument("--json", type=Path, metavar="FILE", help="also write the results here")
    args = parser.parse_args(argv)

    names = [m.name for m in MUTANTS]
    repeated = sorted({n for n in names if names.count(n) > 1})
    if repeated:
        print(f"mutant names used twice: {repeated}")
        return 1
    chosen = [m for m in MUTANTS if not args.only or any(text in m.name for text in args.only)]

    if args.verify:
        broken = [(m.name, why) for m in chosen if (why := problem(m)) is not None]
        for name, why in broken:
            print(f"invalid   {name}: {why}")
        print(f"{len(chosen) - len(broken)} of {len(chosen)} mutants apply")
        return 1 if broken else 0

    jobs: list[Mutant | None] = [None, *chosen]
    done: dict[int, Result] = {}
    with (
        tempfile.TemporaryDirectory(prefix="earful-mutate-") as scratch,
        ThreadPoolExecutor(args.workers) as pool,
    ):
        futures = {pool.submit(run, job, Path(scratch), n): n for n, job in enumerate(jobs)}
        for future in as_completed(futures):
            result = done[futures[future]] = future.result()
            print(f"{result.outcome:9} {result.name}: {result.detail}".rstrip(": "), flush=True)

    control, *judged = (done[n] for n in range(len(jobs)))
    counts = {o: sum(r.outcome == o for r in judged) for o in ("caught", "survived", "invalid")}
    print(
        f"{len(judged)} mutants: {counts['caught']} caught, "
        f"{counts['survived']} survived, {counts['invalid']} invalid"
    )
    if control.outcome != "passed":
        print("the control failed: the unbroken code does not pass, so the results mean nothing")
    if args.json:
        args.json.write_text(json.dumps([asdict(r) for r in (control, *judged)], indent=2) + "\n")
    return 0 if control.outcome == "passed" and counts["caught"] == len(judged) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
