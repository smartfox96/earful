"""What the checker returns: verdicts, per-contract results and the report for a run."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..predicate import Predicate

CONTRACTS = "earful.contracts/1"
REPORT = "earful.report/1"

PASS, FAIL, UNOBSERVABLE, NOT_EXERCISED = "pass", "fail", "unobservable", "not_exercised"
_SEVERITY = {PASS: 0, UNOBSERVABLE: 1, FAIL: 2}  # a verdict's outcomes, from best to worst
SPLIT = "reading_split"  # the flag on a verdict that another reading makes worse

Event = dict[str, Any]


# Contract -> the rules a failed verdict of it can name (docs/contracts.md, section 6).
RULES: dict[str, tuple[str, ...]] = {
    "A1": ("rule", "in-flight"),
    "A2": ("rule",),
    "A3": ("rule",),
    "M1": ("rule 2", "rule 3", "rule 4", "rule 5"),
    "M2": ("rule a", "rule b", "rule c", "rule d", "rule e", "rule f"),
    "L1": ("milestone 1", "milestone 2", "milestone 3"),
    "L2": ("rule a", "rule b"),
}
# Contract -> what a run holds when the contract has an instance to judge.
_EXERCISED_BY = {
    "A1": "speech that retires an intent and whose transcript was delivered",
    "A2": "write request for a declared intent",
    "A3": "claim whose last word was played",
    "M1": "reply or spoken text whose playout finished or was abandoned",
    "M2": "model call",
    "L1": "delivered speech that expects a reply",
    "L2": "speech the VAD reported that is neither noise nor a backchannel",
}


class ContractFailure(AssertionError):
    """A contract rule broke; raised by ``assert_contracts``."""


def rule_names() -> list[str]:
    """Every rule that can break, as ``CONTRACT/RULE``."""
    return [f"{contract}/{rule}" for contract, rules in RULES.items() for rule in rules]


def parse_rule(text: str) -> tuple[str, str]:
    """The contract and the rule ``CONTRACT/RULE`` names. Raises ``ValueError`` if none has it."""
    contract, _, name = text.partition("/")
    if name not in RULES.get(contract, ()):
        raise ValueError(
            f"no contract rule {text!r}. A rule is CONTRACT/RULE, one of: {', '.join(rule_names())}"
        )
    return contract, name


def allowed(allow: Iterable[Predicate | str] | str) -> set[str]:
    """The rules a caller lets break, each as ``CONTRACT/RULE`` text.
    Raises ``ValueError`` for a rule that does not exist."""
    names = [allow] if isinstance(allow, str) else [str(one) for one in allow]
    return {"/".join(parse_rule(name)) for name in names}


def required(require: Iterable[str] | str) -> list[str]:
    """The contracts a caller wants a pass or a fail for.
    Raises ``ValueError`` for a contract that does not exist."""
    names = [require] if isinstance(require, str) else list(require)
    unknown = [name for name in names if name not in RULES]
    if unknown:
        raise ValueError(
            f"no contract {', '.join(map(repr, unknown))}. The contracts are: {', '.join(RULES)}"
        )
    return names


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
    witnesses: tuple[str, ...] = ()
    measures: dict[str, Any] = field(default_factory=dict)
    flags: tuple[str, ...] = ()  # qualifications, e.g. "reference_in_doubt"


@dataclass(frozen=True)
class ContractResult:
    contract: str
    outcome: str  # pass, fail, unobservable or not_exercised
    policy: dict[str, Any]
    verdicts: tuple[Verdict, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def failures(self) -> tuple[Verdict, ...]:
        return tuple(v for v in self.verdicts if v.outcome == FAIL)


@dataclass(frozen=True)
class Report:
    run: str | None
    valid: bool
    invalid: tuple[str, ...]  # why the run yields no verdicts; empty when valid
    results: dict[str, ContractResult]
    # utterance -> model call for each ambiguous text, and how many such readings exist.
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
        """The report as text: one line per contract and per verdict that is not a plain pass."""
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

    def unjudged(self, require: Iterable[str] | str) -> dict[str, str]:
        """Each contract in ``require`` that has no pass or fail, and why. Empty for an
        invalid run, which has no results."""
        out = {}
        for contract in required(require):
            result = self.results.get(contract)
            if result is not None and result.outcome == NOT_EXERCISED:
                out[contract] = f"was not exercised: the run has no {_EXERCISED_BY[contract]}"
            elif result is not None and result.outcome == UNOBSERVABLE:
                why = [f.detail for v in result.verdicts for f in v.findings]
                out[contract] = f"is unobservable: {'; '.join(dict.fromkeys(why))}"
        return out

    def assert_contracts(
        self,
        *,
        allow: Iterable[Predicate | str] | str = (),
        require: Iterable[str] | str = (),
        what: str = "the run",
    ) -> None:
        """Raise ``ContractFailure`` if the run is invalid, breaks a rule not in ``allow``, or
        gives a contract in ``require`` no pass or fail. Raises ``ValueError`` for a rule or a
        contract that does not exist."""
        __tracebackhide__ = True  # pytest then shows the test's own line, not this one
        allowing, unjudged = allowed(allow), self.unjudged(require)
        if not self.valid:
            raise ContractFailure(
                f"{what} is not valid evidence, so no contract was checked:\n"
                + "\n".join(f"  {reason}" for reason in self.invalid)
            )
        broken = [rule for rule in self.broken() if rule not in allowing]
        if broken or unjudged:
            said = [f"breaks {', '.join(broken)}"] if broken else []
            if unjudged:
                said.append(f"has no pass or fail for the required {', '.join(unjudged)}")
            parts = [
                f"{what} {' and '.join(said)}",
                "\n".join(f"{contract} {why}" for contract, why in unjudged.items()),
                "\n".join(self.lines()),
            ]
            raise ContractFailure("\n\n".join(part for part in parts if part))


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
    """Build a verdict: fail on any finding, unobservable if ``unobservable`` is set, else pass."""
    used = sorted({e["seq"]: e for e in events if e is not None}.values(), key=lambda e: e["seq"])
    findings = list(findings)
    unknown = [finding.rule for finding in findings if finding.rule not in RULES[contract]]
    if unknown:
        raise ValueError(f"{contract} has no rule {unknown[0]!r}; its rules are {RULES[contract]}")
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
    """A contract's outcome is its worst verdict; with no verdicts it was not exercised."""
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
    """Flag each verdict of ``reported`` that another reading in ``checked`` makes worse."""

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
