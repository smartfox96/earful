"""The ``earful`` command.
Exit status: 0 nothing broke, 1 a rule broke, 2 the command was refused, 3 no verdict."""

from __future__ import annotations

import argparse
import contextlib
import importlib
import inspect
import json
import os
import sys
import sysconfig
import traceback
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

from . import __version__, _adapters
from .core import bundle, crosscheck, html, timeline
from .core import case as cases
from .core.checker import CONTRACTS, Report, check
from .core.checker.report import RULES, allowed, required
from .core.ledger import read_jsonl, write_jsonl
from .core.predicate import Predicate
from .core.search import CaseRefused, default_workers

OK, BROKEN, REFUSED, NO_VERDICT = 0, 1, 2, 3
_PACKAGE = Path(__file__).resolve().parent
# The standard library and the installed packages: code there is not the scenario's.
_INSTALLED = tuple(
    {
        Path(sysconfig.get_path(name)).resolve()
        for name in ("stdlib", "platstdlib", "purelib", "platlib")
    }
)


class Refused(Exception):
    """The command cannot be carried out as asked. Its text is what the user is told."""


def _scenario_path() -> None:
    # The installed script would not otherwise find a scenario's module in this directory.
    here = os.getcwd()
    if here not in sys.path:
        sys.path.insert(0, here)


def _outside(exc: BaseException) -> str | None:
    """Where code that is not Earful's raised ``exc``, as ``file, line N``, or None."""
    if isinstance(exc, SyntaxError):
        return f"{Path(exc.filename or '?').name}, line {exc.lineno}"
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames or Path(frames[-1].filename).resolve().is_relative_to(_PACKAGE):
        return None
    return f"{Path(frames[-1].filename).name}, line {frames[-1].lineno}"


def _scenario(reference: str) -> tuple[Any, Any]:
    """The adapter and the scenario ``reference`` names, with its first case built."""
    try:
        return _adapters.select(reference)
    except (CaseRefused, ImportError):
        raise
    except Exception as exc:
        # The module and its function are the author's code. Earful's own crash is raised.
        where = _outside(exc)
        if where is None:
            raise
        said = exc.msg if isinstance(exc, SyntaxError) else str(exc)
        raise Refused(
            f"scenario {reference!r} could not be built: {type(exc).__name__}: {said} ({where})"
        ) from exc


def _in_the_scenario(exc: BaseException) -> str | None:
    """The innermost line of the scenario's own code under ``exc``, unless Earful raised it."""
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames or Path(frames[-1].filename).resolve().is_relative_to(_PACKAGE):
        return None
    for frame in reversed(frames):
        path = Path(frame.filename).resolve()
        ours = frame.filename.startswith("<") or path.is_relative_to(_PACKAGE)
        if not ours and not any(path.is_relative_to(home) for home in _INSTALLED):
            return f"{path.name}, line {frame.lineno}"
    return None


@contextlib.contextmanager
def _running(reference: str) -> Iterator[None]:
    """A crash of the scenario's own code during a run is a refusal. Any other is raised."""
    try:
        yield
    except Exception as exc:
        where = _in_the_scenario(exc)
        if where is None:
            raise
        raise Refused(
            f"scenario {reference!r} could not be run: {type(exc).__name__}: {exc} ({where})"
        ) from exc


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


def _status(reports: Sequence[Report], allow: set[str], require: list[str]) -> int:
    # A rule that broke outranks a run that could not be judged.
    if any(rule not in allow for report in reports for rule in report.broken()):
        return BROKEN
    unjudged = any(not report.valid or report.unjudged(require) for report in reports)
    return NO_VERDICT if unjudged else OK


def _asked(args: argparse.Namespace) -> tuple[set[str], list[str]]:
    """What ``--allow`` and ``--require`` name, checked before anything runs."""
    return allowed(args.allow or ()), required(args.require or ())


def _required(report: Report, require: list[str]) -> list[str]:
    return [f"required   {contract} {why}" for contract, why in report.unjudged(require).items()]


