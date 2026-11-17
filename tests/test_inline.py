"""A scenario built where it is used: run and searched in the process of the test, and
refused, before anything runs, by what needs to build it again from a name."""

import livekit.agents as lk
import pytest
from breaks import BOOKING, TalksToItsServiceItself, as_written_confirms_a_failed_booking

from earful.core import case as cases
from earful.core.checker import ContractFailure, check
from earful.core.predicate import Predicate
from earful.livekit import scenario
from earful.livekit.api import explore, keep, replay, run
from earful.livekit.cases import BOOKING_ACK, BOOKING_INTENT, barge_in_mid_word
from earful.livekit.harness import Case, Fault
from earful.livekit.playbook import (
    ScriptedModel,
    reply,
    tool_returned,
    tool_succeeded,
    user_ever_said,
)
from earful.livekit.providers import UserSpeech
from earful.livekit.scenario import INLINE, Scenario

CONFIRMED_BUT_NOT_DONE = Predicate("A3", "rule")

BOOK = reply(BOOKING_ACK, call=("create_reservation", {"party_size": 6}))
CONFIRMS_ANYTHING = (
    ScriptedModel()
    .when(tool_returned(), say="Your table is booked.", claims=[BOOKING_INTENT])
    .when(user_ever_said("table"), BOOK)
    .otherwise(say="How can I help?")
)
READS_THE_ERROR = (
    ScriptedModel()
    .when(tool_succeeded(), say="Your table is booked.", claims=[BOOKING_INTENT])
    .when(tool_returned(), say="Sorry, I could not make that booking.")
    .when(user_ever_said("table"), BOOK)
    .otherwise(say="How can I help?")
)


def booking(model, **changes) -> Case:
    """A whole scenario in one expression: an agent with nothing of Earful in it, its
    booking declared as an effect, a model written as rules and one user turn."""
    fields = {
        "name": "inline_booking",
        "agent": TalksToItsServiceItself,
        "effects": {"create_reservation": BOOKING},
        "speeches": [UserSpeech(start=0.5, end=2.5, transcript="Book a table for six tonight.")],
        "policy": model,
        "drain_delay": 8.0,
    }
    return Case(**{**fields, **changes})


@pytest.fixture
def pools(monkeypatch):
    """The sizes of the pools of worker processes started while the test runs."""
    started, start = [], scenario.pool

    def counted(workers=None):
        started.append(workers)
        return start(workers)

    monkeypatch.setattr(scenario, "pool", counted)
    return started


# --------------------------------------------------------------------------- one run


def test_a_case_built_in_a_test_runs_and_its_contracts_are_asserted():
    result = run(booking(READS_THE_ERROR))
    result.assert_contracts()
    assert result.report.outcome("A2") == "pass" and result.report.outcome("A3") == "pass"
    assert result.events is result.ledger.events


def test_a_broken_contract_fails_the_test_and_says_which_rule_and_why():
    failing = booking(CONFIRMS_ANYTHING, faults=[Fault("backend.error", 1)])
    result = run(failing)
    with pytest.raises(ContractFailure) as caught:
        result.assert_contracts()
    message = str(caught.value)
    assert message.startswith(f"inline_booking (seed 0, livekit-agents {lk.__version__}) breaks")
    assert "A3/rule" in message and "A3  fail" in message
    assert BOOKING_INTENT in message
    assert isinstance(caught.value, AssertionError)  # a test fails as on any assertion
    result.assert_contracts(allow=["A3/rule"])  # a rule that is known to break may
    result.assert_contracts(allow=[CONFIRMED_BUT_NOT_DONE])


def test_a_ledger_that_is_not_a_valid_run_fails_the_assertion_and_says_why():
    events = run(booking(READS_THE_ERROR)).events[:-1]  # cut off before run.end
    with pytest.raises(ContractFailure, match="not valid evidence.*\n.*run.end"):
        check(events).assert_contracts(what="the truncated run")


def test_the_same_case_run_twice_gives_the_same_ledger_and_is_not_changed_by_running():
    case = booking(READS_THE_ERROR)
    speeches = list(case.speeches)
    first, second = run(case), run(case)
    assert first.hash == second.hash
    assert case.speeches == speeches and case.faults == [] and case.hangup is None


def test_every_run_builds_its_agent_anew():
    built = []

    def agent():
        built.append(TalksToItsServiceItself())
        return built[-1]

    case = booking(READS_THE_ERROR, agent=agent)
    run(case), run(case)
    assert len(built) == 2 and built[0] is not built[1]


def test_a_function_that_returns_a_case_is_called_for_each_run():
    """A policy that keeps state between calls starts again in each run, if the scenario
    is given as the function that builds it."""
    made = []

    def make():
        made.append(booking(READS_THE_ERROR))
        return made[-1]

    assert run(make).hash == run(make).hash
    assert len(made) == 2


def test_a_run_of_a_case_a_search_found_is_run_from_its_data():
    source = booking(CONFIRMS_ANYTHING)
    data = cases.with_fault(Scenario.of(source).default(), "backend.error", 1)
    with pytest.raises(ContractFailure, match="A3/rule"):
        run(source, case=data).assert_contracts()
    run(source).assert_contracts()  # as written it passes


def test_what_is_not_a_scenario_is_refused():
    with pytest.raises(TypeError, match="a Case, a function that returns one"):
        run(42)
    with pytest.raises(ValueError, match="returned NoneType, not a Case"):
        run(lambda: None, case=Scenario.of(booking(READS_THE_ERROR)).default())


# --------------------------------------------------------------------------- names


