"""What a run of ci has to do: the jobs a change selects, and the reason for each.

  python3 scripts/ci_scope.py --explain <base> [<head>]   the jobs a diff selects, and why
  python3 scripts/ci_scope.py --table                      the table, as CONTRIBUTING.md shows it
  python3 scripts/ci_scope.py --github                     for a run of ci at HEAD, from the
      variables the workflow sets: TIER, EVENT, ACTION, BEFORE and BASE_SHA

A path selects what the first rule it matches says. A path no rule matches runs everything.
The full tier runs everything whatever changed. Needs Python 3.10 and git.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mutate

# Each supported release, oldest first, with the date its environment is resolved as of.
LIVEKIT = {
    "1.5.6": "2026-04-30",
    "1.5.7": "2026-05-06",
    "1.6.5": "2026-07-20",
    "1.6.6": "2026-07-20",
    "1.8.12": "2026-11-14",
    "1.8.13": "2026-11-20",
}
PIPECAT = {"1.16.0": "2026-11-18", "1.17.0": "2026-11-24"}
# Every other Python the package supports runs once, with one LiveKit release.
OTHER_PYTHONS = {"3.10": "1.6.6", "3.11": "1.5.6", "3.12": "1.8.13", "3.14": "1.8.13"}

# What a group of paths runs before a pull request merges. An item is a job, a cell
# ("lk <release> <system> [<python>]", "pc <system> <python> [wheel]"), or another group.
GROUPS = {
    "prose": ("prose",),
    "pipecat": ("lint", "pc linux 3.13", "pc linux 3.11"),
    "livekit": ("lint", "lk every linux", "quickstart"),
    "its release": ("lk its linux",),
    "core": ("livekit", "pipecat", "wheel"),
    "clock": ("core", "lk oldest macos", "lk newest macos", "lk other pythons", "pc macos 3.13"),
    "report": ("lint", "lk newest linux", "report"),
    "build": (
        "lint",
        "lk oldest linux",
        "lk newest linux",
        "pc linux 3.12 wheel",
        "wheel",
        "quickstart",
    ),
    "breaks": ("lint",),
    "everything": ("full",),
}
# What the full tier runs: each night on main, on request, and before a release.
FULL = (
    "prose",
    "lint",
    "wheel",
    "quickstart",
    "report",
    "bare",
    "lk every linux",
    "lk every macos",
    "lk other pythons",
    "pc linux 3.13",
    "pc linux 3.11",
    "pc linux 3.12 wheel",
    "pc macos 3.13",
)

# The first rule a path matches decides. "*" stays within a directory and "**" does not.
# "declared" is the framework the file needs: a test file's pytest.importorskip, a helper's
# imports. Anything no rule names runs everything, the workflows and scripts among them.
RULES = (
    ("docs/**", "prose"),
    ("*.md", "prose"),
    (".github/ISSUE_TEMPLATE/**", "prose"),
    (".github/pull_request_template.md", "prose"),
    ("compat/lk-*.lock.txt", "its release"),
    ("compat/pc-*.lock.txt", "pipecat"),
    ("src/earful/pipecat/**", "pipecat"),
    ("examples/pipecat_booking/**", "pipecat"),
    ("tests/golden/pc-*", "pipecat"),
    ("tests/shapes/pipecat/**", "pipecat"),
    ("src/earful/livekit/**", "livekit"),
    ("examples/booking/**", "livekit"),
    ("skills/**", "livekit"),
    ("tests/golden/lk-*", "livekit"),
    ("tests/bundles/**", "livekit"),
    ("tests/shapes/livekit/**", "livekit"),
    ("src/earful/scripted/clock.py", "clock"),
    ("src/earful/scripted/timers.py", "clock"),
    ("src/earful/scripted/ceilings.py", "clock"),
    ("src/earful/scripted/guards.py", "clock"),
    ("src/earful/core/html.py", "report"),
    ("tests/mutants.py", "breaks"),
    ("tests/mutants_seconds.json", "breaks"),
    ("tests/conftest.py", "core"),
    ("tests/*.py", "declared"),
    ("tests/shapes/*.py", "declared"),
    ("src/**", "core"),
    ("releases.toml", "everything"),
    ("pyproject.toml", "build"),
    ("LICENSE", "build"),
    ("NOTICE", "build"),
    (".gitignore", "build"),
)
UNKNOWN = "everything"
# On a pull request the middle LiveKit releases leave out the slow tests: the oldest and
# the newest run them, and every cell runs them in the full tier.
SLOW_ON_A_CHANGE = ("oldest", "newest")

_SYSTEMS = {"linux": "ubuntu-latest", "macos": "macos-latest"}
_NAMES = {"linux": "Linux", "macos": "macOS"}
_JOBS = {
    "prose": "the tests that read the pages",
    "lint": "format, lint, types",
    "wheel": "the wheel installs clean",
    "quickstart": "the quickstart from the wheel",
    "report": "the example reports",
    "bare": "no framework, the oldest jsonschema",
}
# The jobs of checks.yml after the scope, in its order.
JOB_ORDER: tuple[str, ...] = ("prose", "lint", "wheel", "quickstart", "report", "livekit")
JOB_ORDER += ("pipecat", "bare", "control", "breaks")
_SKIPS_ITSELF = re.compile(r'^pytest\.importorskip\("(livekit|pipecat)[."]', re.MULTILINE)
_IMPORTS = re.compile(r"^\s*(?:import|from)\s+(?:earful\.)?(livekit|pipecat)\b", re.MULTILINE)


@dataclass(frozen=True, order=True)
class Cell:
    framework: str  # lk or pc
    version: str
    system: str  # linux or macos
    python: str
    source: str = "checkout"  # or wheel

    @property
    def name(self) -> str:
        built = " / the wheel" if self.source == "wheel" else ""
        return f"{self.framework} {self.version} / {_NAMES[self.system]} / py{self.python}{built}"


@dataclass
class Plan:
    tier: str
    jobs: dict[str, str] = field(default_factory=dict)  # a job, and why it runs
    cells: dict[Cell, str] = field(default_factory=dict)
    breaks: list[mutate.Mutant] = field(default_factory=list)
    breaks_why: str = ""
    paths: list[tuple[str, str, str]] = field(default_factory=list)  # path, rule, group
    since: str = ""  # the commit the breaks are chosen since; empty for every break

    @property
    def left(self) -> list[str]:
        """What the full tier runs and this run does not."""
        jobs, cells = _expand(FULL, "")
        left = [_JOBS[j] for j in jobs if j not in self.jobs and j != "prose"]
        left += [cell.name for cell in sorted(cells) if cell not in self.cells]
        if len(self.breaks) < len(mutate.MUTANTS):
            left.append(f"{len(mutate.MUTANTS) - len(self.breaks)} deliberate breaks")
        return left


def _pattern(glob: str) -> re.Pattern[str]:
    parts = re.split(r"(\*\*|\*)", glob)
    swap = {"**": ".*", "*": "[^/]*"}
    return re.compile("".join(swap.get(part, re.escape(part)) for part in parts))


def rule_of(path: str) -> tuple[str, str]:
    """The rule ``path`` matches, as its pattern and its group."""
    for glob, group in RULES:
        if _pattern(glob).fullmatch(path):
            return glob, group
    return "no rule", UNKNOWN


def declared(text: str, test_file: bool) -> str:
    """The group of a file under tests/: the framework it says it needs, or core."""
    found = set((_SKIPS_ITSELF if test_file else _IMPORTS).findall(text))
    if len(found) == 1:
        return {"livekit": "livekit", "pipecat": "pipecat"}[found.pop()]
    return "core" if not found else UNKNOWN


def group_of(path: str, read: Callable[[str], str | None]) -> tuple[str, str]:
    """The rule ``path`` matches and the group it selects. ``read`` gives a file's text."""
    glob, group = rule_of(path)
    if group == "declared":
        text = read(path)
        group = UNKNOWN if text is None else declared(text, Path(path).name.startswith("test_"))
    return glob, group