def _check(args: argparse.Namespace) -> int:
    allow, require = _asked(args)
    reports = []
    for path in args.ledger:
        report = check(_ledger(path))
        reports.append(report)
        if args.json:
            print(json.dumps({"ledger": path.name, **report.to_json()}))
            continue
        print(f"== {path.name}")
        for line in (*report.lines(measures=args.measures), *_required(report, require)):
            print(f"   {line}")
    return _status(reports, allow, require)


def _run(args: argparse.Namespace) -> int:
    allow, require = _asked(args)
    case = _case(args.case)
    adapter, source = _scenario(args.scenario)
    try:
        with _running(args.scenario):
            result = adapter.run(source, data=case, seed=args.seed)
    except adapter.InvalidRun as exc:
        result = exc.result
    if args.ledger is not None:
        write_jsonl(result.events, args.ledger, normalized=True)
    report = result.report
    if args.json:
        print(json.dumps({"case": result.case, "hash": result.hash, **report.to_json()}))
    else:
        unjudged = _required(report, require)
        print("\n".join([*timeline.heading(result.events), "", *report.lines()]))
        if unjudged:
            print("\n" + "\n".join(unjudged))
        if args.ledger is not None:
            print(f"\nledger     {args.ledger}")
    return _status([report], allow, require)


def _search(args: argparse.Namespace) -> int:
    allow, require = _asked(args)
    keep = [Predicate.parse(rule) for rule in args.keep or []]
    if keep and args.out is None:
        raise Refused("--keep needs --out, the directory to write bundles to")
    faults = () if args.faults == "none" else tuple(args.faults.split(","))
    case = _case(args.case)

    adapter, source = _scenario(args.scenario)

    def search(workers: int) -> Any:
        return adapter.explore(
            source,
            data=case,
            seed=args.seed,
            workers=workers,
            variables=() if args.variables == "none" else tuple(args.variables.split(",")),
            faults=faults,
            pairs=args.pairs,
            profiles=args.profiles,
            words=not args.no_words,
            max_runs=args.max_runs,
            planned=lambda plan: print(f"earful: {plan}", file=sys.stderr),
        )

    try:
        found = search(default_workers() if args.workers is None else args.workers)
    except adapter.WorkersUnavailable as exc:
        if args.workers is not None:  # asked for by number: say so, do not do otherwise
            raise Refused(str(exc)) from exc
        print(f"earful: {exc}. Searching in this process.", file=sys.stderr)
        found = search(0)
    # A rule that is kept is measured whatever else is: its reduction starts from there.
    broken = found.breaks(windows=keep if args.no_windows else True)
    kept: dict[str, Any] = {}
    function = args.scenario.rpartition(":")[2]
    release = (found.runner.ledger(found.case) or [{}])[0].get("data", {}).get("framework", {})
    for predicate in keep:
        if not any(one.predicate == predicate for one in broken):
            continue
        directory = args.out / _bundle_name(release, function, predicate)
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

    unjudged = found.unjudged(require)
    if args.json:
        summary = {"scenario": args.scenario, **found.summary(), "coverage": found.coverage()}
        summary["unjudged"] = unjudged
        for one in broken:
            if not args.no_windows or one.predicate in keep:
                entry = summary["found"][str(one.predicate)]
                entry["windows"] = [w.to_json() for w in one.windows]
                entry["unmeasured"] = one.unmeasured
                entry["instants_only"] = one.instants_only
        summary["bundles"] = kept
        print(json.dumps(summary, indent=2))
    else:
        print(found.describe(broken, unjudged))
        unmeasured = [str(one.predicate) for one in broken if one.predicate not in keep]
        if args.no_windows and unmeasured:
            print(
                "\nnot measured, so counted even if they break only at single instants: "
                + ", ".join(unmeasured)
            )
        for rule, entry in kept.items():
            runs = entry["reduction"]["runs"]
            print(f"\nkept {rule} in {entry['directory']}, reduced in {runs} more run(s)")
        missing = [str(p) for p in keep if str(p) not in kept]
        if missing:
            print(f"\nnot kept, since no case broke them: {', '.join(missing)}")
    if any(not one.instants_only and str(one.predicate) not in allow for one in broken):
        return BROKEN
    return NO_VERDICT if unjudged or found.no_verdict else OK


