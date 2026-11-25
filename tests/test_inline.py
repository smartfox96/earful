"""A scenario built in a test: run and searched in process, refused where it needs a name."""

import hashlib
import os
import sys
from concurrent.futures.process import BrokenProcessPool

import pytest

pytest.importorskip("livekit.agents")

import livekit.agents as lk
from breaks import BOOKING, TalksToItsServiceItself, as_written_confirms_a_failed_booking

from earful.core import case as cases
from earful.core.checker import ContractFailure, check
from earful.core.predicate import Predicate
from earful.core.schema import validity
from earful.core.search import evaluate
from earful.livekit import scenario
from earful.livekit.api import WorkersUnavailable, cross_check, explore, keep, replay, run
from earful.livekit.cases import BOOKING_ACK, BOOKING_INTENT, barge_in_mid_word
from earful.livekit.harness import Case, Delay, Fault, Intent, InvalidRun
from earful.livekit.providers import UserSpeech
from earful.livekit.scenario import INLINE, Scenario
from earful.scripted.playbook import (
    ScriptedModel,
    reply,
    tool_returned,
    tool_succeeded,
    user_ever_said,
)

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
    """An inline scenario: a plain agent, its booking declared as an effect, one user turn."""
    fields = {
        "name": "inline_booking",
        "agent": TalksToItsServiceItself,
        "scripted_turns": True,
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


def test_a_search_nobody_gave_a_number_for_gets_a_worker_per_processor_where_it_can(monkeypatch):
    """Where workers cannot be started, it runs in this process; a number given is held to."""
    monkeypatch.delenv("EARFUL_WORKERS", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 6)
    started = []

    def refused(workers=None):
        started.append(workers)
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(scenario, "pool", refused)
    narrow = {"variables": (), "faults": ["backend.error"], "words": False}
    importable = "breaks:as_written_confirms_a_failed_booking"
    assert explore(importable, **narrow).tried  # judged here, after the pool was refused
    explore(booking(READS_THE_ERROR), **narrow)  # built inline: this process
    monkeypatch.setattr(os, "cpu_count", lambda: 1)  # one worker is no faster than this process
    explore(importable, **narrow)
    monkeypatch.setenv("EARFUL_WORKERS", "3")
    explore(importable, **narrow)
    assert started == [6, 3]
    with pytest.raises(WorkersUnavailable):
        explore(importable, workers=2, **narrow)


def test_a_search_takes_the_wait_the_stretch_and_the_delay_limit_it_is_given():
    narrow = {"variables": (), "faults": ["backend.timeout"], "words": False, "workers": 0}
    found = explore(booking(READS_THE_ERROR), wait_us=2_500_000, **narrow)
    added = [t for t in found.tried if t.candidate.variable is None]  # a fault, nothing moved
    assert added and {f["us"] for t in added for f in t.candidate.case["faults"]} == {2_500_000}
    for name, least in (("wait_us", 0), ("stretch_us", 0), ("delay_limit_us", 1)):
        with pytest.raises(
            ValueError, match=f"{name} is a number of microseconds, at least {least}"
        ):
            explore(booking(READS_THE_ERROR), **{name: least - 1}, **narrow)


def test_a_case_built_in_a_test_runs_and_its_contracts_are_asserted():
    result = run(booking(READS_THE_ERROR))
    result.assert_contracts()
    assert result.report.outcome("A2") == "pass" and result.report.outcome("A3") == "pass"
    assert result.events is result.ledger.events


def test_a_tool_still_running_when_the_scenario_is_over_is_waited_for():
    """A draining session would ask the model to answer the timed-out booking without a tool.
    On livekit-agents 1.5 and 1.6 the agent looks idle during a tool this long."""
    tries_again = (
        ScriptedModel()
        .when(tool_succeeded(), say="Your table is booked.", claims=[BOOKING_INTENT])
        .when(tool_returned(), BOOK)
        .when(user_ever_said("table"), BOOK)
        .otherwise(say="How can I help?")
    )
    slow = booking(tries_again, drain_delay=1.0, faults=[Fault("backend.timeout", 1, 20.0)])
    result = run(slow)
    called = [e["data"]["name"] for e in result.events if e["kind"] == "tool.called"]
    assert called == ["create_reservation", "create_reservation"]
    assert result.report.valid


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
    result.assert_contracts(allow=["A3/rule"])
    result.assert_contracts(allow=[CONFIRMED_BUT_NOT_DONE])


def test_a_model_that_calls_a_tool_the_agent_lacks_gives_a_run_that_is_not_valid_evidence():
    misspelt = (
        ScriptedModel()
        .when(tool_returned(), say="Your table is booked.")
        .when(user_ever_said("table"), reply(BOOKING_ACK, call="create_reservatoin"))
        .otherwise(say="How can I help?")
    )
    with pytest.raises(InvalidRun) as refused:
        run(booking(misspelt))
    (why,) = refused.value.reasons
    assert why == (
        "harness error: call 1: the scripted model called 'create_reservatoin', which the "
        "agent does not offer; it offers ['create_reservation']"
    )
    with pytest.raises(ValueError, match="cannot be searched: .* does not offer; it offers"):
        explore(booking(misspelt))


def test_a_model_that_never_calls_the_tool_fails_a_test_that_requires_the_write():
    only_talks = ScriptedModel().otherwise(say="Your table is booked.")
    result = run(booking(only_talks))
    result.assert_contracts()  # nothing was written, so nothing about a write broke
    with pytest.raises(ContractFailure) as caught:
        result.assert_contracts(require=["A2", "L1"])
    head, why, _ = str(caught.value).split("\n\n", 2)
    assert head == (
        f"inline_booking (seed 0, livekit-agents {lk.__version__}) "
        "has no pass or fail for the required A2"
    )
    assert why == "A2 was not exercised: the run has no write request for a declared intent"
    run(booking(READS_THE_ERROR)).assert_contracts(require=["A2", "A3"])
    with pytest.raises(ValueError, match="no contract rule 'A3'"):
        result.assert_contracts(allow=["A3"])


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        (
            {"speeches": [UserSpeech(start=2.5, end=0.5, transcript="Book a table.")]},
            "the speech at 2.5 s ends at 0.5 s; a speech ends after it starts",
        ),
        (
            {"speeches": [UserSpeech(start=2.5, end=2.5, transcript="Book a table.")]},
            "the speech at 2.5 s ends at 2.5 s; a speech ends after it starts",
        ),
        (
            {"backend_mode": "idempotent"},
            "unknown backend mode 'idempotent'; choose from ['plain', 'keyed']",
        ),
    ],
)
def test_a_case_that_contradicts_itself_is_refused_before_its_agent_is_built(changes, why):
    built = []
    case = booking(READS_THE_ERROR, agent=lambda: built.append(1), **changes)
    with pytest.raises(ValueError) as refused:
        run(case)
    assert str(refused.value) == why and not built
    with pytest.raises(ValueError, match="after it starts|idempotent"):
        explore(case)
    assert not built


