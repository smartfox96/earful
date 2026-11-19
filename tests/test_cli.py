"""The ``earful`` command: what each subcommand prints and the status it exits with."""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import livekit.agents as lk
import pytest

from earful import __version__
from earful.cli import BROKEN, NO_VERDICT, OK, REFUSED, main
from earful.core import case as cases
from earful.core.ledger import ledger_hash, read_jsonl, write_jsonl
from earful.core.schema import validate, validity
from earful.livekit import scenario

HERE = Path(__file__).parent
GOLDEN, BUNDLES = HERE / "golden", HERE / "bundles"
BOOKED_TWICE = GOLDEN / "lk-1.5.6.book_then_interrupt.ledger.normalized.jsonl"  # fails A2 and M2
CLEAN = GOLDEN / "lk-1.8.12.book_then_interrupt.ledger.normalized.jsonl"
KEPT_3702 = BUNDLES / "lk-1.5.6.book_then_interrupt.A2-rule"
VERSION = tuple(int(part) for part in lk.__version__.split(".")[:3])
HAS_3702 = VERSION < (1, 6, 6)


def earful(capsys, *argv):
    """Run the command in this process: (exit status, stdout, stderr)."""
    status = main([str(arg) for arg in argv])
    captured = capsys.readouterr()
    return status, captured.out, captured.err


@pytest.fixture
def truncated(tmp_path):
    path = tmp_path / "truncated.jsonl"
    write_jsonl(read_jsonl(CLEAN)[:-1], path)
    return path


def test_check_prints_each_contract_and_exits_0_when_nothing_broke(capsys):
    status, out, _ = earful(capsys, "check", CLEAN)
    assert status == OK
    assert out.startswith(f"== {CLEAN.name}\n   A1  not_exercised\n   A2  pass\n")


def test_check_exits_1_when_a_rule_broke_and_says_which(capsys):
    status, out, _ = earful(capsys, "check", CLEAN, BOOKED_TWICE)
    assert status == BROKEN
    assert "A2  fail" in out and "the scenario authorized 1" in out
    assert out.count("== ") == 2


def test_a_rule_that_is_allowed_to_break_does_not_fail_the_command(capsys):
    assert earful(capsys, "check", BOOKED_TWICE, "--allow", "A2/rule")[0] == BROKEN  # M2 too
    allowed = ("--allow", "A2/rule", "--allow", "M2/rule d")
    assert earful(capsys, "check", BOOKED_TWICE, *allowed)[0] == OK


def test_check_exits_3_for_a_ledger_that_is_not_a_valid_run(capsys, truncated):
    status, out, _ = earful(capsys, "check", truncated)
    assert status == NO_VERDICT and "invalid: " in out and "run.end" in out
    # A rule that broke outranks a run that could not be judged.
    assert earful(capsys, "check", truncated, BOOKED_TWICE)[0] == BROKEN


def test_check_as_json_is_one_report_per_line(capsys):
    status, out, _ = earful(capsys, "check", CLEAN, BOOKED_TWICE, "--json")
    first, second = (json.loads(line) for line in out.splitlines())
    assert status == BROKEN and first["ledger"] == CLEAN.name and first["valid"]
    assert second["results"]["A2"]["outcome"] == "fail"


def test_check_with_measures_prints_what_each_verdict_measured(capsys):
    _, out, _ = earful(capsys, "check", CLEAN, "--measures")
    assert '"committed": 1' in out


def test_what_cannot_be_read_or_is_not_a_ledger_is_refused_with_status_2(capsys, tmp_path):
    status, out, err = earful(capsys, "check", tmp_path / "missing.jsonl")
    assert status == REFUSED and out == "" and "cannot read" in err
    notes = tmp_path / "notes.txt"
    notes.write_text("not a ledger\n")
    status, _, err = earful(capsys, "check", notes)
    assert status == REFUSED and "is not a ledger file" in err
    numbers = tmp_path / "numbers.jsonl"
    numbers.write_text("1\n2\n")
    assert earful(capsys, "check", numbers)[0] == REFUSED
    status, _, err = earful(capsys, "check", CLEAN, "--allow", "A2")
    assert status == REFUSED and "CONTRACT/RULE" in err


