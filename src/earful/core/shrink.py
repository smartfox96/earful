"""The shrinker: reduce a failing case to the smallest one that fails the same way, and
set its times away from the edges of the failure."""

from __future__ import annotations

import copy
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, TypeVar

from . import case as cases
from .case import Variable
from .predicate import Predicate
from .search import DELAY_LIMIT_US, Run, Runner, Spent, Window, window

T = TypeVar("T")

# From the roundest time to the finest the shrinker will settle for, in microseconds.
GRIDS_US = (1_000_000, 500_000, 100_000, 50_000, 10_000, 1_000)
# How far from the one known edge of a half-open window a settled time stays: one VAD tick.
MARGIN_US = 50_000


def _keeping(case: cases.Case, name: str, keep: Sequence[int]) -> cases.Case:
    return {**copy.deepcopy(case), name: [copy.deepcopy(case[name][i]) for i in keep]}


def ddmin(items: Sequence[T], fails: Callable[[list[T]], bool]) -> list[T]:
    """Zeller and Hildebrandt's ddmin: a 1-minimal sub-list of ``items``, in order, for which
    ``fails`` is true. Raises ``ValueError`` if ``fails(list(items))`` is false. The empty
    list is tried first, which ddmin itself takes for granted does not fail."""
    current = list(items)
    if not fails(current):
        raise ValueError("ddmin needs a failing start")
    if fails([]):
        return []
    chunks = 2
    while len(current) >= 2:
        size = len(current) / chunks
        parts = [current[round(i * size) : round((i + 1) * size)] for i in range(chunks)]
        parts = [p for p in parts if p]
        reduced = False
        for part in parts:  # does one part alone still fail?
            if len(part) < len(current) and fails(part):
                current, chunks, reduced = part, 2, True
                break
        if not reduced and chunks > 2:
            for i in range(len(parts)):  # does the rest, without one part?
                rest = [x for j, part in enumerate(parts) if j != i for x in part]
                if fails(rest):
                    current, chunks, reduced = rest, max(chunks - 1, 2), True
                    break
        if not reduced:
            if chunks >= len(current):
                break
            chunks = min(chunks * 2, len(current))
    return current


@dataclass(frozen=True)
class Difference:
    """One way a case differs from the scenario its author wrote."""

    label: str  # for a person: "speech 'Stop.' moved to 3140000 us"
    target: tuple[Any, ...]  # what it changes: ("speech", n, "start"), ("setting", name), ...
    value: Any = None  # what it changes it to


def _pairs(author: cases.Case, found: cases.Case) -> list[tuple[int | None, int | None]]:
    # (author index, found index) pairs; None on a side for a speech the other does not have.
    taken: set[int] = set()
    out: list[tuple[int | None, int | None]] = []
    for j, theirs in enumerate(found["speeches"]):
        match = next(
            (
                i
                for i, ours in enumerate(author["speeches"])
                if i not in taken and ours["transcript"] == theirs["transcript"]
            ),
            None,
        )
        if match is not None:
            taken.add(match)
        out.append((match, j))
    out += [(i, None) for i in range(len(author["speeches"])) if i not in taken]
    return out


