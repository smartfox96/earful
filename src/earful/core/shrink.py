"""The shrinker: reduce a failing case to the smallest one that fails the same way, and
set it where it will keep failing.

A case found by a search is the scenario its author wrote, changed. The shrinker asks
which of the changes the failure needs, and after every step runs the real system again:
a step is kept only if the run still breaks the same rule of the same contract.

1. **Isolate.** List every difference between the found case and the author's scenario:
   a speech moved, lengthened, added or dropped; a hangup; a delay declared; a fault
   added; a setting, a contract policy or the seed changed. Delta debugging (Zeller and
   Hildebrandt's *ddmin*) over that list gives a smallest set of differences that still
   fails. The result is the author's scenario with only those changes, and if the
   author's scenario fails as written it is the author's scenario.
2. **Remove.** ddmin again, over the speeches, the declared delays and the faults that
   remain, the author's own included: the fewest that still fail. Then the hangup, if
   the case still fails with a user who stays.
3. **Settle.** For each time that still differs from the author's, measure the window in
   which the failure holds. If the failure has an edge along that time, put the time on
   the roundest value in the middle half of the window. A reduced case that sat on the
   edge of its window would stop failing at the smallest change to the system or to
   Earful, for no reason anyone could read from it.

The result is 1-minimal: undoing any one remaining difference, or removing any one
remaining speech or declared delay, makes it pass.

Two things a reduction cannot promise. It cannot reduce the scenario's code: the agent
and the model policy are not data. And it follows a rule, not a mechanism: if the same
rule can be broken two ways, a reduction may end at the other one. The findings of the
case as found and as reduced are kept side by side so that this can be seen.

Framework-independent: this module must never import livekit.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

from . import case as cases
from .case import Variable
from .predicate import Predicate
from .search import Run, Runner, Window, window

T = TypeVar("T")

# From the roundest time to the finest the shrinker will settle for, in microseconds.
GRIDS_US = (1_000_000, 500_000, 100_000, 50_000, 10_000, 1_000)
# How far from the one known edge of a half-open window a settled time stays: one VAD tick.
MARGIN_US = 50_000


def _keeping(case: cases.Case, name: str, keep: Sequence[int]) -> cases.Case:
    """A copy of ``case`` with only the items of its list ``name`` at the positions ``keep``."""
    return {**copy.deepcopy(case), name: [copy.deepcopy(case[name][i]) for i in keep]}


def ddmin(items: Sequence[T], fails: Callable[[list[T]], bool]) -> list[T]:
    """A smallest sub-list of ``items``, in order, for which ``fails`` is true, such that
    removing any single item makes it false (1-minimal). ``fails(list(items))`` must be
    true. This is Zeller and Hildebrandt's ddmin, which takes for granted that nothing at
    all does not fail; here that is tried first, and if it fails the answer is nothing."""
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


# --------------------------------------------------------------------------- differences


@dataclass(frozen=True)
class Difference:
    """One way a case differs from the scenario its author wrote."""

    label: str  # for a person: "speech 'Stop.' moved to 3140000 us"
    target: tuple[Any, ...]  # what it changes: ("speech", n, "start"), ("setting", name), ...
    value: Any = None  # what it changes it to


def _pairs(author: cases.Case, found: cases.Case) -> list[tuple[int | None, int | None]]:
    """Which speech of ``found`` is which speech of ``author``: in order, the first not
    yet claimed with the same words. ``(author index, found index)``, either side None for
    a speech the other does not have."""
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
    """Every difference between ``found`` and ``author``, each small enough to be undone
    on its own. Applying all of them to ``author`` gives ``found`` again."""
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
    """``author`` with the ``chosen`` differences and no others. The result may not be a
    valid case: two speeches moved without a third may overlap it."""
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
    """Of several failing cases, the one to reduce: the one that differs from the
    author's scenario in the fewest ways, and among those by the least time. A reduction
    can only undo differences, so it ends nearest the author's scenario if it starts
    there."""

    def distance(case: cases.Case) -> tuple[int, int]:
        moved = sum(
            abs(d.value - cases.value(author, Variable("speech.position", d.target[1])))
            for d in differences(author, case)
            if d.target[0] == "speech" and d.target[2] == "start"
        )
        return len(differences(author, case)), moved

    return min(found, key=distance)


# --------------------------------------------------------------------------- settling


def settled(found: Window, at: int) -> int | None:
    """The value to leave a variable at, given the window it fails in and where it is
    now: the roundest value in the middle half of the window, nearest its centre. None if
    the window has no inside to speak of.

    An end the search gave up on is no edge. With one such end the value is taken from
    the second just inside the other; with two, from the second around where it is now.
    """
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


def _moved_times(author: cases.Case | None, case: cases.Case) -> list[Variable]:
    """The times of ``case`` that are not the author's: what a reduction settles."""
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
    case: cases.Case  # the reduced case
    steps: list[str] = field(default_factory=list)  # what was done, in order
    runs: int = 0  # how many times the system was run
    complete: bool = True  # false: the run budget was spent before nothing more could go
    differences: list[str] = field(
        default_factory=list
    )  # how the reduced case differs from the author's
    windows: list[Window] = field(
        default_factory=list
    )  # how far each speech and each settled time can move