def test_a_rule_or_a_contract_that_does_not_exist_is_refused_before_anything_runs(
    capsys, monkeypatch, tmp_path
):
    def ran(source):
        raise AssertionError(f"{source} was built")

    monkeypatch.setattr("earful._adapters.select", ran)
    reference = "earful.livekit.cases:barge_in_mid_word"
    for refused in (
        ("check", CLEAN, "--allow", "Z9/rule"),
        ("check", CLEAN, "--require", "A9"),
        ("run", reference, "--allow", "nonsense"),
        ("run", reference, "--require", "A2/rule"),
        ("search", reference, "--allow", "A3"),
        ("search", reference, "--require", "Z9"),
        ("search", reference, "--keep", "Z9/rule", "--out", tmp_path),
    ):
        status, out, err = earful(capsys, *refused)
        assert status == REFUSED and out == "", refused
        rules, contracts = "one of: A1/rule, A1/in-flight", "are: A1, A2, A3, M1, M2, L1, L2"
        assert err.startswith("earful: no contract "), err
        assert (contracts if "--require" in refused else rules) in err, err
    assert list(tmp_path.iterdir()) == []


def test_a_required_contract_with_no_pass_or_fail_exits_3_and_says_why(capsys):
    assert earful(capsys, "check", CLEAN, "--require", "A2", "--require", "A3")[0] == OK
    status, out, _ = earful(capsys, "check", CLEAN, "--require", "A2", "--require", "A1")
    assert status == NO_VERDICT
    assert "\n   required   A1 was not exercised: the run has no speech that retires" in out
    assert "required   A2" not in out
    # A rule that broke outranks a contract that was not judged.
    assert earful(capsys, "check", BOOKED_TWICE, "--require", "A1")[0] == BROKEN
    reference = "earful.livekit.cases:barge_in_mid_word"
    assert earful(capsys, "run", reference, "--require", "L2")[0] == OK
    status, out, _ = earful(capsys, "run", reference, "--require", "A2")
    assert status == NO_VERDICT
    assert out.endswith(
        "\n\nrequired   A2 was not exercised: the run has no write request for a declared intent\n"
    )


def test_a_command_that_does_not_exist_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as stopped:
        main(["frobnicate"])
    assert stopped.value.code == REFUSED
    with pytest.raises(SystemExit) as stopped:
        main([])
    assert stopped.value.code == REFUSED


def test_run_runs_the_scenario_and_writes_the_ledger_it_printed_the_hash_of(
    capsys, tmp_path, reference_ledger
):
    ledger = tmp_path / "out" / "run.jsonl"
    reference = "earful.livekit.cases:barge_in_mid_word"
    status, out, _ = earful(capsys, "run", reference, "--ledger", ledger)
    assert status == OK
    assert "case       barge_in_mid_word  (seed 0)" in out
    assert f"framework  livekit-agents {lk.__version__}" in out
    events = read_jsonl(ledger)
    assert validate(events) == [] and f"hash       {ledger_hash(events)}" in out
    assert ledger.read_text() == reference_ledger("barge_in_mid_word").read_text()


def test_run_exits_1_when_the_scenario_breaks_a_rule_as_it_does_from_a_saved_case(capsys, tmp_path):
    reference = "breaks:as_written_confirms_a_failed_booking"
    assert earful(capsys, "run", reference)[0] == OK
    saved = tmp_path / "failed.case.json"
    saved.write_text(json.dumps(cases.with_fault(scenario.default(reference), "backend.error", 1)))
    status, out, _ = earful(capsys, "run", reference, "--case", saved, "--json")
    assert status == BROKEN
    assert json.loads(out)["results"]["A3"]["outcome"] == "fail"
    assert earful(capsys, "run", reference, "--case", saved, "--allow", "A3/rule")[0] == OK


def test_run_exits_3_for_a_run_that_is_not_valid_evidence_and_still_writes_its_ledger(
    capsys, tmp_path
):
    ledger = tmp_path / "invalid.jsonl"
    status, out, _ = earful(capsys, "run", "breaks:host_with_no_rule_for_stop", "--ledger", ledger)
    assert status == NO_VERDICT and "invalid: " in out and "NoRule" in out
    assert read_jsonl(ledger)[-1]["kind"] == "run.end"


def test_a_scenario_or_a_case_that_does_not_exist_is_refused(capsys, tmp_path):
    status, _, err = earful(capsys, "run", "earful.livekit.cases:nope")
    assert status == REFUSED and "no scenario 'earful.livekit.cases:nope'" in err
    bad = tmp_path / "bad.case.json"
    bad.write_text('{"schema": "earful.case/0"}')
    status, _, err = earful(capsys, "run", "breaks:host", "--case", bad)
    assert status == REFUSED and "is not an earful.case/1 document" in err
    assert earful(capsys, "run", "breaks:host", "--case", tmp_path / "none.json")[0] == REFUSED