def differences(author: cases.Case, found: cases.Case) -> list[Difference]:
    """Every difference between ``found`` and ``author``, each small enough to undo alone."""
    out: list[Difference] = []
    for i, j in _pairs(author, found):
        if j is None:
            out.append(
                Difference(
                    f"speech {author['speeches'][i]['transcript']!r} dropped",
                    ("speech", i, "dropped"),
                )
            )
            continue
        theirs = found["speeches"][j]
        words = theirs["transcript"] or "(noise)"
        if i is None:
            out.append(
                Difference(
                    f"speech {words!r} added at {theirs['start_us']} us",
                    ("added", j),
                    copy.deepcopy(theirs),
                )
            )
            continue
        ours = author["speeches"][i]
        if theirs["start_us"] != ours["start_us"]:
            out.append(
                Difference(
                    f"speech {words!r} moved to {theirs['start_us']} us",
                    ("speech", i, "start"),
                    theirs["start_us"],
                )
            )
        length = theirs["end_us"] - theirs["start_us"]
        if length != ours["end_us"] - ours["start_us"]:
            out.append(
                Difference(f"speech {words!r} lasting {length} us", ("speech", i, "length"), length)
            )
        for name in ("stt_delay_us", "expects_reply", "backchannel", "retires"):
            if theirs[name] != ours[name]:
                out.append(
                    Difference(
                        f"speech {words!r} with {name} {theirs[name]!r}",
                        ("speech", i, name),
                        copy.deepcopy(theirs[name]),
                    )
                )
    if author["hangup_us"] != found["hangup_us"]:
        to = found["hangup_us"]
        out.append(
            Difference(
                "the user stays" if to is None else f"the user hangs up at {to} us", ("hangup",), to
            )
        )
    ours_delays = {(d["of"], d["nth"]): d["us"] for d in author["delays"]}
    theirs_delays = {(d["of"], d["nth"]): d["us"] for d in found["delays"]}
    for key in sorted(ours_delays.keys() | theirs_delays.keys()):
        if ours_delays.get(key) != theirs_delays.get(key):
            to = theirs_delays.get(key)
            label = f"{key[0]} {key[1]} " + (
                "left to the scenario's code" if to is None else f"declared as {to} us"
            )
            out.append(Difference(label, ("delay", *key), to))
    ours_faults = {f["nth"]: (f["of"], f["us"]) for f in author["faults"]}
    theirs_faults = {f["nth"]: (f["of"], f["us"]) for f in found["faults"]}
    for nth in sorted(ours_faults.keys() | theirs_faults.keys()):
        if ours_faults.get(nth) != theirs_faults.get(nth):
            to = theirs_faults.get(nth)
            if to is None:
                label = f"backend write {nth} without its fault"
            else:
                label = f"{to[0]} on backend write {nth}" + (
                    "" if to[1] is None else f", the caller waiting {to[1]} us"
                )
            out.append(Difference(label, ("fault", nth), to))
    for name in sorted(author["settings"].keys() | found["settings"].keys()):
        if author["settings"].get(name) != found["settings"].get(name):
            out.append(
                Difference(
                    f"setting {name} = {found['settings'].get(name)!r}",
                    ("setting", name),
                    found["settings"].get(name),
                )
            )
    for contract in sorted(author["contracts"].keys() | found["contracts"].keys()):
        if author["contracts"].get(contract) != found["contracts"].get(contract):
            out.append(
                Difference(
                    f"contract policy of {contract} changed",
                    ("contract", contract),
                    copy.deepcopy(found["contracts"].get(contract)),
                )
            )
    if author["seed"] != found["seed"]:
        out.append(Difference(f"seed {found['seed']}", ("seed",), found["seed"]))
    return out


def applied(author: cases.Case, chosen: Sequence[Difference]) -> cases.Case:
    """``author`` with only the ``chosen`` differences. The result may not be a valid case."""
    out = copy.deepcopy(author)
    drafts: list[dict[str, Any] | None] = [
        {**s, "length": s["end_us"] - s["start_us"]} for s in out["speeches"]
    ]
    added: list[dict[str, Any]] = []
    delays = {(d["of"], d["nth"]): d["us"] for d in out["delays"]}
    faults = {f["nth"]: (f["of"], f["us"]) for f in out["faults"]}
    for difference in chosen:
        target, to = difference.target, difference.value
        if target[0] == "added":
            added.append(copy.deepcopy(to))
        elif target[0] == "speech" and target[2] == "dropped":
            drafts[target[1]] = None
        elif target[0] == "speech":
            draft = drafts[target[1]]
            if draft is not None:
                draft[{"start": "start_us"}.get(target[2], target[2])] = copy.deepcopy(to)
        elif target[0] == "hangup":
            out["hangup_us"] = to
        elif target[0] == "delay":
            delays.pop(target[1:], None) if to is None else delays.__setitem__(target[1:], to)
        elif target[0] == "fault":
            faults.pop(target[1], None) if to is None else faults.__setitem__(target[1], tuple(to))
        elif target[0] == "setting":
            out["settings"].pop(target[1], None) if to is None else out["settings"].__setitem__(
                target[1], to
            )
        elif target[0] == "contract":
            out["contracts"].pop(target[1], None) if to is None else out["contracts"].__setitem__(
                target[1], copy.deepcopy(to)
            )
        elif target[0] == "seed":
            out["seed"] = to
    speeches = list(added)
    for draft in drafts:
        if draft is not None:
            length = draft.pop("length")
            speeches.append({**draft, "end_us": draft["start_us"] + length})
    out["speeches"] = sorted(speeches, key=lambda s: s["start_us"])
    out["delays"] = [{"of": of, "nth": nth, "us": us} for (of, nth), us in sorted(delays.items())]
    out["faults"] = sorted(
        ({"of": of, "nth": nth, "us": us} for nth, (of, us) in faults.items()),
        key=lambda f: (f["of"], f["nth"]),
    )
    return out