def _cells(item: str, release: str | None) -> list[Cell]:
    words = item.split()
    if words[0] == "pc":
        source = words[3] if len(words) > 3 else "checkout"
        return [Cell("pc", v, words[1], words[2], source) for v in PIPECAT]
    if words[1:] == ["other", "pythons"]:
        return [Cell("lk", v, "linux", py) for py, v in OTHER_PYTHONS.items()]
    which, system = words[1], words[2]
    python = words[3] if len(words) > 3 else "3.13"
    versions = list(LIVEKIT)
    chosen = {
        "every": versions,
        "oldest": versions[:1],
        "newest": versions[-1:],
        "its": [release] if release in LIVEKIT else versions,
    }.get(which, [which])
    return [Cell("lk", v, system, python) for v in chosen]


def _expand(items: tuple[str, ...], path: str) -> tuple[list[str], list[Cell]]:
    """The jobs and the cells ``items`` stand for. ``path`` names the release of a lock file."""
    jobs: list[str] = []
    cells: list[Cell] = []
    for item in items:
        more_cells: list[Cell] = []
        if item in _JOBS:
            more_jobs = [item]
        elif item in GROUPS:
            more_jobs, more_cells = _expand(GROUPS[item], path)
        elif item == "full":
            more_jobs, more_cells = _expand(FULL, path)
        else:
            found = re.fullmatch(r"compat/lk-(.+)\.lock\.txt", path)
            more_jobs, more_cells = [], _cells(item, found.group(1) if found else None)
        jobs += [j for j in more_jobs if j not in jobs]
        cells += [c for c in more_cells if c not in cells]
    return jobs, cells