MISTAKES = """
import functools

import livekit.agents as lk

from earful.livekit import Case, ScriptedModel, UserSpeech
from earful.livekit.cases import barge_in_mid_word

model = ScriptedModel().otherwise(say="Hello.")


def needs(party_size):
    return barge_in_mid_word()


def raises():
    raise RuntimeError("the fixtures are missing")


host_for_four = functools.partial(needs, 4)


def misspelt():
    return Case(name="host", agent=lk.Agent, speechs=[], policy=model)


def instance():
    case = barge_in_mid_word()
    case.agent = lk.Agent(instructions="You are a restaurant host.")
    return case


def mode():
    case = barge_in_mid_word()
    case.backend_mode = "idempotent"
    return case


def backwards():
    case = barge_in_mid_word()
    case.speeches = [UserSpeech(start=2.5, end=0.5, transcript="Hello.")]
    return case
"""


@pytest.fixture
def mistakes(tmp_path, monkeypatch):
    (tmp_path / "mistakes.py").write_text(MISTAKES)
    (tmp_path / "half_written.py").write_text("def booking(:\n    pass\n")
    monkeypatch.syspath_prepend(tmp_path)
    monkeypatch.delitem(sys.modules, "mistakes", raising=False)


_NAMED = "a scenario is named module:function, such as test_booking:booking; got"
_NOT_ONE = "is not a function that takes no arguments and returns a Case"


@pytest.mark.parametrize(
    ("reference", "said"),
    [
        ("mistakes:model", f"scenario 'mistakes:model' {_NOT_ONE}: model is of type ScriptedModel"),
        ("mistakes:needs", f"scenario 'mistakes:needs' {_NOT_ONE}: needs needs arguments"),
        ("mistakes", f"{_NAMED} 'mistakes'"),
        ("mistakes:", f"{_NAMED} 'mistakes:'"),
        ("half_written:booking", "SyntaxError: invalid syntax (half_written.py, line 1)"),
        ("mistakes:raises", "RuntimeError: the fixtures are missing (mistakes.py, line 17)"),
        ("mistakes:misspelt", "TypeError: Case.__init__() got an unexpected keyword argument"),
        ("mistakes:instance", "got an object of Agent. Write agent=lambda: Agent(...)"),
        ("mistakes:mode", "unknown backend mode 'idempotent'; choose from ['plain', 'keyed']"),
        ("mistakes:backwards", "the speech at 2.5 s ends at 0.5 s; a speech ends after it starts"),
    ],
)
def test_a_common_mistake_in_a_scenario_is_refused_in_one_line(capsys, mistakes, reference, said):
    status, out, err = earful(capsys, "run", reference)
    assert status == REFUSED and out == ""
    assert err.startswith("earful: ") and said in err and len(err.splitlines()) == 1
    if "Error: " in said:  # raised by the scenario's own code
        assert err.startswith(f"earful: scenario {reference!r} could not be built: ")
    status, out, err = earful(capsys, "search", reference, "--workers", "0")
    assert status == REFUSED and out == "" and len(err.splitlines()) == 1


def test_a_scenario_is_anything_its_module_names_that_can_be_called_with_no_arguments(
    capsys, mistakes
):
    status, out, _ = earful(capsys, "run", "mistakes:host_for_four")
    assert status == OK and "case       barge_in_mid_word" in out


def test_what_earful_itself_raises_while_a_scenario_is_built_is_not_made_a_refusal(
    capsys, monkeypatch
):
    from earful import _adapters

    monkeypatch.setattr(_adapters, "_ADAPTERS", None)  # select then fails in Earful's own code
    with pytest.raises(AttributeError):
        main(["run", "earful.livekit.cases:barge_in_mid_word"])


def test_a_kept_failure_whose_scenario_no_longer_builds_is_refused_in_one_line(
    capsys, tmp_path, monkeypatch
):
    kept = tmp_path / "kept"
    shutil.copytree(KEPT_3702, kept)
    monkeypatch.setattr("earful.livekit.cases.book_then_interrupt", lambda: 1 / 0)
    for command in ("replay", "crosscheck"):
        status, out, err = earful(capsys, command, kept)
        assert status == REFUSED and out == ""
        assert len(err.splitlines()) == 1 and err.startswith(
            "earful: scenario 'earful.livekit.cases:book_then_interrupt' could not be built: "
            "ZeroDivisionError: division by zero (test_cli.py, line "
        )


