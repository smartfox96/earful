"""The ``earful`` command.

    earful check LEDGER...         the verdicts for recorded ledgers
    earful run SCENARIO            run a scenario once and print its verdicts
    earful search SCENARIO         search a scenario for the timings and faults that break it
    earful replay BUNDLE           run a kept failure again
    earful report LEDGER|BUNDLE    a run, drawn as one HTML page or written out as text

A SCENARIO is ``module:function``: a function that takes no arguments and returns the
case. Running one imports and calls it, and replaying a bundle imports and runs the
scenario the bundle names, so run only code you trust.

``check`` and ``report`` read files and need nothing else. ``run``, ``search`` and
``replay`` run an agent and need the framework adapter: ``pip install "earful[livekit]"``.

Exit status, for every command:

    0   nothing broke, or what happened is what was expected; for ``report``, the
        report was written, whatever it says
    1   a contract rule broke, or a replay did not do what --expect names
    2   the command could not be carried out: bad arguments, a file that is not what it
        should be, a scenario that does not exist
    3   there is no verdict: a run was not valid evidence, or did not repeat
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import __version__
from .core import bundle, html, timeline
from .core import case as cases
from .core.checker import Report, check
from .core.checker.report import allowed
from .core.ledger import read_jsonl, write_jsonl
from .core.predicate import Predicate

if TYPE_CHECKING:
    from types import ModuleType

OK, BROKEN, REFUSED, NO_VERDICT = 0, 1, 2, 3


class Refused(Exception):
    """The command cannot be carried out as asked. Its text is what the user is told."""


def _adapter() -> ModuleType:
    """The LiveKit adapter, imported only by the commands that run an agent.

    Those commands name a scenario as ``module:function``, so the directory the command
    is run from is put on the import path, as ``python -m`` and pytest put it there. The
    installed ``earful`` script would otherwise not find a module beside it.
    """
    here = os.getcwd()
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        from . import livekit
    except ImportError as exc:
        raise Refused(str(exc)) from exc
    return livekit


def _ledger(path: Path) -> list[dict[str, Any]]:
    try:
        events = read_jsonl(path)
    except OSError as exc:
        raise Refused(f"cannot read {path}: {exc.strerror}") from exc
    except ValueError as exc:  # not JSON, or not ASCII
        raise Refused(f"{path} is not a ledger file: {exc}") from exc
    if not all(isinstance(event, dict) for event in events):
        raise Refused(f"{path} is not a ledger file: a line is not a JSON object")
    return events


def _case(path: Path | None) -> cases.Case | None:
    if path is None:
        return None
    try:
        data = json.loads(path.read_text())
    except OSError as exc:
        raise Refused(f"cannot read {path}: {exc.strerror}") from exc
    except ValueError as exc:
        raise Refused(f"{path} is not a case file: {exc}") from exc
    errors = cases.validate(data)
    if errors:
        raise Refused(f"{path} is not an earful.case/1 document: " + "; ".join(errors))
    return dict(data)


def _status(reports: Sequence[Report], allow: Sequence[str] = ()) -> int:
    """A rule that broke outranks a run that could not be judged."""
    if any(rule not in allowed(allow) for report in reports for rule in report.broken()):
        return BROKEN
    return NO_VERDICT if any(not report.valid for report in reports) else OK


def _rules(text: list[str] | None) -> list[str]:
    return [str(Predicate.parse(rule)) for rule in text or []]


# --------------------------------------------------------------------------- check


def _check(args: argparse.Namespace) -> int:
    reports = []
    for path in args.ledger:
        report = check(_ledger(path))
        reports.append(report)
        if args.json:
            print(json.dumps({"ledger": path.name, **report.to_json()}))
            continue
        print(f"== {path.name}")
        for line in report.lines(measures=args.measures):
            print(f"   {line}")
    return _status(reports, _rules(args.allow))


# --------------------------------------------------------------------------- run


def _run(args: argparse.Namespace) -> int:
    adapter = _adapter()
    try:
        result = adapter.run(args.scenario, case=_case(args.case), seed=args.seed)
    except adapter.InvalidRun as exc:
        result = exc.result
    if args.ledger is not None:
        write_jsonl(result.events, args.ledger, normalized=True)
    report = result.report
    if args.json:
        print(json.dumps({"case": result.case, "hash": result.hash, **report.to_json()}))
    else:
        print("\n".join([*timeline.heading(result.events), "", *report.lines()]))
        if args.ledger is not None:
            print(f"\nledger     {args.ledger}")
    return _status([report], _rules(args.allow))


# --------------------------------------------------------------------------- search


def _search(args: argparse.Namespace) -> int:
    adapter = _adapter()
    keep = [Predicate.parse(rule) for rule in args.keep or []]
    if keep and args.out is None:
        raise Refused("--keep needs --out, the directory to write bundles to")
    faults = () if args.faults == "none" else tuple(args.faults.split(","))
    found = adapter.explore(
        args.scenario,
        case=_case(args.case),
        seed=args.seed,
        workers=args.workers,
        variables=() if args.variables == "none" else tuple(args.variables.split(",")),
        faults=faults,
        pairs=args.pairs,
        profiles=args.profiles,
        words=not args.no_words,
    )
    # A rule that is kept is measured whatever else is: its reduction starts from there.
    broken = found.breaks(windows=keep if args.no_windows else True)
    kept: dict[str, Any] = {}
    function = args.scenario.rpartition(":")[2]
    release = (found.runner.ledger(found.case) or [{}])[0].get("data", {}).get("framework", {})
    for predicate in keep:
        if not any(one.predicate == predicate for one in broken):
            continue
        rule = f"{predicate.contract}-{predicate.rule}".replace(" ", "-")
        directory = args.out / f"lk-{release.get('version')}.{function}.{rule}"
        manifest = adapter.keep(found, predicate, directory).manifest
        kept[str(predicate)] = {
            "directory": str(directory),
            "speeches": [manifest["original"]["speeches"], manifest["minimal"]["speeches"]],
            "delays": [manifest["original"]["delays"], manifest["minimal"]["delays"]],
            "faults": [manifest["original"]["faults"], manifest["minimal"]["faults"]],
            "reduction": manifest["reduction"],
            "windows": manifest["windows"],
            "replay": manifest["replay"],
        }

    if args.json:
        summary = {"scenario": args.scenario, **found.summary(), "coverage": found.coverage()}
        for one in broken:
            if not args.no_windows or one.predicate in keep:
                entry = summary["found"][str(one.predicate)]
                entry["windows"] = [w.to_json() for w in one.windows]
                entry["instants_only"] = one.instants_only
        summary["bundles"] = kept
        print(json.dumps(summary, indent=2))
    else:
        print(found.describe(broken))
        for rule, entry in kept.items():
            print(f"\nkept {rule} in {entry['directory']}")
        missing = [str(p) for p in keep if str(p) not in kept]
        if missing:
            print(f"\nnot kept, since no case broke them: {', '.join(missing)}")
    counted = [
        one
        for one in broken
        if not one.instants_only and str(one.predicate) not in allowed(_rules(args.allow))
    ]
    return BROKEN if counted else OK


# --------------------------------------------------------------------------- replay


def _replay(args: argparse.Namespace) -> int:
    adapter = _adapter()
    try:
        kept = bundle.load(args.bundle)
    except OSError as exc:
        raise Refused(f"cannot read the bundle in {args.bundle}: {exc.strerror}") from exc
    result = adapter.replay(args.bundle, repeats=args.repeats)
    outcome = {
        "predicate": str(kept.predicate),
        "made_on": kept.manifest["framework"],
        "replayed_on": result.framework,
        "reproduced": result.reproduced,
        "repeatable": result.repeatable,
        "identical_to_recorded": result.identical,
        "scenario_code_changed": result.scenario_changed,
        "invalid": list(result.invalid),
    }
    if args.json:
        print(json.dumps(outcome, indent=2))
    else:
        made, on = kept.manifest["framework"], result.framework
        print(f"{kept.predicate}, kept on {made.get('name')} {made.get('version')}")
        print(f"replayed {args.repeats} times on {on.get('name')} {on.get('version')}")
        print(f"  breaks the rule       {'yes' if result.reproduced else 'no'}")
        print(f"  every run the same    {'yes' if result.repeatable else 'no'}")
        print(f"  the ledger recorded   {'yes' if result.identical else 'no'}")
        changed = {None: "not known", True: "yes", False: "no"}[result.scenario_changed]
        print(f"  scenario code changed {changed}")
        for reason in result.invalid:
            print(f"  invalid: {reason}")
    if result.invalid or not result.repeatable:
        return NO_VERDICT
    if args.expect is None:
        return OK
    return OK if result.reproduced == (args.expect == "reproduced") else BROKEN


# --------------------------------------------------------------------------- report


def _report(args: argparse.Namespace) -> int:
    kept = args.source.is_dir()
    if kept:
        try:
            events = bundle.load(args.source).ledger(args.which)
        except OSError as exc:
            raise Refused(f"cannot read the bundle in {args.source}: {exc.strerror}") from exc
    else:
        events = _ledger(args.source)
    # A page where the output is a page, text where it is a terminal or a text file.
    as_page = args.format == "html" or (
        args.format is None
        and args.output is not None
        and args.output.suffix.lower() in (".html", ".htm")
    )
    if not as_page:
        written = timeline.text(events)
    elif kept:  # with what the bundle froze beside its ledger
        written = html.render_bundle(args.source, args.which)
    else:
        written = html.render(events, title=args.source.name)
    if args.output is None:
        sys.stdout.write(written)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(written, encoding="utf-8")
        print(f"report     {args.output}")
    return OK  # the report was written; what it says is not this command's status


# --------------------------------------------------------------------------- the command


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="earful",
        description="Deterministic regression tests for real-time voice agents.",
        epilog="Exit status: 0 nothing broke; 1 a contract rule broke; 2 the command could "
        "not be carried out; 3 no verdict (a run was not valid evidence, or did not repeat).",
    )
    parser.add_argument("--version", action="version", version=f"earful {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def command(name: str, run: Callable[[argparse.Namespace], int], help: str) -> Any:
        sub = commands.add_parser(name, help=help, description=help)
        sub.set_defaults(run=run)
        return sub

    def allow(sub: Any) -> None:
        sub.add_argument(
            "--allow",
            action="append",
            metavar="CONTRACT/RULE",
            help="a rule that may break without failing the command (repeatable)",
        )

    def scenario(sub: Any) -> None:
        sub.add_argument("scenario", metavar="SCENARIO", help="module:function")
        sub.add_argument("--case", type=Path, metavar="FILE", help="a saved case (earful.case/1)")
        sub.add_argument("--seed", type=int, default=0, help="seed of the run (default 0)")

    sub = command("check", _check, "Print the verdicts for recorded ledgers.")
    sub.add_argument("ledger", nargs="+", type=Path, metavar="LEDGER")
    sub.add_argument("--json", action="store_true", help="one JSON report per ledger")
    sub.add_argument("--measures", action="store_true", help="what each verdict measured")
    allow(sub)

    sub = command("run", _run, "Run a scenario once and print its verdicts.")
    scenario(sub)
    sub.add_argument("--ledger", type=Path, metavar="FILE", help="write the ledger here")
    sub.add_argument("--json", action="store_true", help="the report as JSON")
    allow(sub)

    sub = command(
        "search", _search, "Search a scenario for the timings and faults that break a contract."
    )
    scenario(sub)
    sub.add_argument(
        "--variables",
        default=",".join(("speech.position", *cases.DELAY_KINDS, cases.HANGUP)),
        metavar="A,B",
        help="kinds of time to move (default: speech.position, every provider delay and "
        'user.hangup; speech.end and speech.stt_delay can be added; "none" for none)',
    )
    sub.add_argument(
        "--faults",
        default=",".join(cases.FAULT_KINDS),
        metavar="A,B",
        help='faults to add to each backend write (default: all three; "none" for none)',
    )
    sub.add_argument(
        "--pairs",
        action="store_true",
        help="also move every speech and the hangup with each fault and each stretched "
        "delay in place, and add faults to two writes at once",
    )
    sub.add_argument(
        "--profiles", type=int, default=0, metavar="N", help="seeded provider-timing profiles"
    )
    sub.add_argument(
        "--no-words", action="store_true", help="the end of a played word is not a boundary"
    )
    sub.add_argument(
        "--no-windows",
        action="store_true",
        help="do not measure how wide each failure is (a kept one is still measured)",
    )
    sub.add_argument(
        "--keep",
        action="append",
        metavar="CONTRACT/RULE",
        help="reduce a case that breaks this rule and write it as a bundle (repeatable)",
    )
    sub.add_argument("--out", type=Path, metavar="DIR", help="where bundles go")
    sub.add_argument(
        "--workers",
        type=int,
        default=os.cpu_count() or 1,
        metavar="N",
        help="worker processes (default: one per processor; 0 runs in this process)",
    )
    sub.add_argument("--json", action="store_true", help="the whole result as JSON")
    allow(sub)

    sub = command("replay", _replay, "Run a kept failure's reduced case again.")
    sub.add_argument("bundle", type=Path, metavar="BUNDLE_DIR")
    sub.add_argument("--repeats", type=int, default=3, metavar="N", help="runs (default 3)")
    sub.add_argument(
        "--expect",
        choices=("reproduced", "clean"),
        help="fail unless the rule still breaks (reproduced) or no longer does (clean)",
    )
    sub.add_argument("--json", action="store_true", help="the outcome as JSON")

    sub = command(
        "report",
        _report,
        "Draw a run as one HTML page (a timeline, the verdicts and every event), or write "
        "it out as text.",
    )
    sub.add_argument("source", type=Path, metavar="LEDGER|BUNDLE_DIR")
    sub.add_argument(
        "--which",
        choices=tuple(bundle.FILES),
        default="minimal",
        help="for a bundle: the reduced case or the one first found (default minimal)",
    )
    sub.add_argument("-o", "--output", type=Path, metavar="FILE", help="write here, not stdout")
    sub.add_argument(
        "--format",
        choices=("html", "text"),
        help="default: html for an output file named .html, text otherwise",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.run(args))
    except Refused as exc:
        print(f"earful: {exc}", file=sys.stderr)
    except ValueError as exc:  # a scenario, a case or a bundle the core refuses
        print(f"earful: {exc}", file=sys.stderr)
    return REFUSED


__all__ = ["BROKEN", "NO_VERDICT", "OK", "REFUSED", "main"]
