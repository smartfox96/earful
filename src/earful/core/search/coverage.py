"""The cases a search tried and the rules and windows it found."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

from .. import case as cases
from ..case import Variable
from ..checker import ContractFailure, check
from ..checker.report import allowed
from ..outcome import Decision, decide
from ..predicate import Predicate, details_of
from .results import Break, NotJudged, Plan, Tried, Unread
from .runner import Outcome, Runner
from .space import Candidate, Placement
from .windows import Spent, Window, _joined, window


def _seconds(us: int) -> str:
    return f"{us / 1_000_000:.3f} s"


# A contract's outcomes in a case, in the order a search counts them.
_OUTCOMES = ("pass", "fail", "not_exercised", "unobservable")

_ENDS = {
    "passes": "passes beyond it",
    "blocked": "no case exists beyond it",
    "reach": "still breaks where the search stopped",
}


def _end(end: str, unjudged: str | None) -> str:
    return f"a run beyond it could not be judged, {unjudged}" if unjudged else _ENDS[end]


# What a search's runs were for, in the order its report lists them.
_PURPOSES = {
    "read": "to find where to put things",
    "cases": "for the cases",
    "measure": "to measure the failures",
    "examples": "to read an example of each",
    "other": "for other reads",
}


@dataclass
class Exploration:
    case: cases.Case
    original: Outcome  # the case as given
    tried: list[Tried]
    impossible: list[Placement]
    offsets_us: tuple[int, ...]
    runner: Runner
    max_runs: int | None = None  # the budget of runs, if the search was given one
    not_run: int = 0  # cases the budget left untried
    cut: dict[Predicate, int] = field(default_factory=dict)  # values it left unmeasured
    unread: list[Unread] = field(default_factory=list)  # runs that read where to put things

    @property
    def unmeasured(self) -> int:
        """How many breaking values the budget left unmeasured, over every rule measured."""
        return sum(self.cut.values())

    @property
    def complete(self) -> bool:
        """False if the budget was spent before every case was run and every window measured."""
        return not self.not_run and not self.unmeasured

    @property
    def runs(self) -> int:
        """The runs this search made, without the reductions of failures kept from it."""
        return self.runner.runs - self.runner.by_purpose.get("reduce", 0)

    def _spent(self) -> bool:
        return self.max_runs is not None and self.runs >= self.max_runs

    def predicates(self) -> list[Predicate]:
        """Every rule the case as given or some candidate broke."""
        tried = {p for t in self.tried for p in t.outcome.failures}
        return sorted(tried.union(self.original.failures))

    def breaking(self, predicate: Predicate) -> list[Tried]:
        return [t for t in self.tried if predicate in t.outcome.failures]

    def everywhere(self, predicate: Predicate) -> bool:
        """Whether the rule breaks in the case as given and in every candidate that ran."""
        ran = [t for t in self.tried if t.outcome.ran]
        return predicate in self.original.failures and all(
            predicate in t.outcome.failures for t in ran
        )

    def windows(
        self, predicate: Predicate, *, most: int = 3, resolution_us: int = 1_000
    ) -> list[Window]:
        """The measured windows in which the rule breaks: every instant, and at most ``most``
        wider windows per variable and base."""
        return self._measured(predicate, most, resolution_us)[0]

    def _measured(
        self, predicate: Predicate, most: int = 3, resolution_us: int = 1_000
    ) -> tuple[list[Window], int]:
        """The windows, and how many breaking values lie in none: ``most`` was reached, or
        the budget of runs was spent. An instant costs four runs and is measured whatever
        ``most`` is."""
        measured: list[tuple[str, Window]] = []
        wide: dict[tuple[str, Variable], int] = {}
        left_out = cut = 0
        moved = [t for t in self.breaking(predicate) if t.candidate.variable is not None]
        for tried in sorted(
            moved, key=lambda t: (t.candidate.base, t.candidate.variable, t.candidate.value_us)
        ):
            variable, at = tried.candidate.variable, tried.candidate.value_us
            assert variable is not None and at is not None
            key = (tried.candidate.base, variable)
            if any(
                b == tried.candidate.base and w.variable == variable and w.lo_us <= at <= w.hi_us
                for b, w in measured
            ):
                continue
            if wide.get(key, 0) >= most:
                left_out += 1
                continue
            try:
                with self.runner.counting("measure"):
                    found = window(
                        self.runner,
                        tried.candidate.case,
                        variable,
                        predicate,
                        resolution_us=resolution_us,
                        spent=self._spent,
                    )
            except Spent:
                cut += 1
                continue
            # A window is located to its resolution, so a breaking value just past the end of one
            # already measured finds the same failure again: one window, counted once.
            same = next(
                (
                    n
                    for n, (b, w) in enumerate(measured)
                    if b == tried.candidate.base
                    and w.variable == variable
                    and found.lo_us <= w.hi_us
                    and w.lo_us <= found.hi_us
                ),
                None,
            )
            if same is not None:
                measured[same] = (tried.candidate.base, _joined(measured[same][1], found))
                continue
            measured.append((tried.candidate.base, found))
            if not found.instant:
                wide[key] = wide.get(key, 0) + 1
        self.cut[predicate] = cut
        return [found for _base, found in measured], left_out + cut

    def coverage(self) -> dict[str, dict[str, Any]]:
        """For each handle, the kinds of transition it was put beside and those it never was."""
        reached: dict[str, set[str]] = {}
        missed: dict[str, dict[str, set[str]]] = {}

        def note(placement: Placement, why: str | None) -> None:
            name = placement.handle.name
            reached.setdefault(name, set())
            missed.setdefault(name, {})
            for kind in placement.transition.kinds:
                if why is None:
                    reached[name].add(kind)
                else:
                    missed[name].setdefault(kind, set()).add(why)

        for placement in self.impossible:
            note(placement, "impossible")
        for tried in self.tried:
            for placement in tried.candidate.placements:
                note(placement, tried.outcome.why_not)
        return {
            name: {
                "reached": sorted(reached[name]),
                "never_reached": {
                    kind: sorted(why)
                    for kind, why in sorted(missed[name].items())
                    if kind not in reached[name]
                },
            }
            for name in sorted(reached)
        }

    @property
    def nothing_judged(self) -> bool:
        """Whether the search judged no case: it tried none, or could judge none it tried."""
        if not self.tried:
            return not self.not_run  # cases the budget left untried make it incomplete instead
        return not any(t.outcome.ran for t in self.tried)

    @property
    def unreadable(self) -> int:
        """How many runs made to find where to put things raised or were invalid. The cases
        each would have placed were never tried, so no healthy search has one."""
        return sum(one.fails for one in self.unread)

    @property
    def errored(self) -> int:
        """How many of the cases tried raised. No healthy search has one."""
        return sum(t.outcome.error is not None for t in self.tried)

    def by_contract(self) -> dict[str, dict[str, int]]:
        """For each contract, how many of the cases judged it passed, failed, had nothing to
        judge (not exercised) or lost the evidence (unobservable)."""
        out: dict[str, dict[str, int]] = {}
        for tried in self.tried:
            for contract, outcome in tried.outcome.outcomes.items():
                counts = out.setdefault(contract, dict.fromkeys(_OUTCOMES, 0))
                counts[outcome] += 1
        return out

    def not_judged(self) -> list[NotJudged]:
        """Why cases could not be judged, the reason with the most cases first. Reasons that
        differ only in their numbers are one reason."""
        groups: dict[tuple[str, str], list[Tried]] = {}
        for tried in self.tried:
            why = tried.outcome.why_not
            if why is not None:
                alike = re.sub(r"\d+", "0", tried.outcome.reason)
                groups.setdefault((why, alike), []).append(tried)
        out = [
            NotJudged(why, same[0].outcome.reason, len(same), self._placed(same[0].candidate))
            for (why, _alike), same in groups.items()
        ]
        return sorted(out, key=lambda group: (-group.cases, group.why, group.reason))

    def breaks(self, *, windows: bool | Iterable[Predicate] = True) -> list[Break]:
        """Every rule the search found broken, with an example of each. ``windows`` says which
        failures have their width measured, which takes further runs: all, none or those named."""
        wanted = None if isinstance(windows, bool) else set(windows)
        out = []
        for predicate in self.predicates():
            breaking = self.breaking(predicate)
            measure = windows is True or (wanted is not None and predicate in wanted)
            measured, unmeasured = self._measured(predicate) if measure else ([], 0)
            unmoved = predicate in self.original.failures or any(
                t.candidate.variable is None for t in breaking
            )

            def distance(tried: Tried) -> tuple[int, int]:
                variable, to = tried.candidate.variable, tried.candidate.value_us
                if variable is None or to is None:
                    return (1 if tried.candidate.base else 0, 0)
                was = cases.value(self.case, variable)
                moved = abs(to - was) if was is not None else to
                return (2 if tried.candidate.base else 1, moved)

            if predicate in self.original.failures:
                example = self.case
                what = "the original case only" if not breaking else "nothing: it breaks as written"
            else:
                nearest = min(breaking, key=distance)
                example, what = nearest.candidate.case, self._what(nearest.candidate)
            with self.runner.counting("examples"):
                ledger = self.runner.ledger(example)
            details = details_of(check(ledger), predicate) if ledger is not None else ()
            out.append(
                Break(
                    predicate=predicate,
                    candidates=len(breaking),
                    everywhere=self.everywhere(predicate),
                    changed=tuple(sorted({t.candidate.what for t in breaking})),
                    windows=tuple(measured),
                    unmeasured=unmeasured,
                    # A value left unmeasured may lie in a window: the rule is then counted.
                    instants_only=bool(measured)
                    and not unmoved
                    and not unmeasured
                    and all(w.instant for w in measured),
                    example=example,
                    what=what,
                    details=details,
                )
            )
        return out

    def _what(self, candidate: Candidate) -> str:
        if candidate.variable is None or candidate.value_us is None:
            return candidate.base
        was = cases.value(self.case, candidate.variable)
        written = f"as written {_seconds(was)}" if was is not None else "not declared as written"
        moved = f"{candidate.variable} at {_seconds(candidate.value_us)} ({written})"
        return f"{candidate.base} + {moved}" if candidate.base else moved

    def _placed(self, candidate: Candidate) -> str:
        """What a candidate changed and, where a time was moved, what that put beside what."""
        if not candidate.placements:
            return self._what(candidate)
        first = candidate.placements[0]
        offset, beside = first.offset_us, first.transition.kinds[0]
        side = f"{abs(offset) // 1000} ms {'before' if offset < 0 else 'after'}" if offset else "at"
        return f"{self._what(candidate)}, to put {first.handle.moment} {side} {beside}"

    def decision(self, found: list[Break], unjudged: dict[str, str], allow: set[str]) -> Decision:
        predicates = [one.predicate for one in found if not one.instants_only]
        return decide(
            (str(p) for p in predicates if not p.lost),
            (str(p) for p in predicates if p.lost),
            bool(unjudged) or self.no_verdict,
            allow,
        )

    def describe(
        self,
        found: list[Break] | None = None,
        unjudged: dict[str, str] | None = None,
        *,
        allow: set[str] | None = None,
    ) -> str:
        """What the search found, as text for a person. ``unjudged`` is what ``require`` asked
        of the case as given and did not get."""
        found = self.breaks() if found is None else found
        header = (self.runner.ledger(self.case) or [{"data": {}}])[0]["data"]
        framework = header.get("framework", {})
        on = f" on {framework.get('name')} {framework.get('version')}" if framework else ""
        counts = self.summary()
        of = f" of {counts['candidates'] + self.not_run}" if self.not_run else ""
        decision = self.decision(found, unjudged or {}, allow or set())
        lines = [
            (
                f"{header.get('case', 'the case')}{on}: {counts['candidates']}{of} cases tried, "
                f"{counts['ran']} judged, {len(decision.broken)} contract rule(s) broken"
            )
        ]
        if self.original.session_scope:
            lines.append(self.original.session_scope)
        made = self.runner.by_purpose
        purposes = [f"{made[p]} {why}" for p, why in _PURPOSES.items() if made.get(p)]
        lines.append(f"    {self.runs} run(s): {', '.join(purposes)}")
        counted = self.by_contract()
        if counted:
            lines.append("    by contract: pass, fail, nothing to judge, evidence lost")
            for contract, tally in counted.items():
                lines.append(f"    {contract:<4}" + " ".join(f"{n:>5}" for n in tally.values()))
        if not self.complete:
            left = [f"{self.not_run} case(s) not run"] if self.not_run else []
            left += [f"{self.unmeasured} failure(s) not measured"] if self.unmeasured else []
            spent = f"incomplete: the budget of {self.max_runs} runs was spent with "
            lines += ["", f"{spent}{' and '.join(left)}. Nothing is concluded about them"]
        for one in found:
            lines.append("")
            where = (
                f"in {one.candidates} case(s)"
                if one.candidates
                else "in the original case only (0 search candidates)"
            )
            lines.append(
                f"{one.predicate}  {where}"
                + (
                    "  (at single instants only: same-instant order, not counted)"
                    * one.instants_only
                )
            )
            lines += [f"    {detail}" for detail in one.details]
            lines.append(f"    changed: {one.what}")
            others = [c for c in one.changed if c and c != one.what.partition(" at ")[0]]
            if others:
                lines.append(f"    also by changing: {'; '.join(others)}")
            widths = [
                f"    breaks with {w.variable} from {_seconds(w.lo_us)}"
                f" ({_end(w.lo_end, w.lo_unjudged)}) to {_seconds(w.hi_us)}"
                f" ({_end(w.hi_end, w.hi_unjudged)})"
                for w in one.windows
            ]
            lines += list(dict.fromkeys(widths))  # the same window under two faults is one line
            if one.unmeasured:
                more = f"{one.unmeasured} more value(s)"
                lines.append(f"    breaks at {more}, which were not measured")
        skipped = self.not_judged()
        if skipped:
            cut = sum(group.cases for group in skipped)
            result = ""
            if self.nothing_judged:
                result = ", which leaves the search with no result"
            elif self.errored:
                result = f"; the search fails, since {self.errored} raised"
            lines += ["", f"not judged: {cut} of {counts['candidates']} cases{result}"]
            for group in skipped:
                lines.append(f"    {group.cases} {group.why}: {group.reason}")
                lines.append(f"        such as {group.example}")
        elif not self.tried and not self.not_run:
            lines += ["", "no case was tried, which leaves the search with no result"]
        if self.unread:
            fails = "; the search fails" if self.unreadable else ""
            said = (
                f"not read: {len(self.unread)} run(s) made to find where to put things could "
                f"not be judged, so the cases they would place were not tried{fails}"
            )
            lines += ["", said]
            lines += [
                f"    {one.run}: {one.outcome.why_not}: {one.outcome.reason}" for one in self.unread
            ]
        if unjudged:
            lines += ["", "required, with no pass or fail in the case as given:"]
            lines += [f"    {contract} {why}" for contract, why in unjudged.items()]
        return "\n".join(lines)

    def unjudged(self, require: Iterable[str] | str) -> dict[str, str]:
        """Each contract in ``require`` that the case as given has no pass or fail for, and why."""
        ledger = self.runner.ledger(self.case)
        return check(ledger).unjudged(require) if ledger is not None else {}

    def assert_contracts(
        self, *, allow: Iterable[Predicate | str] | str = (), require: Iterable[str] | str = ()
    ) -> None:
        """Raise ``ContractFailure`` if the search found a rule broken, if a case it tried
        raised, if it tried none or could judge none of them, if a run made to find where to
        put things raised or was invalid, if its budget of runs cut it short, or if the case
        as given has no pass or fail for a contract in ``require``. Rules that break only at
        single instants, or are named in ``allow`` as ``CONTRACT/RULE``, are not counted."""
        __tracebackhide__ = True  # pytest then shows the test's own line, not this one
        allowing, unjudged = allowed(allow), self.unjudged(require)
        found = self.breaks()
        if self.decision(found, unjudged, allowing).status:
            raise ContractFailure(self.describe(found, unjudged, allow=allowing))

    @property
    def no_verdict(self) -> bool:
        """Whether the search cannot pass whatever it found: it judged no case, a case raised,
        a run made to find where to put things raised or was invalid, or the budget ran out."""
        return bool(self.nothing_judged or self.errored or self.unreadable or not self.complete)

    def summary(self) -> dict[str, Any]:
        counts = {"ran": 0, "refused": 0, "error": 0, "invalid": 0}
        for tried in self.tried:
            counts[tried.outcome.why_not or "ran"] += 1
        return {
            "candidates": len(self.tried),
            **(
                {"session_scope": self.original.session_scope}
                if self.original.session_scope
                else {}
            ),
            **counts,
            "impossible_placements": len(self.impossible),
            "runs": self.runs,
            "runs_by_purpose": {p: n for p, n in self.runner.by_purpose.items() if p != "reduce"},
            "max_runs": self.max_runs,
            "complete": self.complete,
            "not_run": self.not_run,
            "unmeasured": self.unmeasured,
            "original_breaks": [str(p) for p in self.original.failures],
            "found": {
                str(p): {
                    "candidates": len(self.breaking(p)),
                    "everywhere": self.everywhere(p),
                    "changed": sorted({t.candidate.what for t in self.breaking(p)}),
                }
                for p in self.predicates()
            },
            "errors": sorted({t.outcome.error for t in self.tried if t.outcome.error}),
            "not_judged": [asdict(group) for group in self.not_judged()],
            "by_contract": self.by_contract(),
            "not_read": [
                {"run": one.run, "why": one.outcome.why_not, "reason": one.outcome.reason}
                for one in self.unread
            ],
        }


__all__ = [
    "Break",
    "Exploration",
    "NotJudged",
    "Plan",
    "Tried",
    "Unread",
]