def test_a_search_asked_for_a_fault_or_a_variable_there_is_not_is_told_which_there_are(capsys):
    status, _, err = earful(capsys, "search", "breaks:host", "--faults", "backend.melts")
    assert status == REFUSED and err == (
        "earful: unknown faults ['backend.melts']; choose from "
        "['backend.error', 'backend.timeout', 'backend.drop']\n"
    )
    status, _, err = earful(capsys, "search", "breaks:host", "--variables", "speech.colour")
    assert status == REFUSED and err.startswith(
        "earful: unknown variables ['speech.colour']; choose from ['speech.position', "
    )


@pytest.mark.slow
def test_search_finds_the_break_keeps_it_and_the_kept_bundle_replays(capsys, tmp_path):
    reference = "breaks:as_written_confirms_a_failed_booking"
    narrow = ("--variables", "none", "--no-words", "--workers", "0")
    status, out, _ = earful(capsys, "search", reference, *narrow)
    assert status == BROKEN
    assert out.startswith(f"book_then_interrupt on livekit-agents {lk.__version__}: ")
    assert "A3/rule  in " in out and "changed: backend." in out
    assert earful(capsys, "search", reference, *narrow, "--allow", "A3/rule")[0] == OK

    kept = ("--keep", "A3/rule", "--keep", "A2/rule", "--out", tmp_path)
    status, out, _ = earful(capsys, "search", reference, *narrow, *kept, "--no-windows", "--json")
    summary = json.loads(out)
    assert status == BROKEN and summary["scenario"] == reference
    assert list(summary["bundles"]) == ["A3/rule"]  # no case broke A2/rule: nothing to keep
    # The kept rule is measured although --no-windows was given.
    assert summary["found"]["A3/rule"]["windows"]
    assert summary["found"]["A3/rule"]["instants_only"] is False
    directory = Path(summary["bundles"]["A3/rule"]["directory"])
    assert directory == tmp_path / (
        f"lk-{lk.__version__}.as_written_confirms_a_failed_booking.A3-rule"
    )
    status, out, _ = earful(capsys, "replay", directory, "--expect", "reproduced")
    assert status == OK and "breaks the rule       yes" in out
    assert "the ledger recorded   yes" in out and "scenario code changed no" in out
    assert earful(capsys, "replay", directory, "--expect", "clean")[0] == BROKEN

    status, out, _ = earful(
        capsys, "search", reference, *narrow, "--keep", "A2/rule", "--out", tmp_path
    )
    assert "not kept, since no case broke them: A2/rule" in out


def test_a_kept_bundle_is_named_after_its_framework_its_scenario_and_its_rule():
    from earful import _adapters
    from earful.cli import _SHORT, _bundle_name
    from earful.core.predicate import Predicate

    pipecat = {"name": "pipecat-ai", "version": "1.16.0"}
    assert _bundle_name(pipecat, "booking", Predicate("L2", "rule b")) == (
        "pc-1.16.0.booking.L2-rule-b"
    )
    livekit = {"name": "livekit-agents", "version": "1.8.12"}
    assert _bundle_name(livekit, "booking", Predicate("A2", "rule")) == "lk-1.8.12.booking.A2-rule"
    assert set(_SHORT) == set(_adapters._ADAPTERS)  # a framework with an adapter has a name


def test_a_ledger_file_is_read_and_written_by_name_or_by_path(tmp_path):
    events = read_jsonl(str(CLEAN))
    assert events == read_jsonl(CLEAN)
    write_jsonl(events, str(tmp_path / "deep" / "copy.jsonl"))
    assert read_jsonl(tmp_path / "deep" / "copy.jsonl") == events


def test_keep_without_a_directory_and_a_search_of_no_scenario_are_refused(capsys):
    status, _, err = earful(capsys, "search", "breaks:host", "--keep", "L2/rule a")
    assert status == REFUSED and "--keep needs --out" in err
    assert earful(capsys, "search", "breaks:nope", "--workers", "0")[0] == REFUSED
    status, _, err = earful(capsys, "search", "breaks:host", "--variables", "speech.colour")
    assert status == REFUSED and "unknown variables" in err


def test_a_search_asked_for_what_there_is_not_starts_no_worker(capsys, monkeypatch):
    started = []
    monkeypatch.setattr(scenario, "pool", lambda workers=None: started.append(workers))
    status, _, err = earful(capsys, "search", "breaks:host", "--variables", "speech.colour")
    assert status == REFUSED and "unknown variables" in err and started == []


