"""A failure bundle (earful.bundle/1): a failing case, its reduction and the ledger of
each, kept so that the failure can be replayed."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from . import case as cases
from .checker import CONTRACTS, FAIL, PASS, check
from .ledger import SCHEMA as LEDGER
from .ledger import ledger_hash, read_jsonl, write_jsonl
from .predicate import Predicate, details_of, failures
from .schema import validate as ledger_errors
from .search import Run, Runner, judged
from .shrink import shrink

SCHEMA = "earful.bundle/1"
SCHEMA_MINOR = 1  # 1 added case_digest
SCHEMA_FILE = "earful.bundle-1.schema.json"
MANIFEST = "manifest.json"
FILES = {
    "original": ("original.case.json", "original.ledger.jsonl"),
    "minimal": ("minimal.case.json", "minimal.ledger.jsonl"),
}
# The formats this Earful writes, as a manifest names them.
FORMATS = {"ledger": LEDGER, "contracts": CONTRACTS, "case": cases.SCHEMA}
# Earlier ones it still reads. A bundle kept under earlier contracts is judged by these.
EARLIER = {"contracts": ("earful.contracts/1",)}

Event = dict[str, Any]


@lru_cache(maxsize=1)
def load_schema() -> dict[str, Any]:
    text = resources.files(__package__).joinpath("schemas").joinpath(SCHEMA_FILE).read_text("utf-8")
    return json.loads(text)


def validate(manifest: Any) -> list[str]:
    """Every way ``manifest`` departs from earful.bundle/1. An empty list means valid."""
    schema = load_schema()
    Draft202012Validator.check_schema(schema)
    return [
        f"{'/'.join(str(p) for p in error.absolute_path) or 'manifest'}: {error.message}"
        for error in sorted(
            Draft202012Validator(schema).iter_errors(manifest),
            key=lambda e: list(map(str, e.absolute_path)),
        )
    ]


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="ascii")


def case_digest(case: cases.Case) -> str:
    """What a manifest records of a case file: SHA-256 of the case as one line of JSON."""
    return hashlib.sha256(cases.canonical(case).encode("ascii")).hexdigest()


def capture(
    directory: Path,
    case: cases.Case,
    run: Run | Runner,
    predicate: Predicate,
    *,
    author: cases.Case | None = None,
    repeats: int = 3,
    max_runs: int = 600,
    environment: dict[str, str] | None = None,
    scenario_digest: str = "",
) -> dict[str, Any]:
    """Reduce a failing ``case``, write the bundle to ``directory`` and return its manifest."""
    if repeats < 1:
        raise ValueError(f"a bundle's case is run again at least once; got repeats={repeats}")
    runner = run if isinstance(run, Runner) else Runner(run)
    with runner.counting("reduce"):
        shrunk = shrink(case, runner, predicate, author=author, max_runs=max_runs)
        found_events = runner.ledger(case)
    not_kept = "a case that does not repeat is not kept as a bundle"
    if found_events is None or ledger_hash(found_events) != runner.outcome(case).hash:
        raise ValueError(
            f"the case as found did not give the ledger that broke {predicate} when it was "
            f"run again; {not_kept}"
        )
    runs = [runner.run(shrunk.case) for _ in range(repeats)]
    reduced = runner.outcome(shrunk.case).hash  # of the run the reduction judged
    same = sum(ledger_hash(events) == reduced for events in runs)
    if same != repeats:
        raise ValueError(
            f"the reduced case gave the ledger that broke {predicate} in {same} of the "
            f"{repeats} runs made after the reduction; {not_kept}"
        )
    minimal_events = runs[0]
    header = minimal_events[0]["data"]
    manifest = {
        "schema": SCHEMA,
        "schema_minor": SCHEMA_MINOR,
        "predicate": {"contract": predicate.contract, "rule": predicate.rule},
        "framework": header["framework"],
        "clock": header["clock"],
        "formats": dict(FORMATS),
        "environment": environment or {},
        "scenario_digest": scenario_digest,
        "original": {
            "hash": ledger_hash(found_events),
            "case_digest": case_digest(case),
            "speeches": len(case["speeches"]),
            "delays": len(case["delays"]),
            "faults": len(case["faults"]),
            "findings": list(details_of(check(found_events), predicate)),
        },
        "minimal": {
            "hash": ledger_hash(minimal_events),
            "case_digest": case_digest(shrunk.case),
            "speeches": len(shrunk.case["speeches"]),
            "delays": len(shrunk.case["delays"]),
            "faults": len(shrunk.case["faults"]),
            "findings": list(details_of(check(minimal_events), predicate)),
        },
        "reduction": {
            "runs": shrunk.runs,
            "complete": shrunk.complete,
            "steps": shrunk.steps,
            "differences": shrunk.differences,
        },
        "windows": [found.to_json() for found in shrunk.windows],
        "replay": {"repeats": repeats, "distinct_hashes": 1},
    }
    directory.mkdir(parents=True, exist_ok=True)
    for name in (MANIFEST, *(f for pair in FILES.values() for f in pair)):
        (directory / name).unlink(missing_ok=True)
    _write_json(directory / FILES["original"][0], case)
    write_jsonl(found_events, directory / FILES["original"][1], normalized=True)
    _write_json(directory / FILES["minimal"][0], shrunk.case)
    write_jsonl(minimal_events, directory / FILES["minimal"][1], normalized=True)
    _write_json(directory / MANIFEST, manifest)
    return manifest


@dataclass
class Bundle:
    """A bundle as read, with every file of it checked against its manifest."""

    directory: Path
    manifest: dict[str, Any]
    predicate: Predicate
    minimal: cases.Case
    original: cases.Case
    ledgers: dict[str, list[Event]]  # by "original" and "minimal"
    unverified: tuple[str, ...] = ()  # the case files the manifest records no digest of

    def ledger(self, which: str = "minimal") -> list[Event]:
        return self.ledgers[which]


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="ascii"))
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc.strerror}") from exc
    except ValueError as exc:  # not JSON, or not ASCII
        raise ValueError(f"{path} is not JSON: {exc}") from exc


def _case(path: Path, recorded: dict[str, Any]) -> cases.Case:
    case = _json(path)
    errors = cases.validate(case)
    if errors:
        raise ValueError(f"{path} is not an {cases.SCHEMA} document: " + "; ".join(errors))
    digest = recorded.get("case_digest")  # absent from a manifest written before minor 1
    if digest is not None and case_digest(case) != digest:
        raise ValueError(
            f"{path} is not the case the manifest records: its digest is "
            f"{case_digest(case)}, and the manifest has {digest}"
        )
    return case


def _ledger(path: Path, recorded: dict[str, Any]) -> list[Event]:
    try:
        events = read_jsonl(path)
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc.strerror}") from exc
    except ValueError as exc:  # not JSON, or not ASCII
        raise ValueError(f"{path} is not a ledger file: {exc}") from exc
    errors = ledger_errors(events)
    if errors:
        more = f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""
        raise ValueError(f"{path} is not an {LEDGER} ledger: {errors[0]}{more}")
    if ledger_hash(events) != recorded["hash"]:
        raise ValueError(
            f"{path} is not the ledger the manifest records: its hash is "
            f"{ledger_hash(events)}, and the manifest has {recorded['hash']}"
        )
    return events


def load(directory: Path) -> Bundle:
    """Read a bundle and check every file of it. A ``ValueError`` names the file refused."""
    path = directory / MANIFEST
    manifest = _json(path)
    errors = validate(manifest)
    if errors:
        raise ValueError(f"{path} is not an {SCHEMA} manifest: " + "; ".join(errors))
    for name, reads in FORMATS.items():
        if manifest["formats"][name] not in (reads, *EARLIER.get(name, ())):
            raise ValueError(
                f"{path} names the {name} format {manifest['formats'][name]}; "
                f"this Earful reads {reads}"
            )
    named = f"{manifest['predicate']['contract']}/{manifest['predicate']['rule']}"
    try:
        predicate = Predicate.parse(named)
    except ValueError:
        raise ValueError(f"{path} names {named}, a rule no contract has") from None
    found = {which: _case(directory / FILES[which][0], manifest[which]) for which in FILES}
    events = {which: _ledger(directory / FILES[which][1], manifest[which]) for which in FILES}
    report = check(events["minimal"])
    if predicate not in failures(report):
        under = f"under {CONTRACTS}"
        if manifest["formats"]["contracts"] != CONTRACTS:
            under += f", and the bundle was kept under {manifest['formats']['contracts']}"
        raise ValueError(
            f"{directory / FILES['minimal'][1]} does not break {predicate} {under}: the "
            "bundle does not show the failure it names"
        )
    return Bundle(
        directory,
        manifest,
        predicate,
        found["minimal"],
        found["original"],
        events,
        tuple(FILES[which][0] for which in FILES if "case_digest" not in manifest[which]),
    )


@dataclass
class Replay:
    reproduced: bool  # every run was judged and broke the bundle's rule
    hashes: list[str]  # the ledger hash of each run
    recorded: str  # the hash the bundle recorded for the reduced case
    framework: dict[str, str]  # what it was replayed on
    scenario_changed: bool | None  # the scenario's code has changed; None if unknown
    outcomes: tuple[str | None, ...]  # the contract's outcome in each run; None if invalid
    invalid: tuple[str, ...] = ()  # why a run was not valid evidence, if one was not
    clean: bool = False  # every run was judged and the bundle's contract passed in each
    unverified: tuple[str, ...] = ()  # the bundle's case files that nothing could check
    others: tuple[str, ...] = ()  # the other rules some run broke, as CONTRACT/RULE
    codes: tuple[str, ...] = ()  # why the contract's verdicts were missing in some run
    broke: int = 0  # how many runs broke the bundle's rule

    loss_judged: bool = False
    other_losses: tuple[str, ...] = ()

    @property
    def judged(self) -> bool:
        """Every run gave a verdict on the contract or observed the loss the bundle names."""
        if self.loss_judged:
            return True
        return all(outcome in (PASS, FAIL) for outcome in self.outcomes)

    @property
    def repeatable(self) -> bool:
        return len(set(self.hashes)) == 1

    @property
    def identical(self) -> bool:
        """The very ledger the bundle recorded: expected only on the same release and code."""
        return self.repeatable and self.hashes[0] == self.recorded


def replay(
    bundle: Bundle, run: Run, *, repeats: int = 3, fingerprint: Callable[[str], str] | None = None
) -> Replay:
    """Run the bundle's reduced case ``repeats`` times. This runs the scenario's code, so
    replay only bundles you trust. ``fingerprint`` digests a scenario's code, to tell whether
    it has changed since capture. Raises ``ValueError`` if the adapter refuses the case."""
    if repeats < 1:
        raise ValueError(f"a replay makes at least one run; got repeats={repeats}")
    recorded = bundle.manifest["scenario_digest"]
    changed = None
    if fingerprint is not None and recorded:
        changed = fingerprint(bundle.minimal["scenario"]) != recorded
    outcomes, framework = [], {}
    for _ in range(repeats):
        events = run(bundle.minimal)
        header = events[0].get("data") if events and isinstance(events[0], dict) else None
        if not isinstance(header, dict) or not isinstance(header.get("framework"), dict):
            raise ValueError("the replay returned a ledger without a framework header")  # noqa: TRY004
        framework = header["framework"]
        outcomes.append(judged(bundle.minimal, events))
    # A run that is not valid evidence has no failures, so it never counts as breaking.
    broke = [bundle.predicate in o.failures for o in outcomes]
    invalid = next((o.invalid for o in outcomes if o.invalid), ())
    contract = [o.outcomes.get(bundle.predicate.contract) for o in outcomes]
    others = {p for o in outcomes for p in o.failures if p != bundle.predicate}
    codes = (code for o in outcomes for code in o.missing.get(bundle.predicate.contract, ()))
    return Replay(
        reproduced=all(broke),
        hashes=[o.hash or "" for o in outcomes],
        recorded=bundle.manifest["minimal"]["hash"],
        framework=framework,
        scenario_changed=changed,
        invalid=invalid,
        clean=all(outcome == PASS for outcome in contract),
        unverified=bundle.unverified,
        outcomes=tuple(contract),
        others=tuple(sorted(str(p) for p in others if not p.lost)),
        other_losses=tuple(sorted(str(p) for p in others if p.lost)),
        codes=tuple(dict.fromkeys(codes)),
        broke=sum(broke),
        loss_judged=bundle.predicate.lost
        and all(matched or verdict in (PASS, FAIL) for matched, verdict in zip(broke, contract)),
    )


__all__ = [
    "EARLIER",
    "FILES",
    "FORMATS",
    "MANIFEST",
    "SCHEMA",
    "Bundle",
    "Replay",
    "capture",
    "case_digest",
    "load",
    "load_schema",
    "replay",
    "validate",
]
