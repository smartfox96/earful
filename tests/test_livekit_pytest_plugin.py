"""The pytest plugin: the ``earful`` fixture, and the evidence a failing test leaves."""

import json
import os

import pytest

pytest.importorskip("livekit.agents")

# Imported before pytester runs: the framework's extension modules cannot be imported twice.
from shapes.corpus import KnownDefect

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


FAILS_WHILE_THE_SERVICE_IS_DOWN = """
def test_booking(earful):
    faults = [Fault("backend.error", 1)] if Path("service-down").exists() else []
    earful.run(booking(faults=faults)).assert_contracts()
"""


def test_a_test_leaves_only_the_evidence_of_its_last_run_and_none_once_it_passes(pytester):
    shared = PROJECT.partition("def test_as_written")[0]
    pytester.makepyfile(
        test_booking="from pathlib import Path\n" + shared + FAILS_WHILE_THE_SERVICE_IS_DOWN
    )

    def run():
        return pytester.runpytest_inprocess("-p", "no:cacheprovider", "-p", "no:asyncio")

    (pytester.path / "service-down").touch()
    run().assert_outcomes(failed=1)
    out = pytester.path / ".earful"
    kept = out / "test_booking.py-test_booking"
    (kept / "from-an-earlier-run.html").touch()
    (out / "of-another-test").mkdir()
    run().assert_outcomes(failed=1)
    assert sorted(p.name for p in kept.iterdir()) == [
        "run-1.booking.html",
        "run-1.booking.ledger.jsonl",
    ]
    (pytester.path / "service-down").unlink()
    run().assert_outcomes(passed=1)
    assert [p.name for p in out.iterdir()] == ["of-another-test"]
    (out / "of-another-test").rmdir()
    (pytester.path / "service-down").touch()
    run().assert_outcomes(failed=1)
    (pytester.path / "service-down").unlink()
    run().assert_outcomes(passed=1)
    assert not out.exists()  # nothing of any test is left in it


def test_the_evidence_goes_where_the_command_line_says(project, tmp_path):
    project.runpytest_inprocess(
        "-p", "no:cacheprovider", "-p", "no:asyncio", "--earful-out", str(tmp_path / "kept")
    )
    assert (tmp_path / "kept" / "test_booking.py-test_with_a_failed_write").is_dir()
    assert not (project.path / ".earful").exists()


IMPORTABLE = "breaks:as_written_confirms_a_failed_booking"


@pytest.fixture
def asked(monkeypatch):
    """The options each search the fixture starts is given."""
    monkeypatch.setattr(os, "cpu_count", lambda: 6)  # so no number given is the default by chance
    given = []
    monkeypatch.setattr(
        adapter, "explore", lambda scenario, **options: given.append(options) or "found"
    )
    return given


def test_a_search_uses_the_workers_the_command_line_gave_unless_it_names_its_own(asked, tmp_path):
    helper = Earful(tmp_path, workers=4)
    assert helper.explore(IMPORTABLE, words=False) == "found"
    helper.explore(IMPORTABLE, workers=0, variables=["llm.ttft"])
    assert [(o["workers"], o["words"]) for o in asked] == [(4, False), (0, True)]
    # What the call leaves out is left to the adapter's defaults.
    assert "variables" not in asked[0] and asked[1]["variables"] == ["llm.ttft"]
    assert helper.searches == ["found", "found"]


def test_a_search_nobody_gave_a_number_for_leaves_it_to_the_adapter(asked, tmp_path):
    helper = Earful(tmp_path, workers=None)
    helper.explore(IMPORTABLE)
    helper.explore(IMPORTABLE, workers=2)
    assert [options["workers"] for options in asked] == [None, 2]


def test_a_search_is_given_the_wait_only_if_the_test_gives_one(asked, tmp_path):
    helper = Earful(tmp_path, workers=0)
    helper.explore(IMPORTABLE)
    helper.explore(IMPORTABLE, wait_us=5, stretch_us=6, delay_limit_us=7)
    assert "wait_us" not in asked[0]
    assert (asked[1]["wait_us"], asked[1]["stretch_us"], asked[1]["delay_limit_us"]) == (5, 6, 7)


def test_a_test_run_that_xdist_has_spread_over_the_processors_searches_in_its_own_process(
    asked, tmp_path
):
    Earful(tmp_path, workers=None, spread=True).explore(IMPORTABLE)
    Earful(tmp_path, workers=3, spread=True).explore(IMPORTABLE)  # the command line said
    assert [options["workers"] for options in asked] == [0, 3]


@pytest.mark.slow
def test_a_failure_is_kept_under_the_tests_own_directory_and_replays(tmp_path):
    helper = Earful(tmp_path / "evidence", workers=0)
    found = helper.explore(
        "breaks:as_written_confirms_a_failed_booking",
        variables=(),
        faults=["backend.error"],
        words=False,
    )
    with pytest.raises(ValueError, match="run again at least once; got repeats=0"):
        helper.keep(found, "A3/rule", repeats=0)
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


COLLIDING = """
import pytest


@pytest.mark.parametrize("who", ["a/b", "a b", "a-b"])
def test_booking(earful, who):
    faults = [] if who == "a-b" else [Fault("backend.error", 1)]
    earful.run(booking(faults=faults)).assert_contracts()
"""


@pytest.mark.xfail(
    strict=True,
    raises=KnownDefect,
    reason="Earful: ids that differ in one character share evidence",
)
def test_tests_whose_ids_differ_in_one_character_keep_their_evidence_apart(pytester):
    pytester.makepyfile(test_booking=PROJECT.partition("def test_as_written")[0] + COLLIDING)
    result = pytester.runpytest_inprocess("-p", "no:cacheprovider", "-p", "no:asyncio")
    result.assert_outcomes(passed=1, failed=2)
    out = pytester.path / ".earful"
    kept = sorted(p.name for p in out.iterdir()) if out.exists() else []
    if len(kept) != 2:
        raise KnownDefect(f"the two failing tests left {kept}")