def test_a_search_starts_a_worker_per_processor_or_as_many_as_the_environment_says(
    capsys, monkeypatch
):
    started = []

    def counted(workers=None):
        started.append(workers)
        raise PermissionError(13, "Permission denied")  # the search then runs in this process

    monkeypatch.setattr(scenario, "pool", counted)
    monkeypatch.setattr("os.cpu_count", lambda: 6)
    narrow = ("--variables", "none", "--faults", "backend.error", "--no-words")
    reference = "breaks:as_written_reads_the_error"
    monkeypatch.delenv("EARFUL_WORKERS", raising=False)
    assert earful(capsys, "search", reference, *narrow)[0] == OK
    monkeypatch.setenv("EARFUL_WORKERS", "2")
    assert earful(capsys, "search", reference, *narrow)[0] == OK
    assert earful(capsys, "search", reference, *narrow, "--workers", "3")[0] == REFUSED
    assert started == [6, 2, 3]
    monkeypatch.setenv("EARFUL_WORKERS", "-4")
    status, _, err = earful(capsys, "search", reference, *narrow)
    assert status == REFUSED and "EARFUL_WORKERS is a number of processes" in err


def test_a_search_exits_3_when_the_case_as_given_has_no_pass_or_fail_for_a_required_contract(
    capsys,
):
    narrow = ("--variables", "none", "--faults", "backend.error", "--no-words", "--workers", "0")
    reference = "breaks:as_written_reads_the_error"
    assert earful(capsys, "search", reference, *narrow, "--require", "A2")[0] == OK
    status, out, _ = earful(capsys, "search", reference, *narrow, "--require", "A1")
    assert status == NO_VERDICT
    assert out.endswith(
        "\n\nrequired, with no pass or fail in the case as given:\n"
        "    A1 was not exercised: the run has no speech that retires an intent and whose "
        "transcript was delivered\n"
    )
    status, out, _ = earful(capsys, "search", reference, *narrow, "--require", "A1", "--json")
    assert status == NO_VERDICT and list(json.loads(out)["unjudged"]) == ["A1"]


def test_a_search_told_not_to_measure_says_which_rules_it_counts_unmeasured(capsys):
    narrow = ("--variables", "none", "--faults", "backend.error", "--no-words", "--workers", "0")
    reference = "breaks:as_written_confirms_a_failed_booking"
    status, out, _ = earful(capsys, "search", reference, *narrow, "--no-windows")
    assert status == BROKEN and out.endswith(
        "\n\nnot measured, so counted even if they break only at single instants: A3/rule\n"
    )
    status, out, _ = earful(capsys, "search", reference, *narrow)
    assert status == BROKEN and "not measured" not in out


def test_a_search_that_could_judge_nothing_it_tried_exits_3_and_gives_the_reasons(
    capsys, monkeypatch
):
    import earful.livekit as adapter
    from earful.core.search import CaseRefused, explore

    def searched(source, **options):
        start = source.default()

        def run(case):
            if case == start:
                return read_jsonl(CLEAN)
            raise CaseRefused("this adapter cannot fail a write")

        return explore(start, run, variables=(), faults=["backend.error"])

    monkeypatch.setattr(adapter, "explore", searched)
    reference = "earful.livekit.cases:book_then_interrupt"
    status, out, _ = earful(capsys, "search", reference, "--workers", "0")
    assert status == NO_VERDICT
    assert out.endswith(
        ": 1 cases tried, 0 judged, 0 contract rule(s) broken\n\n"
        "not judged: 1 of 1 cases, which leaves the search with no result\n"
        "    1 refused: this adapter cannot fail a write\n"
        "        such as backend.error 1\n"
    )
    status, out, _ = earful(capsys, "search", reference, "--workers", "0", "--json")
    assert status == NO_VERDICT and json.loads(out)["not_judged"][0]["cases"] == 1


def test_a_search_says_how_many_cases_it_will_run_and_one_cut_short_by_its_budget_exits_3(
    capsys,
):
    narrow = ("--variables", "llm.ttft", "--faults", "none", "--no-words", "--workers", "0")
    reference = "breaks:as_written_reads_the_error"
    status, out, err = earful(capsys, "search", reference, *narrow, "--max-runs", "3")
    assert status == NO_VERDICT
    (said,) = re.findall(
        r"^earful: (\d+) cases \(\d+ placements of a time, and the faults\); (\d+) to run, "
        r"after 1 run\(s\) to find them\. The budget of 3 runs covers 2 of them$",
        err,
        re.MULTILINE,
    )
    assert said[0] == said[1] and int(said[0]) > 2
    left = int(said[0]) - 2
    assert f": 2 of {said[0]} cases tried, 2 judged, 0 contract rule(s) broken\n" in out
    assert out.endswith(
        f"\n\nincomplete: the budget of 3 runs was spent with {left} case(s) not run. "
        "Nothing is concluded about them\n"
    )
    status, out, _ = earful(capsys, "search", reference, *narrow, "--max-runs", "3", "--json")
    summary = json.loads(out)
    assert status == NO_VERDICT and summary["complete"] is False and summary["not_run"] == left
    assert (summary["runs"], summary["max_runs"]) == (3, 3)
    status, _, err = earful(capsys, "search", reference, *narrow, "--max-runs", "0")
    assert status == REFUSED and "max_runs is a number of runs, at least 1" in err