def nearest(author: cases.Case, found: Sequence[cases.Case]) -> cases.Case:
    """The failing case nearest ``author``: fewest differences, then least time moved."""

    def distance(case: cases.Case) -> tuple[int, int]:
        moved = sum(
            abs(d.value - cases.value(author, Variable("speech.position", d.target[1])))
            for d in differences(author, case)
            if d.target[0] == "speech" and d.target[2] == "start"
        )
        return len(differences(author, case)), moved

    return min(found, key=distance)


def settled(found: Window, at: int) -> int | None:
    """Where to leave a variable that is now ``at``: the roundest value in the middle half
    of its window, nearest the centre, or None for a window with no width. An end the search
    gave up on is no edge: the value then comes from within a second of the other, or of ``at``."""
    if found.width_us <= 0:
        return None
    lo_edge, hi_edge = found.lo_end != "reach", found.hi_end != "reach"
    if lo_edge and hi_edge:
        quarter = found.width_us // 4
        lo, hi, aim = found.lo_us + quarter, found.hi_us - quarter, (found.lo_us + found.hi_us) // 2
    elif lo_edge:
        lo = aim = min(found.lo_us + MARGIN_US, found.hi_us)
        hi = min(lo + 1_000_000, found.hi_us)
    elif hi_edge:
        hi = aim = max(found.hi_us - MARGIN_US, found.lo_us)
        lo = max(hi - 1_000_000, found.lo_us)
    else:
        lo, hi, aim = max(at - 500_000, found.lo_us), min(at + 500_000, found.hi_us), at
    for grid in GRIDS_US:
        first = -(-lo // grid) * grid
        if first <= hi:
            below = aim // grid * grid
            last = hi // grid * grid
            return min(
                (p for p in (below, below + grid, first, last) if lo <= p <= hi),
                key=lambda p: (abs(p - aim), p),
            )
    return None


def _up_to(found: Window, most: int) -> Window | None:
    # The cut is no edge of the failure, so it is marked as an end the search gave up on.
    if found.hi_us <= most:
        return found
    return replace(found, hi_us=most, hi_end="reach") if found.lo_us <= most else None


def _moved_times(author: cases.Case | None, case: cases.Case) -> list[Variable]:
    out: list[Variable] = []
    pairs = _pairs(author, case) if author else [(None, j) for j in range(len(case["speeches"]))]
    for i, j in pairs:
        if j is None:
            continue
        theirs = case["speeches"][j]
        ours = author["speeches"][i] if author is not None and i is not None else None
        if ours is None or theirs["start_us"] != ours["start_us"]:
            out.append(Variable("speech.position", j))
        if (
            ours is None
            or theirs["end_us"] - theirs["start_us"] != ours["end_us"] - ours["start_us"]
        ):
            out.append(Variable("speech.end", j))
    declared = {(d["of"], d["nth"]): d["us"] for d in (author["delays"] if author else [])}
    out += [
        Variable(d["of"], d["nth"])
        for d in case["delays"]
        if declared.get((d["of"], d["nth"])) != d["us"]
    ]
    if case["hangup_us"] is not None and (
        author is None or author["hangup_us"] != case["hangup_us"]
    ):
        out.append(Variable(cases.HANGUP, 0))
    waits = {f["nth"]: f["us"] for f in (author["faults"] if author else [])}
    out += [
        Variable(cases.WAIT, f["nth"])
        for f in case["faults"]
        if f["us"] is not None and waits.get(f["nth"]) != f["us"]
    ]
    return out


@dataclass
class Shrunk:
    case: cases.Case
    steps: list[str] = field(default_factory=list)  # what was done, in order
    runs: int = 0  # how many times the system was run
    complete: bool = True  # false: the run budget was spent before nothing more could go
    differences: list[str] = field(default_factory=list)  # from the author's scenario
    windows: list[Window] = field(default_factory=list)  # how far each time can move


def shrink(
    case: cases.Case,
    run: Run | Runner,
    predicate: Predicate,
    *,
    author: cases.Case | None = None,
    max_runs: int = 600,
    delay_limit_us: int = DELAY_LIMIT_US,
) -> Shrunk:
    """Reduce ``case`` towards ``author``, the scenario as written, while it still breaks
    ``predicate``. Once ``max_runs`` is spent, returns what it has with ``complete`` false.
    Raises ``ValueError`` if ``case`` does not break ``predicate`` to begin with."""
    runner = run if isinstance(run, Runner) else Runner(run)
    start = runner.runs
    if cases.validate(case) or not runner.breaks(case, predicate):
        raise ValueError(f"the case does not break {predicate}; there is nothing to reduce")
    current = copy.deepcopy(case)
    steps: list[str] = []

    def spent() -> bool:
        return runner.runs - start >= max_runs

    def breaks(candidate: cases.Case) -> bool:
        return not cases.validate(candidate) and not spent() and runner.breaks(candidate, predicate)

    if author is not None:
        all_of_them = differences(author, current)
        if all_of_them and breaks(applied(author, all_of_them)):
            needed = ddmin(all_of_them, lambda chosen: breaks(applied(author, chosen)))
            if len(needed) < len(all_of_them):
                current = applied(author, needed)
                steps.append(
                    f"kept {len(needed)} of {len(all_of_them)} differences "
                    "from the author's scenario"
                )

    # Removing one kind of thing can make another removable, so the passes repeat until a
    # whole round removes nothing.
    removed = True
    while removed and not spent():
        removed = False
        if current["hangup_us"] is not None and breaks(cases.without_hangup(current)):
            current = cases.without_hangup(current)
            steps.append("removed the hangup")
            removed = True
        for name, plural in (
            ("speeches", "speeches"),
            ("delays", "declared delays"),
            ("faults", "faults"),
        ):
            items = current[name]
            if items and not spent():

                def fails(
                    keep: list[int], whole: dict[str, Any] = current, name: str = name
                ) -> bool:
                    return breaks(_keeping(whole, name, keep))

                kept = ddmin(range(len(items)), fails)
                if len(kept) < len(items):
                    current = _keeping(current, name, kept)
                    steps.append(f"removed {len(items) - len(kept)} of {len(items)} {plural}")
                    removed = True

    def measured(variable: Variable) -> Window | None:
        """None if the budget ran out first: a window half measured is no measurement."""
        try:
            return window(runner, current, variable, predicate, spent=spent)
        except Spent:
            return None

    windows: list[Window] = []
    for variable in _moved_times(author, current):
        found = measured(variable)
        if found is None:
            break
        # A time is moved only if the failure has an edge along it.
        depends = "passes" in (found.lo_end, found.hi_end)
        at = cases.value(current, variable) or 0
        # A delay is settled below the limit, even where the failure holds far beyond it.
        waits = variable.kind in (*cases.DELAY_KINDS, cases.WAIT)
        inside = _up_to(found, max(delay_limit_us, at)) if waits else found
        to = settled(inside, at) if depends and inside is not None else None
        if to is not None and to != cases.value(current, variable):
            candidate = cases.with_value(current, variable, to)
            if candidate is not None and breaks(candidate):
                current = candidate
                steps.append(f"{variable}: settled at {to} us, inside {found.lo_us}..{found.hi_us}")
        windows.append(found)
    # Speeches left where the author put them are measured too, for the record.
    for index in range(len(current["speeches"])):
        variable = Variable("speech.position", index)
        found = None if any(w.variable == variable for w in windows) else measured(variable)
        if found is not None:
            windows.append(found)

    remaining = [d.label for d in differences(author, current)] if author is not None else []
    return Shrunk(current, steps, runner.runs - start, not spent(), remaining, windows)


__all__ = [
    "GRIDS_US",
    "MARGIN_US",
    "Difference",
    "Shrunk",
    "applied",
    "ddmin",
    "differences",
    "nearest",
    "settled",
    "shrink",
]
