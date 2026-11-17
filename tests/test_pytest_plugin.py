"""The pytest plugin: the ``earful`` fixture in a project of its own, and the evidence a
failing test leaves."""

import json
import subprocess
import sys

import pytest

# The framework is imported here, before any test runs pytest on a project of its own:
# such a run puts the imported modules back as they were when it ends, and the
# framework's extension modules cannot be imported a second time in one process.
import earful.livekit as adapter
from earful.core.ledger import read_jsonl
from earful.core.schema import validate
from earful.pytest_plugin import Earful

PROJECT = '''
import livekit.agents as lk

from earful.livekit import Case, Effect, Fault, ScriptedModel, UserSpeech
from earful.livekit import tool_returned, user_ever_said


class Agent(lk.Agent):
    def __init__(self):
        super().__init__(instructions="You take restaurant reservations.")

    @lk.function_tool
    async def create_reservation(self, party_size: int) -> str:
        """Create a reservation.

        Args:
            party_size: Number of guests
        """
        raise AssertionError("a test reached the reservation service")


def booking(**changes):
    fields = dict(
        name="booking",
        agent=Agent,
        effects={"create_reservation": Effect("book-table")},
        speeches=[UserSpeech(start=0.5, end=2.5, transcript="Book a table for six tonight.")],
        policy=ScriptedModel()
        .when(tool_returned(), say="Your table is booked.", claims=["book-table"])
        .when(user_ever_said("table"), say="One moment.",
              call=("create_reservation", {"party_size": 6}))
        .otherwise(say="How can I help?"),
        drain_delay=6.0,
    )
    return Case(**{**fields, **changes})


def test_as_written(earful):
    earful.run(booking()).assert_contracts()


def test_with_a_failed_write(earful):
    earful.run(booking(faults=[Fault("backend.error", 1)])).assert_contracts()


def test_searched(earful):
    found = earful.explore(booking(), variables=(), faults=["backend.error"], words=False)
    found.assert_contracts()


def test_that_fails_without_earful():
    assert False
'''


@pytest.fixture
def project(pytester):
    pytester.makepyfile(test_booking=PROJECT)
    return pytester


def test_the_fixture_runs_and_searches_and_a_broken_contract_fails_the_test(project):
    result = project.runpytest_inprocess("-p", "no:cacheprovider", "-p", "no:asyncio")
    result.assert_outcomes(passed=1, failed=3)
    result.stdout.fnmatch_lines(
        [
            "*ContractFailure: booking (seed 0, livekit-agents *) breaks A3/rule*",
            "*ContractFailure: booking on livekit-agents *: * 1 contract rule(s) broken*",
        ]
    )


def test_a_failing_test_leaves_its_ledgers_and_cases_and_says_where(project):
    result = project.runpytest_inprocess("-p", "no:cacheprovider", "-p", "no:asyncio")
    out = project.path / ".earful"
    ran = out / "test_booking.py-test_with_a_failed_write"
    ledger = ran / "run-1.booking.ledger.jsonl"
    assert validate(read_jsonl(ledger)) == []
    page = (ran / "run-1.booking.html").read_text(encoding="utf-8")
    assert "<svg" in page and "A3" in page and "://" not in page
    result.stdout.fnmatch_lines(
        [
            "*- earful -*",
            "run 1 (booking) breaks A3/rule",
            f"    ledger  {ledger.relative_to(project.path)}",
            f"    report  {(ran / 'run-1.booking.html').relative_to(project.path)}",
            "search 1 (booking) breaks A3/rule: backend.error 1",
        ]
    )
    searched = out / "test_booking.py-test_searched"
    case = json.loads((searched / "search-1.booking.A3-rule.case.json").read_text())
    assert case["faults"] == [{"of": "backend.error", "nth": 1, "us": None}]
    assert validate(read_jsonl(searched / "search-1.booking.A3-rule.ledger.jsonl")) == []
    # A test that passes, and one that fails without the fixture, leave nothing.
    assert {p.name for p in out.iterdir()} == {ran.name, searched.name}


def test_the_evidence_goes_where_the_command_line_says(project, tmp_path):
    project.runpytest_inprocess(
        "-p", "no:cacheprovider", "-p", "no:asyncio", "--earful-out", str(tmp_path / "kept")
    )
    assert (tmp_path / "kept" / "test_booking.py-test_with_a_failed_write").is_dir()
    assert not (project.path / ".earful").exists()


def test_a_search_uses_the_workers_the_command_line_gave_unless_it_names_its_own(
    monkeypatch, tmp_path
):
    asked = []
    monkeypatch.setattr(
        adapter, "explore", lambda scenario, **options: asked.append(options) or "found"
    )
    helper = Earful(tmp_path, workers=4)
    assert helper.explore("some:scenario", words=False) == "found"
    helper.explore("some:scenario", workers=0)
    assert asked == [{"words": False, "workers": 4}, {"workers": 0}]
    assert helper.searches == ["found", "found"]


@pytest.mark.slow
def test_a_failure_is_kept_under_the_tests_own_directory_and_replays(tmp_path):
    helper = Earful(tmp_path / "evidence", workers=0)
    found = helper.explore(
        "breaks:as_written_confirms_a_failed_booking",
        variables=(),
        faults=["backend.error"],
        words=False,
    )
    kept = helper.keep(found, "A3/rule")
    assert kept.directory == tmp_path / "evidence" / "bundle.A3-rule"
    assert helper.replay(kept.directory).reproduced


def test_a_run_that_is_not_valid_evidence_is_kept_as_evidence_too(tmp_path):
    helper = Earful(tmp_path, workers=0)
    with pytest.raises(adapter.InvalidRun):
        helper.run("breaks:host_with_no_rule_for_stop")
    (line, *_rest) = helper.evidence()
    assert line == "run 1 (barge_in_mid_word) breaks not valid evidence"
    assert (tmp_path / "run-1.barge_in_mid_word.ledger.jsonl").exists()


def test_loading_the_plugin_does_not_import_the_framework():
    """pytest loads the plugin in every project that has Earful installed, whether or not
    its tests use it."""
    code = (
        "import sys, earful.pytest_plugin\n"
        "loaded = [m for m in sys.modules if m.split('.')[0] == 'livekit']\n"
        "assert not loaded, loaded\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr


def test_the_plugin_is_found_by_its_entry_point(pytester):
    result = pytester.runpytest_subprocess("--help")
    result.stdout.fnmatch_lines(["*--earful-out=DIR*", "*--earful-workers=N*"])