def test_a_search_in_which_a_case_raised_exits_3_and_says_that_this_is_why(capsys, monkeypatch):
    import earful.livekit as adapter
    from earful.core.search import explore

    def searched(source, **options):
        start = source.default()

        def run(case):
            if any(fault["of"] == "backend.drop" for fault in case["faults"]):
                raise RuntimeError("the reservation service is down")
            return read_jsonl(CLEAN)

        return explore(start, run, variables=(), faults=["backend.error", "backend.drop"])

    monkeypatch.setattr(adapter, "explore", searched)
    reference = "earful.livekit.cases:book_then_interrupt"
    status, out, _ = earful(capsys, "search", reference, "--workers", "0")
    assert status == NO_VERDICT
    assert out.endswith(
        ": 2 cases tried, 1 judged, 0 contract rule(s) broken\n\n"
        "not judged: 1 of 2 cases; the search fails, since 1 raised\n"
        "    1 error: RuntimeError: the reservation service is down\n"
        "        such as backend.drop 1\n"
    )
    status, out, _ = earful(capsys, "search", reference, "--workers", "0", "--json")
    assert status == NO_VERDICT and json.loads(out)["error"] == 1


def test_where_workers_cannot_be_started_the_search_runs_in_this_process_unless_told_a_number(
    capsys, monkeypatch
):
    def refused(workers=None):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(scenario, "pool", refused)
    narrow = ("--variables", "none", "--faults", "backend.error", "--no-words")
    reference = "breaks:as_written_reads_the_error"
    status, out, err = earful(capsys, "search", reference, *narrow)
    assert status == OK and "0 contract rule(s) broken" in out
    assert "cannot be started here" in err and "Searching in this process." in err
    status, out, err = earful(capsys, "search", reference, *narrow, "--workers", "2")
    assert status == REFUSED and out == "" and "cannot be started here" in err
    assert earful(capsys, "search", reference, *narrow, "--workers", "0")[0] == OK


def test_replay_of_a_committed_bundle_says_whether_this_release_still_has_the_defect(capsys):
    status, out, _ = earful(capsys, "replay", KEPT_3702, "--json")
    outcome = json.loads(out)
    assert status == OK  # without --expect, either outcome is a result
    assert outcome["predicate"] == "A2/rule" and outcome["repeatable"]
    assert outcome["reproduced"] is HAS_3702
    assert outcome["replayed_on"]["version"] == lk.__version__
    expected, other = ("reproduced", "clean") if HAS_3702 else ("clean", "reproduced")
    assert earful(capsys, "replay", KEPT_3702, "--expect", expected)[0] == OK
    assert earful(capsys, "replay", KEPT_3702, "--expect", other)[0] == BROKEN


def test_replay_that_could_not_be_judged_has_no_verdict_whatever_was_expected(capsys, monkeypatch):
    clean, broken = read_jsonl(CLEAN), read_jsonl(BOOKED_TWICE)
    runs = []

    def stand_in(*ledgers):
        runs[:] = ledgers * 3
        monkeypatch.setattr(scenario, "run", lambda data: runs.pop(0))

    for expect in ((), ("--expect", "clean"), ("--expect", "reproduced")):
        stand_in(clean[:-1])  # a run with no end is not valid evidence
        status, out, _ = earful(capsys, "replay", KEPT_3702, *expect)
        assert status == NO_VERDICT
        assert "breaks the rule       not judged" in out and "  invalid: " in out
    stand_in(clean[:-1])
    outcome = json.loads(earful(capsys, "replay", KEPT_3702, "--json")[1])
    assert (outcome["judged"], outcome["reproduced"], outcome["clean"]) == (False, False, False)
    assert outcome["repeatable"] and outcome["invalid"]

    stand_in(clean)
    status, out, _ = earful(capsys, "replay", KEPT_3702, "--expect", "clean")
    assert status == OK and "breaks the rule       no\n" in out and "invalid" not in out
    stand_in(clean)
    outcome = json.loads(earful(capsys, "replay", KEPT_3702, "--json")[1])
    assert (outcome["judged"], outcome["reproduced"], outcome["clean"]) == (True, False, True)
    stand_in(broken)
    status, out, _ = earful(capsys, "replay", KEPT_3702, "--expect", "clean")
    assert status == BROKEN and "breaks the rule       yes\n" in out

    stand_in(broken, clean, clean)  # runs that differ show nothing either
    status, out, _ = earful(capsys, "replay", KEPT_3702, "--expect", "clean")
    assert status == NO_VERDICT and "breaks the rule       in some runs\n" in out
    status, _, err = earful(capsys, "replay", KEPT_3702, "--repeats", "0")
    assert status == REFUSED and err == "earful: a replay makes at least one run; got repeats=0\n"