# What the directory of a kept bundle begins with, for each framework.
_SHORT = {"livekit-agents": "lk", "pipecat-ai": "pc"}


def _bundle_name(framework: dict[str, str], function: str, predicate: Predicate) -> str:
    rule = f"{predicate.contract}-{predicate.rule}".replace(" ", "-")
    return f"{_SHORT[framework['name']]}-{framework['version']}.{function}.{rule}"


def _replay(args: argparse.Namespace) -> int:
    kept = bundle.load(args.bundle)
    adapter = _adapters.by_framework(kept.manifest["framework"]["name"])
    _scenario(kept.minimal["scenario"])  # refused here if its code cannot build a case
    with _running(kept.minimal["scenario"]):
        result = adapter.replay(kept, repeats=args.repeats)
    kept_with = kept.manifest["environment"].get("earful")
    outcome = {
        "predicate": str(kept.predicate),
        "made_on": kept.manifest["framework"],
        "replayed_on": result.framework,
        "kept_with_earful": kept_with,
        "replayed_with_earful": __version__,
        "kept_under_contracts": kept.manifest["formats"]["contracts"],
        "judged_by_contracts": CONTRACTS,
        "judged": result.judged,
        "reproduced": result.reproduced,
        "clean": result.clean,
        "contract_outcomes": list(result.outcomes),
        "contract_codes": list(result.codes),
        "other_rules_broken": list(result.others),
        "repeatable": result.repeatable,
        "identical_to_recorded": result.identical,
        "scenario_code_changed": result.scenario_changed,
        "unverified": list(result.unverified),
        "invalid": list(result.invalid),
    }
    if args.json:
        print(json.dumps(outcome, indent=2))
    else:
        made, on = kept.manifest["framework"], result.framework
        if result.reproduced:
            breaks = "yes"
        elif not result.judged:
            breaks = "not judged"
        else:
            breaks = "in some runs" if result.broke else "no"
        gave = ["not valid evidence" if one is None else one for one in result.outcomes]
        print(f"{kept.predicate}, kept on {made.get('name')} {made.get('version')}")
        print(f"replayed {args.repeats} times on {on.get('name')} {on.get('version')}")
        print(f"  breaks the rule       {breaks}")
        contract = f"{kept.predicate.contract} gave"
        why = f" ({', '.join(result.codes)})" if result.codes else ""
        print(f"  {contract:<22}{gave[0] if len(set(gave)) == 1 else ', '.join(gave)}{why}")
        if result.others:
            print(f"  other rules broken    {', '.join(result.others)}")
        print(f"  every run the same    {'yes' if result.repeatable else 'no'}")
        print(f"  the ledger recorded   {'yes' if result.identical else 'no'}")
        changed = {None: "not known", True: "yes", False: "no"}[result.scenario_changed]
        print(f"  scenario code changed {changed}")
        if result.unverified:
            files = " and ".join(result.unverified)
            print(f"  not verified: {files} (the manifest records no digest of them)")
        if kept_with not in (None, __version__):
            print(f"  kept with Earful {kept_with}, replayed with Earful {__version__}")
        if outcome["kept_under_contracts"] != CONTRACTS:
            print(f"  kept under {outcome['kept_under_contracts']}, judged here by {CONTRACTS}")
        for reason in result.invalid:
            print(f"  invalid: {reason}")
    if not result.judged or not result.repeatable:
        return NO_VERDICT
    if args.expect is None:
        return OK
    expected = result.reproduced if args.expect == "reproduced" else result.clean
    return OK if expected else BROKEN


