"""Break the code on purpose, one place at a time, and check that a test it names fails each time.

.venvs/lint/bin/python scripts/mutate.py                the control, then every break
.venvs/lint/bin/python scripts/mutate.py --verify       only check that each break still applies
.venvs/lint/bin/python scripts/mutate.py --only L1      the breaks whose name contains "L1"
.venvs/lint/bin/python scripts/mutate.py --since main   the breaks a change since main can affect
.venvs/lint/bin/python scripts/mutate.py --control      the control alone: the unbroken code passes
.venvs/lint/bin/python scripts/mutate.py --part 2/3     the second of three parts, no control
.venvs/lint/bin/python scripts/mutate.py --record F...  keep the seconds of the results in files F

A break runs the test files that hold its named tests. Only if none of them fails does it
run its named tests in the whole suite, and then the whole suite, to tell a misnamed break
from one that survives.
"""

from __future__ import annotations

import argparse
import functools
import importlib.util
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, astuple, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from mutants import MUTANTS, Mutant

MUTANTS_FILE = "tests/mutants.py"
SECONDS_FILE = "tests/mutants_seconds.json"
# A part holds about this many seconds of breaks, as recorded with four workers at once.
PART_SECONDS = 480
MOST_PARTS = 6
# Longer than any break has taken, and far short of a job's limit.
TIMEOUT_SECONDS = 300

_LEFT_OUT = shutil.ignore_patterns(
    ".git", ".venvs", "__pycache__", ".*_cache", ".hypothesis", "out", "evidence", ".claude"
)
_PYTEST = ["-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"]
_FAILED = re.compile(r"^(?:FAILED|ERROR) (\S+::\S+)", re.MULTILINE)
_TEST = re.compile(r"^\s*(?:async )?def (test_\w+)", re.MULTILINE)
_ASYNC_TEST = re.compile(r"^\s*async def test_", re.MULTILINE)
_MARK = re.compile(r"pytest\.mark\.(\w+)")
_WORD = re.compile(r"[^\s()]+")


@dataclass
class Result:
    name: str
    outcome: (
        str  # caught, misnamed, survived, timed out or invalid; passed or failed for the control
    )
    detail: str  # the failing test, or why the break is invalid
    seconds: float = 0.0


@functools.cache
def _test_names(root: Path) -> frozenset[str]:
    """The name of every test function under ``root``, in lower case."""
    return frozenset(
        name.lower()
        for path in (root / "tests").glob("test_*.py")
        for name in _TEST.findall(path.read_text())
    )


@functools.cache
def _keywords(root: Path) -> dict[str, frozenset[str]]:
    """What ``pytest -k`` matches a word against in each test file, as far as the text shows:
    its tests' names, its own name, and its markers."""
    found = {}
    for path in sorted((root / "tests").glob("test_*.py")):
        text = path.read_text()
        names = {name.lower() for name in _TEST.findall(text)} | {path.name.lower()}
        names |= {mark.lower() for mark in _MARK.findall(text)}
        if _ASYNC_TEST.search(text):
            names.add("asyncio")  # pytest-asyncio marks each async test
        found[path.relative_to(root).as_posix()] = frozenset(names)
    return found


def _words(first: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(first) if w not in ("and", "or", "not")]


def files_of(mutant: Mutant, root: Path = ROOT) -> tuple[str, ...]:
    """The test files a break runs first: those that hold a test its ``first`` names."""
    keywords = _keywords(root)
    words = _words(mutant.first)
    if any(word in "tests" for word in words):  # the directory's name matches every test
        return tuple(keywords)
    return tuple(f for f, names in keywords.items() if any(w in n for w in words for n in names))


def problem(mutant: Mutant, root: Path = ROOT) -> str | None:
    """Why ``mutant`` cannot be run against the code under ``root``, or None if it can."""
    path = root / mutant.path
    if not path.is_file():
        return f"{mutant.path} does not exist"
    text = path.read_text()
    found = text.count(mutant.old)
    if found != 1:
        return f"its text is found {found} times in {mutant.path}"
    if mutant.old == mutant.new:
        return "it changes nothing"
    if path.suffix == ".py":
        try:
            compile(text.replace(mutant.old, mutant.new), mutant.path, "exec")
        except SyntaxError as exc:
            return f"the broken file does not compile: {exc.msg} (line {exc.lineno})"
    # pytest -k takes a word as part of a name, whatever its case.
    words = _words(mutant.first)
    if not words:
        return "it names no test to catch it"
    for word in words:
        if not any(word in name for name in _test_names(root)):
            return f"{word!r} in its first is part of no test's name"
    return None