def test_replay_says_what_it_could_not_verify_and_which_earful_kept_the_bundle(
    capsys, tmp_path, monkeypatch
):
    monkeypatch.setattr(scenario, "run", lambda data: read_jsonl(CLEAN))
    kept = tmp_path / "kept"
    shutil.copytree(KEPT_3702, kept)
    manifest = json.loads((kept / "manifest.json").read_text())

    def replayed(earful_version, digests):
        manifest["environment"] = {"earful": earful_version} if earful_version else {}
        if not digests:  # as a bundle written before manifests recorded them
            del manifest["original"]["case_digest"], manifest["minimal"]["case_digest"]
        (kept / "manifest.json").write_text(json.dumps(manifest))
        out = earful(capsys, "replay", kept)[1]
        return out, json.loads(earful(capsys, "replay", kept, "--json")[1])

    out, outcome = replayed(__version__, digests=True)
    assert "not verified" not in out and "kept with" not in out
    assert outcome["unverified"] == [] and outcome["kept_with_earful"] == __version__
    out, outcome = replayed(None, digests=True)  # a bundle that does not say
    assert "kept with" not in out and outcome["kept_with_earful"] is None
    out, outcome = replayed("0.0.1", digests=False)
    assert (
        "  not verified: original.case.json and minimal.case.json "
        "(the manifest records no digest of them)\n"
    ) in out
    assert f"  kept with Earful 0.0.1, replayed with Earful {__version__}\n" in out
    assert outcome["unverified"] == ["original.case.json", "minimal.case.json"]
    assert (outcome["kept_with_earful"], outcome["replayed_with_earful"]) == ("0.0.1", __version__)


def test_replay_of_what_is_not_a_bundle_is_refused(capsys, tmp_path):
    status, _, err = earful(capsys, "replay", tmp_path)
    assert status == REFUSED
    assert err == f"earful: cannot read {tmp_path / 'manifest.json'}: No such file or directory\n"
    (tmp_path / "manifest.json").write_text('{"schema": "ear')
    status, _, err = earful(capsys, "replay", tmp_path)
    assert status == REFUSED
    assert err.startswith(f"earful: {tmp_path / 'manifest.json'} is not JSON: Unterminated string")


def test_a_bundle_whose_files_are_not_what_its_manifest_records_is_refused_by_every_command(
    capsys, tmp_path
):
    kept = tmp_path / "kept"
    shutil.copytree(KEPT_3702, kept)
    (kept / "minimal.ledger.jsonl").write_text("garbage\n")
    for command in ("replay", "crosscheck", "report"):
        status, out, err = earful(capsys, command, kept)
        assert status == REFUSED and out == ""
        assert err.startswith(f"earful: {kept / 'minimal.ledger.jsonl'} is not a ledger file: ")
        assert len(err.splitlines()) == 1


@pytest.mark.slow
def test_crosscheck_runs_a_kept_failure_on_both_clocks_and_writes_every_ledger(capsys, tmp_path):
    """Whether the real-clock run agrees depends on the machine, so the tolerance is wide."""
    kept = BUNDLES / "lk-1.5.6.speak_over_new_turn.L2-rule-b"
    wide = ("--tolerance-ms", "2000", "--words", "40")
    status, out, _ = earful(capsys, "crosscheck", kept, "--runs", "1", *wide, "--out", tmp_path)
    assert status in (OK, BROKEN)  # a rule the real run does not break is a difference
    lines = out.splitlines()
    assert lines[0] == f"speak_over_new_turn on livekit-agents {lk.__version__}"
    assert lines[1].startswith("on the virtual clock it breaks ")
    assert "real-clock runs agree with the virtual run, within 2000 ms and 40 word(s)" in lines[2]
    assert lines[3].startswith("run 1: ")
    assert validity(read_jsonl(tmp_path / "virtual.ledger.jsonl")) == []
    assert validity(read_jsonl(tmp_path / "real-1.ledger.jsonl")) == [
        "the run was on the real clock"
    ]


