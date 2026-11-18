"""The ``earful`` command: what each subcommand prints and the status it exits with."""

import json
import subprocess
import sys
from pathlib import Path

import livekit.agents as lk
import pytest

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
    """Run the command in this process. Returns its exit status, what it printed and what
    it wrote to standard error."""
    status = main([str(arg) for arg in argv])
    captured = capsys.readouterr()
    return status, captured.out, captured.err


@pytest.fixture
def truncated(tmp_path):
    """A ledger cut off before its end: not a valid run."""
    path = tmp_path / "truncated.jsonl"
    write_jsonl(read_jsonl(CLEAN)[:-1], path)
    return path


# --------------------------------------------------------------------------- check


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


def test_a_command_that_does_not_exist_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as stopped:
        main(["frobnicate"])
    assert stopped.value.code == REFUSED
    with pytest.raises(SystemExit) as stopped:
        main([])
    assert stopped.value.code == REFUSED


# --------------------------------------------------------------------------- run


def test_run_runs_the_scenario_and_writes_the_ledger_it_printed_the_hash_of(capsys, tmp_path):
    ledger = tmp_path / "out" / "run.jsonl"
    reference = "earful.livekit.cases:barge_in_mid_word"
    status, out, _ = earful(capsys, "run", reference, "--ledger", ledger)
    assert status == OK
    assert "case       barge_in_mid_word  (seed 0)" in out
    assert f"framework  livekit-agents {lk.__version__}" in out
    events = read_jsonl(ledger)
    assert validate(events) == [] and f"hash       {ledger_hash(events)}" in out
    committed = GOLDEN / f"lk-{lk.__version__}.barge_in_mid_word.ledger.normalized.jsonl"
    assert ledger.read_text() == committed.read_text()


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


# --------------------------------------------------------------------------- search, replay


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
    """By default the command searches in worker processes. A request it will refuse is
    refused before one is started."""
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
    # Asked for by number, the command does not quietly do something else.
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


def test_replay_of_what_is_not_a_bundle_is_refused(capsys, tmp_path):
    status, _, err = earful(capsys, "replay", tmp_path)
    assert status == REFUSED and "cannot read the bundle" in err


# --------------------------------------------------------------------------- crosscheck


@pytest.mark.slow
def test_crosscheck_runs_a_kept_failure_on_both_clocks_and_writes_every_ledger(capsys, tmp_path):
    """One real-clock run, which takes as long as the conversation. Whether it agrees
    depends on the machine: the tolerance here is wide enough for any."""
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


# --------------------------------------------------------------------------- report


def test_report_draws_a_page_where_the_output_is_one_and_text_elsewhere(capsys, tmp_path):
    page = tmp_path / "out" / "run.html"
    status, out, _ = earful(capsys, "report", BOOKED_TWICE, "-o", page)
    assert status == OK and out == f"report     {page}\n"
    drawn = page.read_text(encoding="utf-8")
    assert drawn.startswith("<!DOCTYPE html>") or drawn.lstrip().lower().startswith(
        "<!doctype html>"
    )
    assert "book_then_interrupt" in drawn and "A2" in drawn and "://" not in drawn
    # A bundle's page says what the bundle froze.
    kept = tmp_path / "kept.html"
    assert earful(capsys, "report", KEPT_3702, "-o", kept)[0] == OK
    assert "A2/rule" in kept.read_text(encoding="utf-8")
    # The format can be named: a page to standard output, text to a file named .html.
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


# --------------------------------------------------------------------------- the entry points


def test_the_command_runs_as_a_module_and_says_its_version():
    done = subprocess.run(
        [sys.executable, "-m", "earful", "--version"], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0 and done.stdout.startswith("earful ")


def test_a_scenario_in_the_directory_the_command_is_run_from_is_found(tmp_path):
    """The installed script does not have the working directory on its import path, as
    ``python -m`` does. A scenario module beside the user is found all the same."""
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
    """The commands that read files need nothing but the core. Those that run an agent
    say what is missing."""
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