def plan(tier: str, paths: list[str] | None, read=None, before=None) -> Plan:
    """What a run does. ``paths`` None: everything, as when the base cannot be read."""
    result = Plan(tier)
    if tier == "full" or paths is None:
        why = "the full tier" if tier == "full" else "the change cannot be read"
        jobs, cells = _expand(FULL, "")
        result.jobs = dict.fromkeys(jobs, why)
        result.cells = dict.fromkeys(cells, why)
        result.breaks, result.breaks_why = list(mutate.MUTANTS), why
        return _without_extra_prose(result)
    read = read or (lambda path: None)
    everything = False
    for path in paths:
        glob, group = group_of(path, read)
        result.paths.append((path, glob, group))
        everything |= group == UNKNOWN
        jobs, cells = _expand(GROUPS[group], path)
        why = f"{path} is {group}" if glob != "no rule" else f"no rule names {path}"
        for job in jobs:
            result.jobs.setdefault(job, why)
        for cell in cells:
            result.cells.setdefault(cell, why)
    if everything:
        result.breaks, result.breaks_why = list(mutate.MUTANTS), "a path no rule names"
    else:
        result.breaks = mutate.chosen(paths, before)
        result.breaks_why = "those the changed files can affect (scripts/mutate.py)"
    return _without_extra_prose(result)


def _without_extra_prose(result: Plan) -> Plan:
    """The tests that read the pages run in every cell: their own job only runs without one
    of each framework."""
    frameworks = {cell.framework for cell in result.cells}
    if "prose" in result.jobs and frameworks == {"lk", "pc"}:
        del result.jobs["prose"]
    return result


def _slow(cell: Cell, tier: str) -> bool:
    if tier == "full" or cell.framework != "lk" or cell.system != "linux" or cell.python != "3.13":
        return True
    versions = list(LIVEKIT)
    edges = {"oldest": versions[0], "newest": versions[-1]}
    return cell.version in (edges[which] for which in SLOW_ON_A_CHANGE)