def shrink(
    case: cases.Case,
    run: Run | Runner,
    predicate: Predicate,
    *,
    author: cases.Case | None = None,
    max_runs: int = 600,
) -> Shrunk:
    """Reduce ``case`` while running it still breaks ``predicate``.

    ``author`` is the scenario as its author wrote it: what the reduction measures the
    case against. Without it differences cannot be isolated; speeches, delays, faults and
    the hangup are removed and every time is settled. ``max_runs`` bounds the work; if it
    is spent, the case reduced so far is returned and ``complete`` is false.

    Raises ``ValueError`` if ``case`` does not break ``predicate`` to begin with.
    """
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

    # 1. Isolate: the fewest differences from the author's scenario that still fail.
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

    # 2. Remove: the fewest speeches, declared delays and faults, the author's own included.
    for name, plural in (
        ("speeches", "speeches"),
        ("delays", "declared delays"),
        ("faults", "faults"),
    ):
        items = current[name]
        if items and not spent():

            def fails(keep: list[int], whole: dict[str, Any] = current, name: str = name) -> bool:
                return breaks(_keeping(whole, name, keep))

            kept = ddmin(range(len(items)), fails)
            if len(kept) < len(items):
                current = _keeping(current, name, kept)
                steps.append(f"removed {len(items) - len(kept)} of {len(items)} {plural}")

    if current["hangup_us"] is not None and not spent() and breaks(cases.without_hangup(current)):
        current = cases.without_hangup(current)
        steps.append("removed the hangup")

    # 3. Settle: each time that is not the author's, on a round value in the middle of the
    # window in which the failure holds.
    windows: list[Window] = []
    for variable in _moved_times(author, current):
        if spent():
            break
        found = window(runner, current, variable, predicate)
        # A time is moved only if the failure has an edge along it. Where it holds as far
        # as the scenario can go, the failure does not depend on that time.
        depends = "passes" in (found.lo_end, found.hi_end)
        to = settled(found, cases.value(current, variable) or 0) if depends else None
        if to is not None and to != cases.value(current, variable):
            candidate = cases.with_value(current, variable, to)
            if candidate is not None and breaks(candidate):
                current = candidate
                steps.append(f"{variable}: settled at {to} us, inside {found.lo_us}..{found.hi_us}")
        windows.append(found)
    # How far each speech that was left where the author put it can move: nothing to
    # settle, but worth knowing of a case that is going to be kept.
    for index in range(len(current["speeches"])):
        variable = Variable("speech.position", index)
        if spent() or any(w.variable == variable for w in windows):
            continue
        windows.append(window(runner, current, variable, predicate))

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