def _crosscheck(args: argparse.Namespace) -> int:
    source = args.source
    case = _case(args.case)
    if Path(args.source).is_dir():  # a kept failure: its reduced case
        kept = bundle.load(Path(args.source))
        case = kept.minimal
        _adapters.by_framework(kept.manifest["framework"]["name"])  # imports its adapter
        source = case["scenario"]
    reference = source
    adapter, source = _scenario(source)
    tolerance = crosscheck.Tolerance(time_us=args.tolerance_ms * 1000, words=args.words)
    try:
        with _running(reference):
            checked = adapter.cross_check(
                source, data=case, seed=args.seed, runs=args.runs, tolerance=tolerance
            )
    except adapter.InvalidRun as exc:
        print("the virtual run is not valid evidence, so there is nothing to compare with:")
        for reason in exc.reasons:
            print(f"    {reason}")
        return NO_VERDICT
    if args.out is not None:
        write_jsonl(checked.virtual, args.out / "virtual.ledger.jsonl", normalized=True)
        for number, events in enumerate(checked.real, 1):
            write_jsonl(events, args.out / f"real-{number}.ledger.jsonl", normalized=True)
    if args.json:
        print(json.dumps(checked.to_json(), indent=2))
    else:
        print("\n".join(checked.lines()))
        if args.out is not None:
            print(f"\nledgers    {args.out}")
    if checked.agrees:
        return OK
    return NO_VERDICT if checked.unusable else BROKEN


def _report(args: argparse.Namespace) -> int:
    kept = args.source.is_dir()
    if kept:
        try:
            events = bundle.load(args.source).ledger(args.which)
        except OSError as exc:
            raise Refused(f"cannot read the bundle in {args.source}: {exc.strerror}") from exc
    else:
        events = _ledger(args.source)
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


def _ignored(directory: Path) -> str | None:
    # Adds ``.earful/`` to ``.gitignore`` where there is one or the directory is under git.
    listing = directory / ".gitignore"
    if listing.exists():
        text = listing.read_text(encoding="utf-8")
        if any(line.strip() in (".earful", ".earful/") for line in text.splitlines()):
            return None
        separator = "" if not text or text.endswith("\n") else "\n"
        listing.write_text(f"{text}{separator}.earful/\n", encoding="utf-8")
        return "added to .gitignore"
    if (directory / ".git").exists():
        listing.write_text(".earful/\n", encoding="utf-8")
        return ".gitignore written"
    return None


def _named(reference: str) -> Any:
    """What ``module:name`` names, or None where it names nothing: the scaffold then says why."""
    module, _, name = reference.partition(":")
    try:
        return getattr(importlib.import_module(module), name) if module and name else None
    except (ImportError, AttributeError, ValueError):
        return None