def github(result: Plan) -> dict[str, str]:
    """The outputs of the scope job: what each job of checks.yml reads."""
    newest = list(LIVEKIT)[-1]
    livekit = [
        {
            "name": cell.name,
            "version": cell.version,
            "as_of": LIVEKIT[cell.version],
            "os": _SYSTEMS[cell.system],
            "python": cell.python,
            "slow": _slow(cell, result.tier),
            "control": cell == Cell("lk", newest, "linux", "3.13"),
        }
        for cell in sorted(result.cells)
        if cell.framework == "lk"
    ]
    pipecat = [
        {
            "name": cell.name,
            "version": cell.version,
            "as_of": PIPECAT[cell.version],
            "os": _SYSTEMS[cell.system],
            "python": cell.python,
            "from": cell.source,
        }
        for cell in sorted(result.cells)
        if cell.framework == "pc"
    ]
    n = mutate.part_count(result.breaks)
    parts = [{"part": k, "of": n} for k in range(1, n + 1)]
    ran = {
        *result.jobs,
        *(name for name, cells in (("livekit", livekit), ("pipecat", pipecat)) if cells),
    }
    ran |= {"control", "breaks"} if parts else set()
    due = [job for job in JOB_ORDER if job in ran]
    outputs = {job: str(job in result.jobs).lower() for job in _JOBS}
    outputs |= {
        "livekit": json.dumps(livekit),
        "pipecat": json.dumps(pipecat),
        "breaks": json.dumps(parts),
        "since": result.since,
        "due": json.dumps(due),
        "explained": json.dumps(explain(result)),
    }
    return outputs


def explain(result: Plan) -> str:
    lines = [f"Tier: {result.tier}."]
    if result.paths:
        lines.append(f"{len(result.paths)} changed paths:")
        width = max(len(path) for path, _, _ in result.paths)
        lines += [f"  {path:{width}}  {group}  ({glob})" for path, glob, group in result.paths]
    if not result.jobs and not result.cells and not result.breaks:
        lines.append("Nothing to run.")
    rows = [(_JOBS[job], why) for job, why in result.jobs.items()]
    rows += [(cell.name, why) for cell, why in sorted(result.cells.items())]
    if result.breaks:
        n = mutate.part_count(result.breaks)
        parts = "1 part" if n == 1 else f"{n} parts"
        rows.append((f"{len(result.breaks)} deliberate breaks, in {parts}", result.breaks_why))
    if rows:
        lines.append("Runs:")
        width = max(len(what) for what, _ in rows)
        lines += [f"  {what:{width}}  {why}" for what, why in rows]
    if result.left and result.tier != "full":
        lines.append("Left to the full tier:")
        lines += [f"  {what}" for what in result.left]
    return "\n".join(lines) + "\n"


def table() -> str:
    """The rules, and what each group selects, as two Markdown tables."""

    def describe(group: str) -> str:
        if GROUPS[group] == ("full",):
            return "the full tier: " + describe_items(FULL)
        return describe_items(GROUPS[group], group == "its release")

    def describe_items(items: tuple[str, ...], one_release: bool = False) -> str:
        jobs, cells = _expand(items, "compat/lk-<release>.lock.txt")
        said = [_JOBS[job] for job in jobs]
        lk = [c for c in cells if c.framework == "lk"]
        if one_release:
            said.append("LiveKit, that release alone")
        elif lk:
            versions = sorted({c.version for c in lk if c.system == "linux" and c.python == "3.13"})
            if len(versions) == len(LIVEKIT):
                said.append("LiveKit, every release")
            elif versions:
                said.append("LiveKit " + " and ".join(versions))
            macos = sorted({c.version for c in lk if c.system == "macos"})
            if len(macos) == len(LIVEKIT):
                said.append("LiveKit on macOS, every release")
            elif macos:
                said.append("LiveKit " + " and ".join(macos) + " on macOS")
            if any(c.python != "3.13" for c in lk):
                said.append("LiveKit on every other Python")
        pc = [c for c in cells if c.framework == "pc"]
        if pc:
            linux = sorted({c.python for c in pc if c.system == "linux" and c.source == "checkout"})
            if linux:
                said.append("Pipecat on Python " + " and ".join(linux))
            if any(c.source == "wheel" for c in pc):
                said.append("Pipecat from the wheel")
            if any(c.system == "macos" for c in pc):
                said.append("Pipecat on macOS")
        return "; ".join(said) if said else "the full tier"

    rows = ["| Path | Group |", "| --- | --- |"]
    rows += [f"| `{glob}` | {group} |" for glob, group in RULES]
    rows += [f"| anything else | {UNKNOWN} |", "", "| Group | Runs before a pull request merges |"]
    rows.append("| --- | --- |")
    rows.append(
        "| declared | the group of the framework the file needs: livekit, pipecat or core |"
    )
    rows += [f"| {group} | {describe(group)} |" for group in GROUPS]
    return "\n".join(rows) + "\n"


