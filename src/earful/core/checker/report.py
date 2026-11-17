"""What the checker returns: verdicts, per-contract results and the report for a run.

Framework-independent: this module must never import livekit.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, replace
from typing import Any

CONTRACTS = "earful.contracts/1"
REPORT = "earful.report/1"

PASS, FAIL, UNOBSERVABLE, NOT_EXERCISED = "pass", "fail", "unobservable", "not_exercised"
_SEVERITY = {PASS: 0, UNOBSERVABLE: 1, FAIL: 2}  # a verdict's outcomes, from best to worst
SPLIT = "reading_split"  # the flag on a verdict that another reading makes worse

Event = dict[str, Any]


class ContractFailure(AssertionError):
    """A contract rule broke. What ``assert_contracts`` raises, so a test that calls it
    fails as a test fails on any assertion."""


def allowed(allow: Iterable[Any]) -> set[str]:
    """The rules a caller lets break, each as ``CONTRACT/RULE`` text."""
    return {str(rule) for rule in allow}


@dataclass(frozen=True)
class Finding:
    """One reason a verdict is not a plain pass: a broken rule, or missing evidence."""

    rule: str  # the rule as docs/contracts.md names it, e.g. "rule d" or "unobservable"
    detail: str
    seqs: tuple[int, ...] = ()


@dataclass(frozen=True)
class Verdict:
    """The outcome of one contract for one instance."""

    contract: str
    instance: dict[str, Any]  # what was judged, e.g. {"intent": "book-table-1"}
    outcome: str  # pass, fail or unobservable
    summary: str
    findings: tuple[Finding, ...] = ()
    evidence: tuple[int, ...] = ()  # seq of every event the verdict rests on
    witnesses: tuple[str, ...] = ()  # the sources of those events
    measures: dict[str, Any] = field(default_factory=dict)
    flags: tuple[str, ...] = ()  # qualifications, e.g. "reference_in_doubt"


@dataclass(frozen=True)
class ContractResult:
    contract: str
    outcome: str  # pass, fail, unobservable or not_exercised
    policy: dict[str, Any]  # the policy values the verdicts were reached with
    verdicts: tuple[Verdict, ...] = ()
    notes: tuple[str, ...] = ()  # things worth knowing that are not verdicts

    @property
    def failures(self) -> tuple[Verdict, ...]:
        return tuple(v for v in self.verdicts if v.outcome == FAIL)


@dataclass(frozen=True)
class Report:
    run: str | None
    valid: bool
    invalid: tuple[str, ...]  # why the run yields no verdicts; empty when valid
    results: dict[str, ContractResult]
    # Which reply each ambiguously attributed text was read as (utterance -> model call),
    # and how many such readings the ledger allows. Empty and 1 when nothing is ambiguous.
    reading: dict[int, int] = field(default_factory=dict)
    readings: int = 1
    schema: str = REPORT
    contracts: str = CONTRACTS

    def outcome(self, contract: str) -> str:
        return self.results[contract].outcome

    def outcomes(self) -> dict[str, str]:
        return {name: result.outcome for name, result in self.results.items()}

    @property
    def failures(self) -> tuple[Verdict, ...]:
        return tuple(v for r in self.results.values() for v in r.failures)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    def broken(self) -> list[str]:
        """Every rule a failed verdict names, as ``CONTRACT/RULE``, sorted."""
        return sorted(
            {f"{v.contract}/{finding.rule}" for v in self.failures for finding in v.findings}
        )

    def lines(self, *, measures: bool = False, passes: bool = False) -> list[str]:
        """The report as text, one line per contract and one per verdict that is not a
        plain pass: a failure, an unobservable one, or a pass that carries a flag.
        ``passes`` lists every passing verdict too, and ``measures`` adds what each
        verdict measured."""
        if not self.valid:
            return [f"invalid: {reason}" for reason in self.invalid]
        out: list[str] = []
        for name, result in self.results.items():
            out.append(f"{name}  {result.outcome}")
            for v in result.verdicts:
                if v.outcome != PASS or v.flags or passes or measures:
                    out.append(f"    {v.outcome}: {json.dumps(v.instance)} {v.summary}")
                out += [f"        {f.rule}: {f.detail}" for f in v.findings]
                if measures and v.measures:
                    out.append(f"        {json.dumps(v.measures)}")
                if v.flags:
                    out.append(f"        flags: {', '.join(v.flags)}")
            out += [f"    note: {note}" for note in result.notes]
        return out

    def assert_contracts(self, *, allow: Iterable[Any] = (), what: str = "the run") -> None:
        """Raise ``ContractFailure`` if the run is not valid evidence or a contract rule
        broke. ``allow`` names rules that may break, as ``CONTRACT/RULE``: a known defect
        of a release under test. ``what`` names the run in the message."""
        __tracebackhide__ = True  # pytest then shows the test's own line, not this one
        if not self.valid:
            raise ContractFailure(
                f"{what} is not valid evidence, so no contract was checked:\n"
                + "\n".join(f"  {reason}" for reason in self.invalid)
            )
        broken = [rule for rule in self.broken() if rule not in allowed(allow)]
        if broken:
            raise ContractFailure(
                f"{what} breaks {', '.join(broken)}\n\n" + "\n".join(self.lines())
            )


def verdict(
    contract: str,
    instance: dict[str, Any],
    summary: str,
    *,
    events: Iterable[Event | None] = (),
    findings: Iterable[Finding] = (),
    unobservable: str | None = None,
    measures: dict[str, Any] | None = None,
    flags: Iterable[str] = (),
) -> Verdict:
    """Build a verdict. It fails if any finding names a rule; it is unobservable if
    ``unobservable`` says what evidence is missing; otherwise it passes."""
    used = sorted({e["seq"]: e for e in events if e is not None}.values(), key=lambda e: e["seq"])
    findings = list(findings)
    if findings:
        outcome = FAIL
    elif unobservable is not None:
        outcome = UNOBSERVABLE
        findings = [Finding("unobservable", unobservable)]
    else:
        outcome = PASS
    return Verdict(
        contract=contract,
        instance=instance,
        outcome=outcome,
        summary=summary,
        findings=tuple(findings),
        evidence=tuple(e["seq"] for e in used),
        witnesses=tuple(sorted({e["source"] for e in used})),
        measures=measures or {},
        flags=tuple(flags),
    )


def result(
    contract: str, policy: Any, verdicts: list[Verdict], notes: Iterable[str] = ()
) -> ContractResult:
    """A contract fails if any instance failed, is otherwise unobservable if any instance
    was, otherwise passes; with no instances it was not exercised, which is not a pass."""
    outcomes = {v.outcome for v in verdicts}
    if not verdicts:
        outcome = NOT_EXERCISED
    elif FAIL in outcomes:
        outcome = FAIL
    elif UNOBSERVABLE in outcomes:
        outcome = UNOBSERVABLE
    else:
        outcome = PASS
    return ContractResult(contract, outcome, asdict(policy), tuple(verdicts), tuple(notes))


def split(
    reported: dict[str, ContractResult], checked: list[dict[str, ContractResult]]
) -> dict[str, ContractResult]:
    """``reported`` with what the other readings of the same ledger say against it.

    ``checked`` holds the results under every reading. A verdict is compared with the
    verdict another reading gives the same contract and instance, and is flagged
    ``reading_split`` if that one is worse. Worse is a failure where this one is a pass
    or unobservable, and unobservable where this one is a pass.

    A reading can also judge an instance the reported one has no verdict for. There is
    then no verdict to flag, so if that instance does not pass there, a note on its
    contract says so.
    """

    def marked(v: Verdict) -> Verdict:
        worse = any(
            other.instance == v.instance and _SEVERITY[other.outcome] > _SEVERITY[v.outcome]
            for results in checked
            for other in results[v.contract].verdicts
        )
        return replace(v, flags=(*v.flags, SPLIT)) if worse else v

    def unseen(r: ContractResult) -> list[str]:
        judged = [v.instance for v in r.verdicts]
        notes: list[str] = []
        for results in checked:
            for other in results[r.contract].verdicts:
                if other.outcome == PASS or other.instance in judged:
                    continue
                what = ", ".join(f"{key} {value}" for key, value in sorted(other.instance.items()))
                note = f"another reading judges {what}, which this one does not: {other.outcome}"
                if note not in notes:
                    notes.append(note)
        return notes

    return {
        name: replace(
            r, verdicts=tuple(marked(v) for v in r.verdicts), notes=(*r.notes, *unseen(r))
        )
        for name, r in reported.items()
    }
