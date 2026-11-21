"""The ``earful`` command: what each subcommand prints and the status it exits with."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from earful.cli import BROKEN, NO_VERDICT, OK, REFUSED, main
from earful.core.ledger import read_jsonl, write_jsonl

HERE = Path(__file__).parent
GOLDEN, BUNDLES = HERE / "golden", HERE / "bundles"
BOOKED_TWICE = GOLDEN / "lk-1.5.6.book_then_interrupt.ledger.normalized.jsonl"  # fails A2 and M2
CLEAN = GOLDEN / "lk-1.8.12.book_then_interrupt.ledger.normalized.jsonl"
KEPT_3702 = BUNDLES / "lk-1.5.6.book_then_interrupt.A2-rule"


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


def test_a_command_that_does_not_exist_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as stopped:
        main(["frobnicate"])
    assert stopped.value.code == REFUSED
    with pytest.raises(SystemExit) as stopped:
        main([])
    assert stopped.value.code == REFUSED


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