def _git(*args: str) -> str:
    """Run git in the repository here: the workflow and a reader run this at its root."""
    done = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout


def _known(commit: str) -> bool:
    try:
        _git("cat-file", "-e", f"{commit}^{{commit}}")
    except RuntimeError:
        return False
    return bool(commit)


def _reader(base: str, head: str):
    def read(path: str) -> str | None:
        for commit in (head, base):
            try:
                return _git("show", f"{commit}:{path}")
            except RuntimeError:
                continue
        return None

    return read


def from_diff(tier: str, base: str, head: str) -> Plan:
    if not _known(base):
        return plan(tier, None)
    paths = _git("diff", "--no-renames", "--name-only", base, head).splitlines()
    before = mutate.breaks_at(base, Path.cwd()) if mutate.MUTANTS_FILE in paths else None
    result = plan(tier, paths, _reader(base, head), before)
    result.since = (
        "" if len(result.breaks) == len(mutate.MUTANTS) else _git("rev-parse", base).strip()
    )
    return result


def from_event(env: dict[str, str]) -> Plan:
    """The plan for a run of ci at HEAD, from what the workflow passes."""
    tier = env.get("TIER", "")
    if tier not in ("pull-request", "full"):
        raise SystemExit(f"TIER is {tier!r}; it can be pull-request or full")
    event = env.get("EVENT", "")
    if tier == "full":
        return plan("full", None)
    if event == "pull_request":
        # HEAD is the merge of the pull request into its base, made by GitHub for this event.
        if env.get("ACTION") == "edited" and _git("rev-parse", "HEAD^1").strip() != env.get(
            "BASE_SHA"
        ):
            raise SystemExit(
                "The base of the pull request changed and GitHub has not merged it with the new "
                "base yet. Push to the branch again, or close and reopen the pull request."
            )
        return from_diff(tier, "HEAD^1", "HEAD")
    if event == "push":
        tested = subprocess.run(
            [str(ROOT / "scripts" / "ci_tested.sh"), _git("rev-parse", "HEAD").strip()],
            capture_output=True,
            text=True,
            check=False,
        )
        if tested.stdout.strip() == "tested=true":
            print("These files passed ci as a pull request: nothing to run again.", file=sys.stderr)
            return Plan(tier)
        return from_diff(tier, env.get("BEFORE", ""), "HEAD")
    raise SystemExit(f"EVENT is {event!r}; a pull-request tier runs for pull_request or push")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    how = parser.add_mutually_exclusive_group(required=True)
    how.add_argument("--explain", nargs="+", metavar="COMMIT", help="<base> [<head>]")
    how.add_argument("--table", action="store_true")
    how.add_argument("--github", action="store_true")
    args = parser.parse_args(argv)
    if args.table:
        print(table(), end="")
        return 0
    if args.explain:
        if len(args.explain) > 2:
            parser.error("--explain takes a base and, if it is not HEAD, a head")
        base, head = (*args.explain, "HEAD")[:2]
        print(explain(from_diff("pull-request", base, head)), end="")
        return 0
    result = from_event(dict(os.environ))
    for name, value in github(result).items():
        print(f"{name}={value}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as out:
            out.write(f"```\n{explain(result)}```\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