def test_an_agent_given_where_what_builds_one_is_expected_is_refused_and_told_what_to_write():
    case = booking(READS_THE_ERROR, agent=TalksToItsServiceItself())
    with pytest.raises(ValueError) as refused:
        run(case)
    assert str(refused.value) == (
        "agent is called once for each run and returns the agent, so it is a class or a "
        "function; got an object of TalksToItsServiceItself. Write "
        "agent=lambda: TalksToItsServiceItself(...)"
    )


def test_a_ledger_that_is_not_a_valid_run_fails_the_assertion_and_says_why():
    events = run(booking(READS_THE_ERROR)).events[:-1]  # cut off before run.end
    with pytest.raises(ContractFailure, match="not valid evidence.*\n.*run.end"):
        check(events).assert_contracts(what="the truncated run")


def test_a_search_calls_a_case_refused_only_if_the_adapter_refused_it_before_running():
    def agent():
        raise ValueError("no table of that size")

    found = Scenario.of(booking(READS_THE_ERROR, agent=agent))
    data = found.default()
    raised = evaluate(found.run, data)
    assert raised.why_not == "error" and raised.error == "ValueError: no table of that size"
    for refused, why in (
        ({**data, "settings": {**data["settings"], "volume": 11}}, "unknown setting 'volume'"),
        ({**data, "contracts": {"A1": {"speed": 1}}}, "A1: unknown policy values"),
    ):
        outcome = evaluate(found.run, refused)
        assert outcome.why_not == "refused" and why in outcome.refused


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
    """A policy that keeps state between calls then starts afresh in each run."""
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
        run(source, data=data).assert_contracts()
    run(source).assert_contracts()


def test_what_is_not_a_scenario_is_refused():
    with pytest.raises(TypeError, match="a Case, a function that returns one"):
        run(42)
    with pytest.raises(ValueError, match="returned NoneType, not a Case"):
        run(lambda: None, data=Scenario.of(booking(READS_THE_ERROR)).default())


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


