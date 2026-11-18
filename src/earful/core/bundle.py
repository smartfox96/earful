"""A failure bundle (earful.bundle/1): a failing case, its reduction and the ledger of
each, kept so that the failure can be replayed."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from . import case as cases
from .checker import CONTRACTS, check
from .ledger import SCHEMA as LEDGER
from .ledger import ledger_hash, read_jsonl, write_jsonl
from .predicate import Predicate
from .search import Run, Runner, judged
from .shrink import shrink

SCHEMA = "earful.bundle/1"
SCHEMA_MINOR = 0
SCHEMA_FILE = "earful.bundle-1.schema.json"
MANIFEST = "manifest.json"
FILES = {
    "original": ("original.case.json", "original.ledger.jsonl"),
    "minimal": ("minimal.case.json", "minimal.ledger.jsonl"),
}

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


def _findings(events: list[Event], predicate: Predicate) -> list[str]:
    return [
        finding.detail
        for verdict in check(events).failures
        if verdict.contract == predicate.contract
        for finding in verdict.findings
        if finding.rule == predicate.rule
    ]


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
    """Reduce a failing ``case``, write the bundle to ``directory`` and return its manifest.
    Raises ``ValueError`` if ``case`` does not break ``predicate``, or if the reduced case
    does not give the same ledger in each of ``repeats`` runs."""
    runner = run if isinstance(run, Runner) else Runner(run)
    shrunk = shrink(case, runner, predicate, author=author, max_runs=max_runs)
    found_events = runner.ledger(case)
    if found_events is None:  # shrink has checked that the case breaks the rule
        raise ValueError("the case cannot be judged")
    runs = [runner.run(shrunk.case) for _ in range(repeats)]
    distinct = len({ledger_hash(events) for events in runs})
    if distinct != 1:
        raise ValueError(f"the reduced case gave {distinct} different ledgers in {repeats} runs")
    minimal_events = runs[0]
    header = minimal_events[0]["data"]
    manifest = {
        "schema": SCHEMA,
        "schema_minor": SCHEMA_MINOR,
        "predicate": {"contract": predicate.contract, "rule": predicate.rule},
        "framework": header["framework"],
        "clock": header["clock"],
        "formats": {"ledger": LEDGER, "contracts": CONTRACTS, "case": cases.SCHEMA},
        "environment": environment or {},
        "scenario_digest": scenario_digest,
        "original": {
            "hash": ledger_hash(found_events),
            "speeches": len(case["speeches"]),
            "delays": len(case["delays"]),
            "faults": len(case["faults"]),
            "findings": _findings(found_events, predicate),
        },
        "minimal": {
            "hash": ledger_hash(minimal_events),
            "speeches": len(shrunk.case["speeches"]),
            "delays": len(shrunk.case["delays"]),
            "faults": len(shrunk.case["faults"]),
            "findings": _findings(minimal_events, predicate),
        },
        "reduction": {
            "runs": shrunk.runs,
            "complete": shrunk.complete,
            "steps": shrunk.steps,
            "differences": shrunk.differences,
        },
        "windows": [found.to_json() for found in shrunk.windows],
        "replay": {"repeats": repeats, "distinct_hashes": distinct},
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
    directory: Path
    manifest: dict[str, Any]
    predicate: Predicate
    minimal: cases.Case
    original: cases.Case

    def ledger(self, which: str = "minimal") -> list[Event]:
        return read_jsonl(self.directory / FILES[which][1])


def load(directory: Path) -> Bundle:
    """Read a bundle. Raises ``ValueError`` if its manifest or one of its cases is invalid."""
    manifest = json.loads((directory / MANIFEST).read_text(encoding="ascii"))
    errors = validate(manifest)
    if errors:
        raise ValueError(f"{directory / MANIFEST}: not an {SCHEMA} manifest: " + "; ".join(errors))
    found = {}
    for which, (case_file, _ledger_file) in FILES.items():
        found[which] = json.loads((directory / case_file).read_text(encoding="ascii"))
        errors = cases.validate(found[which])
        if errors:
            raise ValueError(f"{which} case: " + "; ".join(errors))
    predicate = Predicate(manifest["predicate"]["contract"], manifest["predicate"]["rule"])
    return Bundle(directory, manifest, predicate, found["minimal"], found["original"])


@dataclass
class Replay:
    reproduced: bool  # every run broke the bundle's rule
    hashes: list[str]  # the ledger hash of each run
    recorded: str  # the hash the bundle recorded for the reduced case
    framework: dict[str, str]  # what it was replayed on
    scenario_changed: bool | None  # the scenario's code has changed; None if unknown
    invalid: tuple[str, ...] = ()  # why the runs are not valid evidence, if they are not

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
    recorded = bundle.manifest["scenario_digest"]
    changed = None
    if fingerprint is not None and recorded:
        changed = fingerprint(bundle.minimal["scenario"]) != recorded
    outcomes, framework = [], {}
    for _ in range(repeats):
        events = run(bundle.minimal)
        framework = events[0]["data"]["framework"]
        outcomes.append(judged(bundle.minimal, events))
    return Replay(
        reproduced=all(bundle.predicate in o.failures for o in outcomes),
        hashes=[o.hash or "" for o in outcomes],
        recorded=bundle.manifest["minimal"]["hash"],
        framework=framework,
        scenario_changed=changed,
        invalid=next((o.invalid for o in outcomes if o.invalid), ()),
    )


__all__ = [
    "FILES",
    "MANIFEST",
    "SCHEMA",
    "Bundle",
    "Replay",
    "capture",
    "load",
    "load_schema",
    "replay",
    "validate",
]