def _init(args: argparse.Namespace) -> int:
    # A livekit-agents agent is a class; a Pipecat bot is the function that sets it up.
    named = _named(args.agent)
    bot = named is not None and not inspect.isclass(named) and callable(named)
    try:
        if bot:
            from .pipecat.scaffold import scaffold
        else:
            from .livekit.scaffold import scaffold
    except ImportError as exc:
        if bot:
            raise Refused(
                f"{args.agent} is not a class of agent (a subclass of livekit.agents.Agent). "
                "As a function it would be a Pipecat bot, and pipecat-ai is not installed. "
                'Install it with: pip install "earful[pipecat]"'
            ) from exc
        raise Refused(
            f"{args.agent} needs livekit-agents, which is not installed. Install it with: "
            'pip install "earful[livekit]"'
        ) from exc

    made = scaffold(args.agent)
    target: Path = args.output or Path(made.filename)
    if target.exists():
        raise Refused(f"{target} is already there; move it, or name another file with -o")
    try:
        target.write_text(made.source, encoding="utf-8")
    except OSError as exc:
        raise Refused(f"cannot write {target}: {exc.strerror or exc}") from exc
    print(f"wrote      {target}")
    if made.tools:
        print(f"{made.noun:<11}{', '.join(made.tools)}")
        print("           each is declared an effect; delete those that only read")
    if made.unbuilt:
        print(f"build      None for each argument does not build the agent ({made.unbuilt})")
        print("           give build() in the file a stand-in for each before running it")
    done = _ignored(Path.cwd())
    if done:
        print(f"ignored    .earful/ ({done})")
    print(f"next       pytest {target}")
    if made.tools:
        print("           its search fails (A2) while a write has no idempotency key declared;")
        print("           the file says where to declare one")
    return OK


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

    def allow(sub: Any, required_of: str) -> None:
        sub.add_argument(
            "--allow",
            action="append",
            metavar="CONTRACT/RULE",
            help="a rule that may break without failing the command (repeatable)",
        )
        sub.add_argument(
            "--require",
            action="append",
            metavar="CONTRACT",
            help=f"a contract, one of {', '.join(RULES)}, that {required_of} must get a pass "
            "or a fail for; unobservable or not exercised then exits 3 (repeatable)",
        )

    def scenario(sub: Any) -> None:
        sub.add_argument("scenario", metavar="SCENARIO", help="module:function")
        sub.add_argument("--case", type=Path, metavar="FILE", help="a saved case (earful.case/1)")
        sub.add_argument(
            "--seed", type=int, help="seed of the run (default 0, or the one --case holds)"
        )

    sub = command(
        "init",
        _init,
        "Write a first test for a livekit-agents agent class or a Pipecat bot function, "
        "from its own tools.",
    )
    sub.add_argument(
        "agent", metavar="AGENT", help="module:Class, such as myapp.agent:SupportAgent"
    )
    sub.add_argument(
        "-o", "--output", type=Path, metavar="FILE", help="write here, not test_<agent>.py"
    )

    sub = command("check", _check, "Print the verdicts for recorded ledgers.")
    sub.add_argument("ledger", nargs="+", type=Path, metavar="LEDGER")
    sub.add_argument("--json", action="store_true", help="one JSON report per ledger")
    sub.add_argument("--measures", action="store_true", help="what each verdict measured")
    allow(sub, "each ledger")

    sub = command("run", _run, "Run a scenario once and print its verdicts.")
    scenario(sub)
    sub.add_argument("--ledger", type=Path, metavar="FILE", help="write the ledger here")
    sub.add_argument("--json", action="store_true", help="the report as JSON")
    allow(sub, "the run")

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
        "--max-runs",
        type=int,
        metavar="N",
        help="stop after N runs. A search cut short is incomplete and exits 3 unless a rule broke",
    )
    sub.add_argument(
        "--no-windows",
        action="store_true",
        help="do not measure how wide each failure is (a kept one is still measured). A "
        "rule that breaks only at single instants then counts as broken",
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
        metavar="N",
        help="worker processes (default: one per processor or what EARFUL_WORKERS says, "
        "or this process where worker processes cannot be started; 0 runs in this process)",
    )
    sub.add_argument("--json", action="store_true", help="the whole result as JSON")
    allow(sub, "the case as given")

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
        "crosscheck",
        _crosscheck,
        "Run a case once on the virtual clock and again on the real clock, with nothing "
        "patched, and say whether the real-clock runs agree with the virtual one.",
    )
    sub.add_argument(
        "source", metavar="SCENARIO|BUNDLE_DIR", help="module:function, or a kept failure"
    )
    sub.add_argument("--case", type=Path, metavar="FILE", help="a saved case (earful.case/1)")
    sub.add_argument(
        "--seed", type=int, help="seed of the runs (default 0, or the one the case holds)"
    )
    sub.add_argument("--runs", type=int, default=3, metavar="N", help="real-clock runs (default 3)")
    sub.add_argument(
        "--tolerance-ms",
        type=int,
        default=100,
        metavar="MS",
        help="how far an event may be from its virtual time (default 100)",
    )
    sub.add_argument(
        "--words",
        type=int,
        default=1,
        metavar="N",
        help="words a segment that was cut short may differ by (default 1)",
    )
    sub.add_argument("--out", type=Path, metavar="DIR", help="write every run's ledger here")
    sub.add_argument("--json", action="store_true", help="the result as JSON")

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
        if args.run in (_init, _run, _search, _replay, _crosscheck):
            _scenario_path()
        return int(args.run(args))
    except (Refused, ImportError) as exc:
        print(f"earful: {exc}", file=sys.stderr)
    except ValueError as exc:  # a scenario, a case or a bundle the core refuses
        print(f"earful: {exc}", file=sys.stderr)
    return REFUSED


__all__ = ["BROKEN", "NO_VERDICT", "OK", "REFUSED", "main"]