def test_the_digest_of_a_scenario_is_of_the_one_module_that_defines_its_function(
    tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(tmp_path)
    agent, scenarios = tmp_path / "digest_agent.py", tmp_path / "digest_scenarios.py"
    agent.write_text("NAME = 'first'\n")
    scenarios.write_text("import digest_agent\n\n\ndef booking():\n    return digest_agent.NAME\n")
    (tmp_path / "digest_named_elsewhere.py").write_text("from digest_scenarios import booking\n")
    try:
        before = scenario.fingerprint("digest_scenarios:booking")
        assert before == hashlib.sha256(scenarios.read_bytes()).hexdigest()
        assert scenario.fingerprint("digest_named_elsewhere:booking") == before
        agent.write_text("NAME = 'second, and longer'\n")  # code the scenario imports
        assert scenario.fingerprint("digest_scenarios:booking") == before
        scenarios.write_text(scenarios.read_text() + "# a comment is a change\n")
        assert scenario.fingerprint("digest_scenarios:booking") != before
    finally:
        for name in ("digest_agent", "digest_scenarios", "digest_named_elsewhere"):
            sys.modules.pop(name, None)


def test_the_data_of_an_inline_case_cannot_be_run_by_name_and_says_what_to_do():
    data = Scenario.of(booking(READS_THE_ERROR)).default()
    with pytest.raises(ValueError, match="built inline.*top level of a module"):
        scenario.run(data)


@pytest.mark.slow
def test_a_case_built_in_a_test_is_searched_in_the_process_of_the_test():
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
    with pytest.raises(ContractFailure, match="A3/rule"):
        run(booking(CONFIRMS_ANYTHING), data=one.example).assert_contracts()

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
    """The framework logs a tool that raises, with its traceback, in every run."""
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


def test_a_scenario_with_a_name_runs_in_this_process_when_workers_0_is_asked_for(pools):
    found = explore(
        as_written_confirms_a_failed_booking,
        workers=0,
        variables=(),
        faults=["backend.error"],
        words=False,
    )
    assert found.predicates() == [CONFIRMED_BUT_NOT_DONE] and pools == []
    with pytest.raises(ValueError, match="number of processes"):
        explore(as_written_confirms_a_failed_booking, workers=-1)


def test_what_a_search_is_asked_for_is_checked_before_a_worker_is_started(pools):
    scenario_ = as_written_confirms_a_failed_booking
    with pytest.raises(ValueError, match="unknown variables \\['speech.colour'\\]"):
        explore(scenario_, workers=2, variables=["speech.colour"])
    with pytest.raises(ValueError, match="unknown faults \\['backend.melts'\\]"):
        explore(scenario_, workers=2, faults=["backend.melts"])
    with pytest.raises(ValueError, match="not an earful.case/1 document"):
        explore(scenario_, workers=2, data={"schema": "earful.case/0"})
    assert pools == []


@pytest.mark.parametrize(
    "refusal",
    [PermissionError(13, "Permission denied"), NotImplementedError("no sem_open here")],
    ids=["no permission", "no semaphores"],
)
def test_where_worker_processes_cannot_be_started_the_search_says_so(monkeypatch, refusal):
    """A sandbox or a locked-down container can refuse the semaphores a pool needs."""

    def refused(workers=None):
        raise refusal

    monkeypatch.setattr(scenario, "pool", refused)
    with pytest.raises(WorkersUnavailable, match="cannot be started here.*workers=0") as caught:
        explore(as_written_confirms_a_failed_booking, workers=2, variables=(), words=False)
    assert caught.value.__cause__ is refusal
    found = explore(
        as_written_confirms_a_failed_booking,
        variables=(),
        faults=["backend.error"],
        words=False,
    )
    assert found.predicates() == [CONFIRMED_BUT_NOT_DONE]


def test_a_case_given_as_a_scenario_keeps_its_lists_and_dicts_to_itself():
    case = booking(READS_THE_ERROR, contracts={"L2": {"stop_bound_us": 100_000}})
    make = Scenario.of(case).make
    made = make()
    assert made is not case and made.agent is case.agent and made.policy is case.policy
    made.speeches.append(UserSpeech(start=9.0, end=10.0, transcript="And another."))
    made.intents.append(Intent("refund-1"))
    made.effects["refund"] = BOOKING
    made.contracts["L2"]["stop_bound_us"] = 1
    made.delays.append(Delay("llm.ttft", 1, 0.5))
    made.faults.append(Fault("backend.error", 1))
    made.session_kwargs["allow_interruptions"] = False
    for again in (case, make()):
        assert len(again.speeches) == 1 and again.intents == [] and again.delays == []
        assert list(again.effects) == ["create_reservation"] and again.faults == []
        assert again.contracts == {"L2": {"stop_bound_us": 100_000}}
        assert again.session_kwargs == {}


class _DyingPool:
    """A pool whose worker dies once ``after`` cases have run."""

    def __init__(self, after):
        self.after = after

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def map(self, run, wanted):
        for case in wanted:
            if not self.after:
                raise BrokenProcessPool("A process in the process pool was terminated abruptly")
            self.after -= 1
            yield run(case)


def test_a_worker_that_dies_is_said_to_have_died_before_any_case_or_after_how_many(monkeypatch):
    narrow = {"variables": (), "faults": ["backend.error", "backend.drop"], "words": False}
    monkeypatch.setattr(scenario, "pool", lambda workers=None: _DyingPool(after=0))
    with pytest.raises(WorkersUnavailable) as died:
        explore(as_written_confirms_a_failed_booking, workers=2, **narrow)
    assert str(died.value).startswith("a worker process died before any had run a case. ")
    assert '`if __name__ == "__main__":`' in str(died.value)
    monkeypatch.setattr(scenario, "pool", lambda workers=None: _DyingPool(after=1))
    with pytest.raises(WorkersUnavailable) as died:
        explore(as_written_confirms_a_failed_booking, workers=2, **narrow)
    assert str(died.value).startswith(
        "a worker process died after the workers had run 1 cases. Something in the case it "
        "was running ended the process"
    )
    assert "__main__" not in str(died.value)


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


def test_future_minor_case_metadata_is_ignored_but_unknown_settings_are_refused():
    data = scenario.default("earful.livekit.cases:barge_in_mid_word")
    data.update(schema_minor=17, future="metadata")
    data["speeches"][0]["diagnostic"] = {"future": True}
    assert cases.validate(data) == []
    assert scenario.build(data).speeches[0].transcript == data["speeches"][0]["transcript"]
    data["settings"]["future_execution_setting"] = True
    with pytest.raises(ValueError, match="unknown setting"):
        scenario.build(data)


@pytest.mark.slow
def test_a_scenario_is_run_on_both_clocks_and_the_real_run_is_never_evidence():
    """Agreement depends on the machine, so only what holds on any machine is asserted."""
    from earful.livekit.api import cross_check as on_both_clocks

    checked = on_both_clocks("earful.livekit.cases:speak_over_new_turn", runs=1)
    (real,), (comparison,) = checked.real, checked.comparisons
    assert validity(checked.virtual) == []
    reasons = validity(real)
    assert reasons[0] == "the run was on the real clock"
    # A busy machine delays the loop, and a run it spoiled is one the comparison cannot use.
    assert comparison.unusable == tuple(reasons[1:])
    assert comparison.unusable or comparison.drift_us > 0
    assert checked.lines()[0].startswith("speak_over_new_turn on livekit-agents ")
    with pytest.raises(ValueError, match="at least one real-clock run"):
        on_both_clocks("earful.livekit.cases:speak_over_new_turn", runs=0)


def test_rebuilding_a_saved_case_preserves_the_factorys_defaults():
    case = barge_in_mid_word()
    factory = Scenario.of(lambda: case)
    original = factory.default(7)
    changed = factory.default(7)
    changed["speeches"][0]["transcript"] = "A different question"
    rebuilt = factory.build(changed)
    assert rebuilt.speeches[0].transcript == "A different question"
    assert factory.default(7) == original


@pytest.mark.parametrize("operation", [run, explore, cross_check])
@pytest.mark.parametrize(
    "damage,field", [("list", "object"), ("float", "start_us"), ("seed", "seed")]
)
def test_public_operations_refuse_malformed_saved_cases(operation, damage, field):
    source = "earful.livekit.cases:barge_in_mid_word"
    data = scenario.default(source)
    if damage == "list":
        data = []
    elif damage == "float":
        data["speeches"][0]["start_us"] = 1.0
    else:
        del data["seed"]
    options = {"workers": 0} if operation is explore else {}
    with pytest.raises(ValueError, match=field):
        operation(source, data=data, **options)


@pytest.mark.parametrize(
    "field,value", [("sink_can_pause", 1), ("drain_delay", 1e308), ("drain_delay", 10**400)]
)
def test_direct_runs_refuse_unrepresentable_settings(field, value):
    case = barge_in_mid_word()
    setattr(case, field, value)
    with pytest.raises(ValueError, match=field):
        run(case)


def test_direct_runs_refuse_a_missing_transcript():
    case = barge_in_mid_word()
    case.speeches = [UserSpeech(0, 1, None, expects_reply=True)]
    with pytest.raises(ValueError, match="transcript"):
        run(case)