def test_only_a_function_at_the_top_of_a_module_has_a_name():
    def nested():
        return booking(READS_THE_ERROR)

    assert Scenario.of(barge_in_mid_word).reference == "earful.livekit.cases:barge_in_mid_word"
    assert Scenario.of(as_written_confirms_a_failed_booking).reference == (
        "breaks:as_written_confirms_a_failed_booking"
    )
    assert Scenario.of("breaks:host").reference == "breaks:host"
    for unnamed in (nested, lambda: booking(READS_THE_ERROR), booking(READS_THE_ERROR)):
        found = Scenario.of(unnamed)
        assert found.reference is None and found.name == INLINE
        assert found.default()["scenario"] == INLINE
        assert cases.validate(found.default()) == []


def test_the_data_of_an_inline_case_cannot_be_run_by_name_and_says_what_to_do():
    data = Scenario.of(booking(READS_THE_ERROR)).default()
    with pytest.raises(ValueError, match="built inline.*top level of a module"):
        scenario.run(data)


# --------------------------------------------------------------------------- a search


@pytest.mark.slow
def test_a_case_built_in_a_test_is_searched_in_the_process_of_the_test():
    """Every fault on the one write, with nothing else moved. The model that confirms
    whatever came back is caught; the one that reads the error is not."""
    built = []

    def agent():
        built.append(1)
        return TalksToItsServiceItself()

    found = explore(booking(CONFIRMS_ANYTHING, agent=agent), variables=(), words=False)
    assert found.case["scenario"] == INLINE
    assert len(built) == found.runner.runs  # every run was made here, each with a new agent
    assert found.predicates() == [CONFIRMED_BUT_NOT_DONE]
    with pytest.raises(ContractFailure) as caught:
        found.assert_contracts()
    message = str(caught.value)
    assert message.startswith(f"inline_booking on livekit-agents {lk.__version__}: ")
    assert "A3/rule  in " in message and "changed: backend." in message
    found.assert_contracts(allow=["A3/rule"])
    (one,) = found.breaks()
    assert not one.instants_only and not one.everywhere and one.details
    # The example is a case of this scenario: run from its data, it breaks the same rule.
    with pytest.raises(ContractFailure, match="A3/rule"):
        run(booking(CONFIRMS_ANYTHING), case=one.example).assert_contracts()

    control = explore(booking(READS_THE_ERROR), variables=(), words=False)
    control.assert_contracts()
    assert control.breaks() == []


@pytest.mark.slow
def test_a_search_of_the_same_inline_case_gives_the_same_result_each_time():
    def search():
        found = explore(booking(CONFIRMS_ANYTHING), variables=(), faults=["backend.error"])
        return found.summary(), sorted(t.outcome.hash for t in found.tried)

    assert search() == search()


def test_a_search_does_not_fill_the_log_with_the_frameworks_own_records(caplog):
    """A tool that raises is logged by the framework with its traceback, in every run. A
    single run lets that through; a search of hundreds of runs does not."""
    import logging

    failing = booking(CONFIRMS_ANYTHING, faults=[Fault("backend.error", 1)])
    with caplog.at_level(logging.WARNING):
        run(failing)
        from_one_run = [r for r in caplog.records if r.name.startswith("livekit")]
        caplog.clear()
        found = explore(failing, variables=(), faults=())
        found.runner.ledger(cases.with_value(found.case, cases.Variable("llm.ttft", 1), 250_000))
        from_a_search = [r for r in caplog.records if r.name.startswith("livekit")]
    assert from_one_run and not from_a_search
    assert found.original.failures  # the search ran the failing case all the same
    assert logging.getLogger("livekit").propagate  # and left logging as it found it


def test_a_scenario_with_a_name_runs_in_this_process_unless_workers_are_asked_for(pools):
    found = explore(
        as_written_confirms_a_failed_booking, variables=(), faults=["backend.error"], words=False
    )
    assert found.predicates() == [CONFIRMED_BUT_NOT_DONE] and pools == []
    with pytest.raises(ValueError, match="number of processes"):
        explore(as_written_confirms_a_failed_booking, workers=-1)


def test_an_inline_case_is_refused_by_worker_processes_before_anything_runs():
    built = []

    def agent():
        built.append(1)
        return TalksToItsServiceItself()

    with pytest.raises(ValueError, match="to search in worker processes.*importable"):
        explore(booking(CONFIRMS_ANYTHING, agent=agent), workers=2)
    assert built == []


@pytest.mark.slow
def test_a_failure_of_an_inline_case_is_refused_as_a_bundle_before_anything_runs(tmp_path):
    found = explore(booking(CONFIRMS_ANYTHING), variables=(), faults=["backend.error"])
    runs = found.runner.runs
    with pytest.raises(ValueError, match="to keep a failure as a bundle.*importable"):
        keep(found, "A3/rule", tmp_path / "kept")
    assert found.runner.runs == runs and not (tmp_path / "kept").exists()


@pytest.mark.slow
def test_the_same_scenario_as_a_function_in_a_module_is_kept_and_replays(tmp_path, pools):
    """Given as the function itself, the scenario has a name: it is searched in worker
    processes, and its failure is reduced, saved and replayed from the saved files."""
    found = explore(as_written_confirms_a_failed_booking, variables=(), words=False, workers=4)
    assert found.case["scenario"] == "breaks:as_written_confirms_a_failed_booking"
    assert pools == [4], "the search was not made in worker processes"
    kept = keep(found, "A3/rule", tmp_path / "kept")
    assert kept.predicate == CONFIRMED_BUT_NOT_DONE
    assert [(f["of"], f["nth"]) for f in kept.minimal["faults"]] in (
        [("backend.error", 1)],
        [("backend.drop", 1)],
    )
    again = replay(tmp_path / "kept")
    assert again.reproduced and again.identical and again.scenario_changed is False
    with pytest.raises(ValueError, match="no case that breaks A2/rule"):
        keep(found, "A2/rule", tmp_path / "other")
