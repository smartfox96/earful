"""What the checker returns: verdicts, per-contract results and the report for a run."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Any

from ..outcome import decide
from .contracts import DECLARED

if TYPE_CHECKING:
    from ..predicate import Predicate

CONTRACTS = "earful.contracts/2"
REPORT = "earful.report/2"

PASS, FAIL, UNOBSERVABLE, NOT_EXERCISED = "pass", "fail", "unobservable", "not_exercised"
_SEVERITY = {NOT_EXERCISED: 0, PASS: 0, UNOBSERVABLE: 1, FAIL: 2}  # from best to worst
SPLIT = "reading_split"  # the flag on a verdict that another reading makes worse

Event = dict[str, Any]


RULES: dict[str, tuple[str, ...]] = {name: c.rules for name, c in DECLARED.items()}
NOTHING, LOST = "nothing to judge", "evidence lost"
# Why a verdict is missing -> its kind. With nothing to judge the verdict is not exercised;
# with evidence lost it is unobservable, and the run fails unless the code is allowed.
CODES: dict[str, str] = {
    "run-ended": NOTHING,
    "no-memory-after": NOTHING,
    "no-origin": NOTHING,
    "no-reply-under-way": NOTHING,
    "no-interruption-point": NOTHING,
    "outside-backend": LOST,
    "audio-unattributed": LOST,
    "readings-over-cap": LOST,
    "word-start-unknown": LOST,
    "memory-unaligned": LOST,
    "history-before-playout": LOST,
    "no-stable-text": LOST,
    "tool-unwitnessed": LOST,
}
MISSING: dict[str, tuple[str, ...]] = {name: c.codes for name, c in DECLARED.items()}


class ContractFailure(AssertionError):
    """A contract rule broke; raised by ``assert_contracts``."""


def rule_names() -> list[str]:
    """Every rule that can break, as ``CONTRACT/RULE``."""
    return [f"{contract}/{rule}" for contract, rules in RULES.items() for rule in rules]


def lost_names() -> list[str]:
    """Every code of evidence lost, as ``CONTRACT/CODE``. A run fails on one as on a rule."""
    return [
        f"{contract}/{code}"
        for contract, codes in MISSING.items()
        for code in codes
        if CODES[code] == LOST
    ]


def parse_rule(text: str) -> tuple[str, str]:
    """The contract and the rule ``CONTRACT/RULE`` names, or the code of evidence lost
    ``CONTRACT/CODE`` names. Raises ``ValueError`` if no contract has it."""
    contract, _, name = text.partition("/")
    if text not in (*rule_names(), *lost_names()):
        raise ValueError(
            f"no contract rule {text!r}. A rule is CONTRACT/RULE, one of: "
            f"{', '.join(rule_names())}; or evidence lost, CONTRACT/CODE, one of: "
            f"{', '.join(lost_names())}"
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
    """One reason a verdict is not a plain pass: a broken rule, or why it is missing."""

    rule: str  # the rule as docs/contracts.md names it, or "unobservable" or "not exercised"
    detail: str
    seqs: tuple[int, ...] = ()
    code: str | None = None  # why a verdict is missing: a key of ``CODES``

    @property
    def lost(self) -> bool:
        """Whether this is evidence lost, on which a run fails unless the code is allowed."""
        return self.code is not None and CODES[self.code] == LOST


@dataclass(frozen=True)
class Verdict:
    """The outcome of one contract for one instance."""

    contract: str
    instance: dict[str, Any]  # what was judged, e.g. {"intent": "book-table-1"}
    outcome: str  # pass, fail, unobservable (evidence lost) or not_exercised (nothing to judge)
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


def broken_rules(failures: Iterable[Verdict]) -> list[str]:
    return sorted(
        {
            f"{v.contract}/{finding.rule}"
            for v in failures
            for finding in v.findings
            if finding.code is None
        }
    )


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
        """Every failed verdict. An invalid run has none, since it has no verdicts at all:
        read ``valid`` first."""
        return tuple(v for r in self.results.values() for v in r.failures)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    def broken(self) -> list[str]:
        """Every rule a failed verdict names, as ``CONTRACT/RULE``, sorted."""
        return broken_rules(self.failures)

    def lost(self) -> list[str]:
        """Every code of evidence lost a verdict carries, as ``CONTRACT/CODE``, sorted."""
        return sorted(
            {
                f"{v.contract}/{finding.code}"
                for r in self.results.values()
                for v in r.verdicts
                for finding in v.findings
                if finding.lost
            }
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
                out += [
                    f"        {f.rule}{f' ({f.code})' if f.code else ''}: {f.detail}"
                    for f in v.findings
                ]
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
            if result is None or result.outcome not in (NOT_EXERCISED, UNOBSERVABLE):
                continue
            why = [
                f"{f.detail} ({f.code})"
                for v in result.verdicts
                if v.outcome == result.outcome
                for f in v.findings
                if f.code
            ]
            if result.outcome == UNOBSERVABLE:
                out[contract] = f"lost its evidence: {'; '.join(dict.fromkeys(why))}"
            elif why:
                out[contract] = f"had nothing to judge: {'; '.join(dict.fromkeys(why))}"
            else:
                out[contract] = (
                    f"was not exercised: the run has no {DECLARED[contract].exercised_by}"
                )
        return out

    def assert_contracts(
        self,
        *,
        allow: Iterable[Predicate | str] | str = (),
        require: Iterable[str] | str = (),
        what: str = "the run",
    ) -> None:
        """Raise ``ContractFailure`` if the run is invalid, breaks a rule or lost evidence not
        in ``allow``, or gives a contract in ``require`` no pass or fail. Raises ``ValueError``
        for a rule, a code or a contract that does not exist."""
        __tracebackhide__ = True  # pytest then shows the test's own line, not this one
        allowing, unjudged = allowed(allow), self.unjudged(require)
        if not self.valid:
            raise ContractFailure(
                f"{what} is not valid evidence, so no contract was checked:\n"
                + "\n".join(f"  {reason}" for reason in self.invalid)
            )
        decision = decide(self.broken(), self.lost(), bool(unjudged), allowing)
        broken, lost = decision.broken, decision.lost
        if broken or lost or unjudged:
            said = [f"breaks {', '.join(broken)}"] if broken else []
            if lost:
                said.append(f"lost the evidence for {', '.join(lost)}")
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
    missing: Iterable[tuple[str, str]] = (),
    measures: dict[str, Any] | None = None,
    flags: Iterable[str] = (),
) -> Verdict:
    """Build a verdict: fail on any finding; else, for each ``(code, why)`` in ``missing``,
    unobservable if any is evidence lost and not exercised if all are nothing to judge;
    else pass."""
    used = sorted({e["seq"]: e for e in events if e is not None}.values(), key=lambda e: e["seq"])
    findings, missing = list(findings), list(dict.fromkeys(missing))
    unknown = [finding.rule for finding in findings if finding.rule not in RULES[contract]]
    if unknown:
        raise ValueError(f"{contract} has no rule {unknown[0]!r}; its rules are {RULES[contract]}")
    strange = [code for code, _why in missing if code not in MISSING[contract]]
    if strange:
        raise ValueError(
            f"{contract} has no code {strange[0]!r}; its codes are {MISSING[contract]}"
        )
    if findings:
        outcome = FAIL
        findings.extend(
            Finding("unobservable", why, code=code) for code, why in missing if CODES[code] == LOST
        )
    elif missing:
        lost = any(CODES[code] == LOST for code, _why in missing)
        outcome = UNOBSERVABLE if lost else NOT_EXERCISED
        rule = "unobservable" if lost else "not exercised"
        findings = [Finding(rule, why, code=code) for code, why in missing]
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
    """A contract's outcome is its worst verdict: fail, unobservable, pass, then not exercised,
    which it also is with no verdicts."""
    outcomes = {v.outcome for v in verdicts}
    outcome = next((o for o in (FAIL, UNOBSERVABLE, PASS) if o in outcomes), NOT_EXERCISED)
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