def _every_break(path: str) -> bool:
    """A change to the runner or to a helper the tests share can change any break."""
    if path == "scripts/mutate.py":
        return True
    name = path.removeprefix("tests/")
    return (
        name != path
        and "/" not in name
        and name.endswith(".py")
        and not (name.startswith("test_") or path == MUTANTS_FILE)
    )


def chosen(
    paths: Iterable[str], before: dict[str, tuple[str, ...]] | None = None, root: Path = ROOT
) -> list[Mutant]:
    """The breaks a change to ``paths`` can affect: those in a changed file, those whose
    tests are in a changed file, and those ``tests/mutants.py`` changed or added. ``before``
    is each break as it was, by name; without it a change to that file chooses every break."""
    paths = set(paths)
    if any(_every_break(path) for path in paths):
        return list(MUTANTS)
    if MUTANTS_FILE in paths and before is None:
        return list(MUTANTS)
    picked = []
    for mutant in MUTANTS:
        if (
            mutant.path in paths
            or paths.intersection(files_of(mutant, root))
            or (
                MUTANTS_FILE in paths
                and before is not None
                and before.get(mutant.name) != astuple(mutant)
            )
        ):
            picked.append(mutant)
    return picked


def breaks_at(commit: str, root: Path = ROOT) -> dict[str, tuple[str, ...]] | None:
    """Each break as ``tests/mutants.py`` held it at ``commit``, or None if it cannot be read."""
    done = subprocess.run(
        ["git", "show", f"{commit}:{MUTANTS_FILE}"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        return None
    module = types.ModuleType("mutants_before")
    sys.modules[module.__name__] = module  # where a dataclass looks for its own module
    try:
        # The file as this repository held it at a commit of its own.
        exec(compile(done.stdout, f"{commit}:{MUTANTS_FILE}", "exec"), module.__dict__)  # noqa: S102
    finally:
        del sys.modules[module.__name__]
    return {m.name: astuple(m) for m in module.MUTANTS}


def changed_since(commit: str, root: Path = ROOT) -> list[str] | None:
    """The paths that differ between ``commit`` and HEAD, or None if ``commit`` is unknown."""
    known = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if known.returncode != 0:
        return None
    done = subprocess.run(
        ["git", "diff", "--no-renames", "--name-only", commit, "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.splitlines()


def chosen_since(commit: str, root: Path = ROOT) -> list[Mutant]:
    """The breaks a change from ``commit`` to HEAD can affect: every break if it is unknown."""
    paths = changed_since(commit, root)
    if paths is None:
        return list(MUTANTS)
    return chosen(paths, breaks_at(commit, root) if MUTANTS_FILE in paths else None, root)


def recorded(root: Path = ROOT) -> dict[str, float]:
    path = root / SECONDS_FILE
    return json.loads(path.read_text()) if path.is_file() else {}


def _seconds(mutants: list[Mutant], root: Path) -> dict[str, float]:
    """Each break's recorded seconds; one not yet recorded counts as the median of the rest."""
    known = recorded(root)
    values = sorted(known.values())
    usual = values[len(values) // 2] if values else 5.0
    return {m.name: known.get(m.name, usual) for m in mutants}


def part_count(mutants: list[Mutant], root: Path = ROOT) -> int:
    """How many parts ``mutants`` are cut into, so that each takes about PART_SECONDS."""
    if not mutants:
        return 0
    total = sum(_seconds(mutants, root).values())
    return max(1, min(MOST_PARTS, math.ceil(total / PART_SECONDS)))


def part(mutants: list[Mutant], k: int, n: int, root: Path = ROOT) -> list[Mutant]:
    """The ``k``-th of ``n`` parts: the longest break first, each to the part with least."""
    seconds = _seconds(mutants, root)
    loads = [0.0] * n
    held: list[list[Mutant]] = [[] for _ in range(n)]
    for mutant in sorted(mutants, key=lambda m: (-seconds[m.name], m.name)):
        least = loads.index(min(loads))
        loads[least] += seconds[mutant.name]
        held[least].append(mutant)
    return [m for m in mutants if m in held[k - 1]]


def _copy(root: Path, copy: Path) -> None:
    """The files git knows, with the compiled modules, so that a break compiles one file."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if listed.returncode != 0:  # not a checkout: an unpacked source distribution
        shutil.copytree(root, copy, ignore=_LEFT_OUT)
        return
    for name in listed.stdout.decode().split("\0"):
        source = root / name
        if name and source.is_file():
            (copy / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, copy / name)
    for cache in [*root.glob("src/**/__pycache__"), *root.glob("tests/__pycache__")]:
        target = copy / cache.relative_to(root)
        if target.parent.is_dir():
            shutil.copytree(cache, target, dirs_exist_ok=True)


def _pytest(copy: Path, *selection: str, timeout: float) -> tuple[int, str]:
    """Run the tests of the copy against the code of the copy. Status None: it timed out."""
    env = {**os.environ, "PYTHONPATH": f"{copy / 'src'}{os.pathsep}{copy / 'tests'}"}
    with subprocess.Popen(
        [sys.executable, *_PYTEST, *selection],
        cwd=copy,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,  # a search's own processes are stopped with it
    ) as process:
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            return -1, f"no result after {timeout:.0f} s"
        return process.returncode, output


def _judge(code: int, output: str) -> tuple[str, str] | None:
    """The outcome of one pytest run: caught, timed out, invalid, or None if every test passed."""
    if code == 0:
        return None
    if code == -1:
        return "timed out", output
    if code == 1:
        # The last failure named: a pytester test prints its project's failures before its own.
        failed = _FAILED.findall(output)
        return "caught", failed[-1] if failed else "a test failed"
    last = output.strip().splitlines()[-1] if output.strip() else "no output"
    return "invalid", f"pytest exited with status {code}: {last}"


def run(mutant: Mutant, scratch: Path, number: int, timeout: float) -> Result:
    """Run the tests against ``mutant`` in a fresh copy."""
    started = time.monotonic()
    outcome, detail = _outcome(mutant, scratch / f"copy-{number}", timeout)
    return Result(mutant.name, outcome, detail, round(time.monotonic() - started, 1))


def _outcome(mutant: Mutant, copy: Path, timeout: float) -> tuple[str, str]:
    if (why := problem(mutant)) is not None:
        return "invalid", why
    _copy(ROOT, copy)
    try:
        target = copy / mutant.path
        target.write_text(target.read_text().replace(mutant.old, mutant.new))
        for stale in (target.parent / "__pycache__").glob(f"{target.stem}.*.pyc"):
            stale.unlink()
        # The files of its named tests, then its named tests anywhere: the same tests a
        # whole-suite -k selects, the files that hold them first.
        for selection in ([*files_of(mutant), "-k", mutant.first], ["-k", mutant.first]):
            judged = _judge(*_pytest(copy, *selection, timeout=timeout))
            if judged is not None and judged[0] in ("caught", "timed out"):
                return judged
        # The whole suite runs only if the named tests pass, so a wrong name hides no survivor.
        judged = _judge(*_pytest(copy, timeout=timeout))
        if judged is None:
            return "survived", "every test passes"
        if judged[0] == "caught":
            return "misnamed", f"no test it names fails, and this one does: {judged[1]}"
        return judged
    finally:
        shutil.rmtree(copy, ignore_errors=True)


def control(mutants: list[Mutant], workers: int, timeout: float) -> Result:
    """The unbroken code passes every test file the breaks run."""
    started = time.monotonic()
    files = sorted({f for mutant in mutants for f in files_of(mutant)})
    judged: tuple[str, str] | None = None
    if files:
        spread = ["-n", str(workers)] if importlib.util.find_spec("xdist") and workers > 1 else []
        env = {**os.environ, "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{ROOT / 'tests'}"}
        env.setdefault("EARFUL_WORKERS", "0")  # pytest's workers share the processors
        command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *spread]
        try:
            done = subprocess.run(
                [*command, *files],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout * 4,
            )
            judged = _judge(done.returncode, done.stdout)
        except subprocess.TimeoutExpired:
            judged = "timed out", f"no result after {timeout * 4:.0f} s"
    outcome, detail = ("failed", judged[1]) if judged else ("passed", f"{len(files)} test files")
    seconds = round(time.monotonic() - started, 1)
    return Result("control: nothing is broken", outcome, detail, seconds)


def fraction(text: str) -> tuple[int, int]:
    """``K/N`` as two numbers, with 1 <= K <= N."""
    try:
        part, _, of = text.partition("/")
        k, n = int(part), int(of)
    except ValueError:
        raise argparse.ArgumentTypeError(f"a part is K/N, such as 2/4; got {text!r}") from None
    if not 1 <= k <= n:
        raise argparse.ArgumentTypeError(f"a part is K/N with 1 <= K <= N; got {text!r}")
    return k, n


def record(files: list[Path], root: Path = ROOT) -> int:
    """Keep the seconds each break took, from the result files of a run of every break."""
    seconds = {}
    for path in files:
        for result in json.loads(path.read_text()):
            if not result["name"].startswith("control:"):
                seconds[result["name"]] = result["seconds"]
    missing = sorted({m.name for m in MUTANTS} - set(seconds))
    if missing:
        print(f"{len(missing)} breaks have no result in these files, such as {missing[0]!r}")
        return 1
    ordered = {m.name: seconds[m.name] for m in MUTANTS}
    (root / SECONDS_FILE).write_text(json.dumps(ordered, indent=1) + "\n")
    print(f"{len(ordered)} breaks, {sum(ordered.values()) / 60:.0f} minutes in all")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--verify", action="store_true", help="only check that each applies")
    parser.add_argument("--only", action="append", default=[], metavar="TEXT")
    parser.add_argument("--since", metavar="COMMIT", help="the breaks a change since it affects")
    parser.add_argument("--control", action="store_true", help="the control alone")
    parser.add_argument("--part", type=fraction, metavar="K/N", help="one part, no control")
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--timeout", type=float, default=TIMEOUT_SECONDS, metavar="SECONDS")
    parser.add_argument("--json", type=Path, metavar="FILE", help="also write the results here")
    parser.add_argument("--record", type=Path, nargs="+", metavar="FILE")
    args = parser.parse_args(argv)

    names = [m.name for m in MUTANTS]
    repeated = sorted({n for n in names if names.count(n) > 1})
    if repeated:
        print(f"break names used twice: {repeated}")
        return 1
    if args.record:
        return record(args.record)
    picked = chosen_since(args.since) if args.since else list(MUTANTS)
    for text in args.only:
        if not any(text in m.name for m in MUTANTS):
            print(f"no break's name contains {text!r}")
            return 2
    picked = [m for m in picked if not args.only or any(text in m.name for text in args.only)]

    if args.verify:
        broken = [(m.name, why) for m in picked if (why := problem(m)) is not None]
        for name, why in broken:
            print(f"invalid   {name}: {why}")
        print(f"{len(picked) - len(broken)} of {len(picked)} breaks apply")
        return 1 if broken else 0

    results: list[Result] = []
    if not args.part:
        result = control(picked, args.workers, args.timeout)
        print(f"{result.outcome:9} {result.name}: {result.detail} ({result.seconds:.0f} s)")
        results.append(result)
    if args.part:
        k, n = args.part
        picked = part(picked, k, n)
    if not args.control and not (results and results[0].outcome != "passed"):
        results += _breaks(picked, args.workers, args.timeout)
    if args.json:
        args.json.write_text(json.dumps([asdict(r) for r in results], indent=2) + "\n")

    judged = [r for r in results if not r.name.startswith("control:")]
    outcomes = ("caught", "misnamed", "survived", "timed out", "invalid")
    counts = {o: sum(r.outcome == o for r in judged) for o in outcomes}
    if not args.control:
        print(f"{len(judged)} breaks: " + ", ".join(f"{counts[o]} {o}" for o in outcomes))
    if results and results[0].name.startswith("control:") and results[0].outcome != "passed":
        print("the control failed: the unbroken code does not pass, so no break was run")
        return 1
    return 0 if counts["caught"] == len(judged) else 1


def _breaks(picked: list[Mutant], workers: int, timeout: float) -> list[Result]:
    done: dict[int, Result] = {}
    with (
        tempfile.TemporaryDirectory(prefix="earful-mutate-") as scratch,
        ThreadPoolExecutor(max(1, workers)) as pool,
    ):
        futures = {
            pool.submit(run, mutant, Path(scratch), n, timeout): n
            for n, mutant in enumerate(picked)
        }
        for future in as_completed(futures):
            result = done[futures[future]] = future.result()
            line = f"{result.outcome:9} {result.name}: {result.detail}".rstrip(": ")
            print(f"{line} ({result.seconds:.0f} s)", flush=True)
    return [done[n] for n in range(len(picked))]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