def test_crosscheck_of_what_cannot_be_run_is_refused_or_has_no_verdict(capsys, tmp_path):
    assert earful(capsys, "crosscheck", "breaks:nope")[0] == REFUSED
    assert earful(capsys, "crosscheck", tmp_path)[0] == REFUSED  # a directory, not a bundle
    assert earful(capsys, "crosscheck", "breaks:host", "--runs", "0")[0] == REFUSED
    status, out, _ = earful(capsys, "crosscheck", "breaks:host_with_no_rule_for_stop")
    assert status == NO_VERDICT and "nothing to compare with" in out and "NoRule" in out


def test_report_draws_a_page_where_the_output_is_one_and_text_elsewhere(capsys, tmp_path):
    page = tmp_path / "out" / "run.html"
    status, out, _ = earful(capsys, "report", BOOKED_TWICE, "-o", page)
    assert status == OK and out == f"report     {page}\n"
    drawn = page.read_text(encoding="utf-8")
    assert drawn.startswith("<!DOCTYPE html>") or drawn.lstrip().lower().startswith(
        "<!doctype html>"
    )
    assert "book_then_interrupt" in drawn and "A2" in drawn and "://" not in drawn
    kept = tmp_path / "kept.html"
    assert earful(capsys, "report", KEPT_3702, "-o", kept)[0] == OK
    assert "A2/rule" in kept.read_text(encoding="utf-8")
    status, out, _ = earful(capsys, "report", CLEAN, "--format", "html")
    assert status == OK and "<svg" in out
    named = tmp_path / "plain.html"
    assert earful(capsys, "report", CLEAN, "--format", "text", "-o", named)[0] == OK
    assert named.read_text().startswith("case       book_then_interrupt")


def test_report_writes_a_run_out_as_text_and_exits_0_whatever_it_says(capsys, tmp_path):
    status, out, _ = earful(capsys, "report", BOOKED_TWICE)
    assert status == OK
    assert out.startswith("case       book_then_interrupt  (seed 0)\n")
    assert "A2  fail" in out and " effect.committed " in out and " run.end " in out
    assert len(out.splitlines()) > len(read_jsonl(BOOKED_TWICE))  # every event has its line
    target = tmp_path / "reports" / "kept.txt"
    status, out, _ = earful(capsys, "report", KEPT_3702, "--output", target)
    assert status == OK and out == f"report     {target}\n"
    assert "A2  fail" in target.read_text()
    original = earful(capsys, "report", KEPT_3702, "--which", "original")[1]
    assert original.startswith("case       book_then_interrupt")
    assert earful(capsys, "report", tmp_path / "missing.jsonl")[0] == REFUSED


def test_the_command_runs_as_a_module_and_says_its_version():
    done = subprocess.run(
        [sys.executable, "-m", "earful", "--version"], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0 and done.stdout.startswith("earful ")


def test_a_scenario_in_the_directory_the_command_is_run_from_is_found(tmp_path):
    """Unlike ``python -m``, the installed script has no working directory on its path."""
    (tmp_path / "my_scenarios.py").write_text(
        "from earful.livekit.cases import barge_in_mid_word\n\n\n"
        "def host():\n    return barge_in_mid_word()\n"
    )
    code = (
        "import sys\n"
        "sys.path[:] = [entry for entry in sys.path if entry not in ('', '.')]\n"
        "from earful.cli import main\n"
        "sys.exit(main(['run', 'my_scenarios:host']))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False, cwd=tmp_path
    )
    assert done.returncode == 0, done.stderr
    assert "case       barge_in_mid_word" in done.stdout


def test_without_the_framework_ledgers_are_checked_and_a_run_is_refused_with_what_to_install():
    code = (
        "import sys\n"
        "sys.modules['livekit'] = None\n"  # any 'import livekit' now raises ImportError
        "from earful.cli import main\n"
        f"assert main(['check', {str(CLEAN)!r}]) == 0\n"
        f"assert main(['report', {str(CLEAN)!r}]) == 0\n"
        "assert main(['run', 'earful.livekit.cases:barge_in_mid_word']) == 2\n"
        "assert not [m for m in sys.modules if m.startswith('earful.livekit.')]\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert 'pip install "earful[livekit]"' in done.stderr
