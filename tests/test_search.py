"""The boundary scheduler, the shrinker and the bundle, without a framework.

These tests read the committed reference ledgers and use a stand-in for the system under
test: a function that answers a case with one recorded ledger or another. They pin the
search and reduction logic itself; tests/test_search_live.py runs it against a real
session.
"""

import copy
import json
from pathlib import Path

import pytest

from earful.core import bundle
from earful.core import case as cases
from earful.core.case import Variable
from earful.core.checker import check
from earful.core.ledger import ledger_hash, read_jsonl
from earful.core.predicate import Predicate, failures
from earful.core.search import (
    DEFAULT_VARIABLES,
    Handle,
    Runner,
    Transition,
    Window,
    anchors,
    candidates,
    delay_handles,
    evaluate,
    explore,
    speech_handles,
    transitions,
    window,
)
from earful.core.shrink import applied, ddmin, differences, nearest, settled, shrink

GOLDEN = Path(__file__).parent / "golden"
A2 = Predicate("A2", "rule")


def ledger(release, name):
    return read_jsonl(GOLDEN / f"lk-{release}.{name}.ledger.normalized.jsonl")


def speech(start_us, end_us, transcript, **changes):
    return {
        "start_us": start_us,
        "end_us": end_us,
        "transcript": transcript,
        "stt_delay_us": 200_000,
        "expects_reply": None,
        "backchannel": False,
        "retires": [],
        **changes,
    }


def a_case(*speeches, **changes):
    return {
        "schema": "earful.case/3",
        "scenario": "some.module:scenario",
        "seed": 0,
        "speeches": list(speeches),
        "hangup_us": None,
        "delays": [],
        "faults": [],
        "settings": {"tts_ttfb_us": 200_000, "sink_can_pause": True},
        "contracts": {},
        **changes,
    }


BOOK = speech(500_000, 2_500_000, "Book a table for six tonight.")
WINDOW = speech(5_000_000, 6_000_000, "Is the table by the window?")
SECOND = Variable("speech.position", 1)


def answers(fails):
    """A stand-in for the system: a recorded failing ledger where ``fails(case)``, a
    recorded passing one elsewhere. It counts its runs."""
    failing, passing = (
        ledger("1.5.6", "book_then_interrupt"),
        ledger("1.8.12", "book_then_interrupt"),
    )

    def run(case):
        run.runs += 1
        return failing if fails(case) else passing

    run.runs = 0
    return run


def asked_about_the_window_between(lo, hi):
    """Fails when the question about the window starts between ``lo`` and ``hi`` and the
    booking was asked for."""

    def fails(case):
        said = [s["transcript"] for s in case["speeches"]]
        return BOOK["transcript"] in said and any(
            lo <= s["start_us"] <= hi
            for s in case["speeches"]
            if s["transcript"] == WINDOW["transcript"]
        )

    return fails


INTERRUPTS = asked_about_the_window_between(3_000_000, 7_800_000)


# --------------------------------------------------------------------------- the case format


def test_a_case_is_validated_against_its_schema():
    assert cases.validate(a_case(BOOK, WINDOW)) == []
    assert (
        "'seed' is a required property"
        in cases.validate({k: v for k, v in a_case(BOOK).items() if k != "seed"})[0]
    )
    assert "does not match" in cases.validate(a_case(BOOK, scenario="not a reference"))[0]
    assert "must end after it starts" in cases.validate(a_case(speech(2, 2, "x")))[0]
    assert "starts before the speech before it has ended" in cases.validate(a_case(WINDOW, BOOK))[0]
    assert "is not of type" in cases.validate(a_case(BOOK, settings={"tts_ttfb_us": 0.2}))[0]
    assert "is not of type" in cases.validate(a_case(BOOK, settings={"tts_ttfb_us": None}))[0]
    twice = [{"of": "llm.ttft", "nth": 1, "us": 1}, {"of": "llm.ttft", "nth": 1, "us": 2}]
    assert "declared more than once" in cases.validate(a_case(BOOK, delays=twice))[0]
    assert (
        "is not one of"
        in cases.validate(a_case(BOOK, delays=[{"of": "llm.speed", "nth": 1, "us": 1}]))[0]
    )


def test_every_time_in_a_case_is_a_variable_that_moves_on_its_own():
    case = a_case(BOOK, WINDOW)
    assert cases.value(case, SECOND) == 5_000_000
    assert cases.value(case, Variable("speech.end", 1)) == 6_000_000
    assert cases.value(case, Variable("speech.stt_delay", 1)) == 200_000
    assert cases.value(case, Variable("backend.latency", 1)) is None  # left to the scenario's code

    moved = cases.with_value(case, SECOND, 3_140_000)
    assert (moved["speeches"][1]["start_us"], moved["speeches"][1]["end_us"]) == (
        3_140_000,
        4_140_000,
    )  # its length kept
    assert case["speeches"][1]["start_us"] == 5_000_000  # the case given is not changed

    longer = cases.with_value(case, Variable("speech.end", 1), 7_500_000)
    assert (longer["speeches"][1]["start_us"], longer["speeches"][1]["end_us"]) == (
        5_000_000,
        7_500_000,
    )
    slower = cases.with_value(case, Variable("speech.stt_delay", 0), 900_000)
    assert slower["speeches"][0]["stt_delay_us"] == 900_000

    declared = cases.with_value(case, Variable("backend.latency", 2), 1_500_000)
    assert declared["delays"] == [{"of": "backend.latency", "nth": 2, "us": 1_500_000}]
    again = cases.with_value(declared, Variable("backend.latency", 2), 700_000)
    assert again["delays"] == [
        {"of": "backend.latency", "nth": 2, "us": 700_000}
    ]  # replaced, not repeated
    assert cases.without_delay(again, Variable("backend.latency", 2))["delays"] == []
    assert [s["transcript"] for s in cases.without_speech(case, 0)["speeches"]] == [
        WINDOW["transcript"]
    ]


def test_a_value_that_gives_no_case_gives_none():
    case = a_case(BOOK, WINDOW)
    assert cases.with_value(case, SECOND, -1) is None
    assert cases.with_value(case, SECOND, 2_000_000) is None  # inside the other speech
    assert (
        cases.with_value(case, SECOND, 2_500_000) is not None
    )  # touching it is not overlapping it
    assert cases.with_value(case, Variable("speech.end", 1), 5_000_000) is None  # no length left
    # the order of the speeches is part of the scenario: one cannot be moved past another
    assert cases.with_value(case, Variable("speech.position", 0), 7_000_000) is None
    assert cases.with_value(case, SECOND, 0) is None
    assert cases.with_value(case, Variable("llm.ttft", 1), -5) is None


def test_a_predicate_names_a_contract_rule_and_no_instance():
    affected, repaired = (
        check(ledger("1.5.6", "book_then_interrupt")),
        check(ledger("1.8.12", "book_then_interrupt")),
    )
    assert failures(affected) == {A2, Predicate("M2", "rule d")}
    assert A2.holds(affected) and not A2.holds(repaired)
    assert (
        Predicate.parse("L2/rule b") == Predicate("L2", "rule b")
        and str(Predicate("L2", "rule b")) == "L2/rule b"
    )
    with pytest.raises(ValueError, match="CONTRACT/RULE"):
        Predicate.parse("L2")


# --------------------------------------------------------------------------- where to look


def test_transitions_are_the_events_of_a_run_and_the_moments_the_ledger_states():
    events = ledger("1.8.12", "barge_in_mid_word")
    found = transitions(events)
    assert [t.t_us for t in found] == sorted({t.t_us for t in found})
    at = {t.t_us: t.kinds for t in found}
    assert "llm.request" in at[2_700_000]  # recorded
    assert (
        "llm.first_token" in at[2_800_000] and "llm.done" in at[3_100_000]
    )  # 0.1 s and 0.4 s later
    assert "tts.first_audio" in at[3_300_000] and "playout.started" in at[3_300_000]
    assert "user.turn_end" in at[3_000_000]  # the first speech ends at 2.5 s; 0.5 s of silence
    kinds = {kind for t in found for kind in t.kinds}
    assert {"playout.mid_word", "playout.word_end", "sink.pause", "fw.agent_state_changed"} <= kinds
    assert not kinds & {"run.start", "run.end", "history.final", "user.origin"}
    # one per word that played: eight of the long reply before the interruption, two of the
    # short one
    assert sum("playout.word_end" in t.kinds for t in found) == 10
    assert not any("playout.word_end" in t.kinds for t in transitions(events, words=False))


def test_a_speech_acts_at_its_anchors_and_each_variable_moves_some_of_them():
    events = ledger("1.8.12", "barge_in_mid_word")
    header = events[0]["data"]["scenario"]
    assert anchors(header, speech(0, 1_000_000, "Stop.")) == {
        "vad_start": 50_000,
        "interrupt": 500_000,
        "transcript": 1_200_000,
        "vad_end": 1_500_000,
        "turn_end": 1_500_000,
    }
    assert "interrupt" not in anchors(header, speech(0, 300_000, "Wait."))  # too short to interrupt
    assert "transcript" not in anchors(header, speech(0, 1_000_000, ""))  # noise has none

    case = a_case(BOOK, WINDOW)
    handles = speech_handles(case, 1, events, cases.SPEECH_KINDS)
    by = {}
    for handle in handles:
        by.setdefault(handle.variable.kind, {})[handle.moment] = (handle.t_us, handle.value_us)
    assert set(by["speech.position"]) == {
        "vad_start",
        "interrupt",
        "transcript",
        "vad_end",
        "turn_end",
    }
    assert set(by["speech.end"]) == {
        "transcript",
        "vad_end",
        "turn_end",
    }  # its end moves what follows its end
    assert set(by["speech.stt_delay"]) == {"transcript"}
    assert by["speech.position"]["interrupt"] == (
        5_500_000,
        5_000_000,
    )  # when it falls; what the variable is
    assert by["speech.end"]["transcript"] == (6_200_000, 6_000_000)
    assert by["speech.stt_delay"]["transcript"] == (6_200_000, 200_000)


def test_every_delay_a_run_applied_is_a_handle_whether_declared_or_not():
    events = ledger("1.8.12", "book_then_interrupt")
    handles = {
        str(h.variable): (h.t_us, h.value_us) for h in delay_handles(events, cases.DELAY_KINDS)
    }
    assert handles["llm.duration 1"] == (3_000_000, 300_000)  # asked at 2.7 s, took 0.3 s
    assert handles["llm.ttft 1"] == (2_800_000, 100_000)
    assert handles["backend.latency 1"] == (3_000_000, 0)  # the reference tool commits at once
    assert handles["tts.ttfb 2"] == (7_000_000, 200_000)
    assert set(handles) == {
        f"{kind} {n}" for kind in ("llm.ttft", "llm.duration", "tts.ttfb") for n in (1, 2)
    } | {"backend.latency 1"}
    assert [str(h.variable) for h in delay_handles(events, ["backend.latency"])] == [
        "backend.latency 1"
    ]


def test_every_candidate_puts_a_handle_before_at_or_after_a_transition():
    events = ledger("1.8.12", "book_then_interrupt")
    case = a_case(BOOK, WINDOW)
    targets = transitions(events, words=False)
    handles = speech_handles(case, 1, events, ["speech.position"]) + delay_handles(
        events, ["backend.latency"]
    )
    found, impossible = candidates(case, handles, targets)
    assert found and impossible
    for candidate in found:
        assert cases.validate(candidate.case) == []
        assert cases.value(candidate.case, candidate.variable) == candidate.value_us
        for placement in candidate.placements:
            handle = placement.handle
            assert (
                candidate.value_us - handle.value_us
                == placement.transition.t_us + placement.offset_us - handle.t_us
            )
    assert {p.offset_us for c in found for p in c.placements} == {-10_000, 0, 10_000}
    assert len({cases.canonical(c.case) for c in found}) == len(
        found
    )  # cases that coincide are one
    # the commit cannot come before its request, and a speech cannot sit inside another
    assert all(c.value_us >= 0 for c in found)
    assert any(
        p.handle.variable.kind == "backend.latency" and p.transition.t_us < 3_000_000
        for p in impossible
    )
    assert any(p.handle.variable.kind == "speech.position" for p in impossible)


def test_a_search_never_makes_a_provider_slower_than_its_limit():
    events = ledger("1.8.12", "book_then_interrupt")
    handles = delay_handles(events, ["backend.latency"])
    late = [Transition(60_000_000, ("playout.started",))]
    found, impossible = candidates(a_case(BOOK, WINDOW), handles, late)
    assert not found and len(impossible) == 3
    found, _ = candidates(a_case(BOOK, WINDOW), handles, late, delay_limit_us=90_000_000)
    assert sorted(c.value_us for c in found) == [56_990_000, 57_000_000, 57_010_000]


def test_a_caller_is_never_made_to_wait_longer_than_the_limit():
    dropped = cases.with_fault(a_case(BOOK, WINDOW), "backend.drop", 1, 1_000_000)
    gave_up = [Handle(Variable("backend.wait", 1), "gave_up", 4_000_000, 1_000_000)]
    late = [Transition(60_000_000, ("playout.started",))]
    found, impossible = candidates(dropped, gave_up, late)
    assert not found and len(impossible) == 3
    found, _ = candidates(dropped, gave_up, late, delay_limit_us=90_000_000)
    assert sorted(c.value_us for c in found) == [56_990_000, 57_000_000, 57_010_000]
    assert all(
        c.case["faults"] == [{"of": "backend.drop", "nth": 1, "us": c.value_us}] for c in found
    )


# --------------------------------------------------------------------------- running


def test_a_runner_runs_each_case_once_and_one_bad_run_does_not_end_a_search():
    def run(case):
        run.calls += 1
        first = case["speeches"][0]["start_us"]
        if first == 1:
            raise ValueError("too close together")
        if first == 2:
            raise RuntimeError("deadlock")
        return ledger("1.5.6" if first == 3 else "1.8.12", "book_then_interrupt")

    run.calls = 0
    runner = Runner(run)
    wanted = [a_case(speech(n, 1_000_000, "x")) for n in (1, 2, 3, 4, 3)]
    refused, raised, broke, passed, again = runner.many(wanted)
    assert refused.refused == "too close together" and refused.why_not == "refused"
    assert raised.error == "RuntimeError: deadlock" and raised.why_not == "error"
    assert broke.failures == (A2, Predicate("M2", "rule d")) and broke.ran and passed.failures == ()
    assert again is broke and run.calls == 4 == runner.runs  # the fifth was the third again
    assert runner.breaks(wanted[2], A2) and not runner.breaks(wanted[0], A2) and run.calls == 4
    invalid = ledger("1.8.12", "book_then_interrupt")
    invalid[-1]["data"]["underruns"] = 1
    assert evaluate(lambda case: invalid, wanted[0]).why_not == "invalid"


def test_exploring_runs_every_candidate_and_reports_what_broke_and_what_was_reached():
    run = answers(INTERRUPTS)
    far = a_case(BOOK, speech(20_000_000, 21_000_000, WINDOW["transcript"]))
    found = explore(
        far, run, variables=["speech.position"], speeches=[1], moments=["interrupt"], words=False
    )
    assert found.original.failures == ()
    assert found.predicates() == [A2, Predicate("M2", "rule d")] and not found.everywhere(A2)
    assert all(3_000_000 <= t.candidate.value_us <= 7_800_000 for t in found.breaking(A2))
    summary = found.summary()
    assert (
        summary["candidates"] == summary["ran"] == len(found.tried)
        and summary["runs"] == found.runner.runs
    )
    assert summary["found"]["A2/rule"]["changed"] == ["speech.position 1"]
    reached = found.coverage()["speech.position 1 interrupt"]
    assert {"playout.started", "tool.returned", "llm.done"} <= set(reached["reached"])
    # the interruption point cannot be put where the speech would lie inside the other one
    assert summary["impossible_placements"] == len(found.impossible) > 0
    assert all(p.transition.t_us < 3_500_000 for p in found.impossible)


@pytest.mark.slow
def test_a_rule_that_breaks_wherever_things_are_put_is_not_about_timing():
    found = explore(
        a_case(BOOK, WINDOW),
        answers(lambda case: True),
        variables=["speech.position"],
        moments=["interrupt"],
        words=False,
    )
    assert found.everywhere(A2) and found.summary()["found"]["A2/rule"]["everywhere"]


def test_what_cannot_be_run_is_counted_and_said():
    def run(case):
        """Raises whenever the question would interrupt as the tool returns, at 3.0 s."""
        moved = case["speeches"][-1]["start_us"]
        if len(case["speeches"]) == 2 and 2_490_000 <= moved <= 2_510_000:
            raise RuntimeError("boom")
        return ledger("1.8.12", "book_then_interrupt")

    far = a_case(BOOK, speech(20_000_000, 21_000_000, WINDOW["transcript"]))
    found = explore(
        far, run, variables=["speech.position"], speeches=[1], moments=["interrupt"], words=False
    )
    summary = found.summary()
    assert (
        summary["error"] == 2 and summary["ran"] > 0 and summary["errors"] == ["RuntimeError: boom"]
    )
    assert summary["error"] + summary["ran"] == summary["candidates"]
    # the tool's return was never reached, and the report says why: 10 ms before it the
    # question would lie inside the booking; at it and after it the run raised
    never = found.coverage()["speech.position 1 interrupt"]["never_reached"]
    assert never["tool.returned"] == ["error", "impossible"]
    assert "playout.started" not in never


def test_a_case_that_cannot_be_searched_is_refused_up_front():
    run = answers(lambda case: False)
    with pytest.raises(ValueError, match="not an earful.case/3 document"):
        explore({**a_case(BOOK), "schema": "x"}, run)
    with pytest.raises(ValueError, match="unknown variables"):
        explore(a_case(BOOK), run, variables=["speech.volume"])
    with pytest.raises(ValueError, match="cannot be searched: too close"):
        explore(a_case(BOOK), lambda case: (_ for _ in ()).throw(ValueError("too close")))
    with pytest.raises(ValueError, match="cannot be searched: RuntimeError: deadlock"):
        explore(a_case(BOOK), lambda case: (_ for _ in ()).throw(RuntimeError("deadlock")))
    nothing = explore(
        a_case(), run, variables=["speech.position", "backend.latency"], faults=[], words=False
    )  # no speeches to move
    assert nothing.predicates() == [] and all(
        t.candidate.variable.kind in cases.DELAY_KINDS for t in nothing.tried
    )


# --------------------------------------------------------------------------- windows


# --------------------------------------------------------------------------- faults


def fault(of, nth=1, us=None):
    return {"of": of, "nth": nth, "us": us}


def has_fault(of, nth=1):
    return lambda case: any(f["of"] == of and f["nth"] == nth for f in case["faults"])


def test_a_fault_is_data_for_one_write():
    case = a_case(BOOK)
    assert (
        cases.validate(
            a_case(BOOK, faults=[fault("backend.error"), fault("backend.drop", 2, 500_000)])
        )
        == []
    )
    assert (
        "more than one fault"
        in cases.validate(
            a_case(BOOK, faults=[fault("backend.error"), fault("backend.drop", 1, 5)])
        )[0]
    )
    assert (
        "how long the caller waits"
        in cases.validate(a_case(BOOK, faults=[fault("backend.timeout")]))[0]
    )
    assert (
        "an error does not"
        in cases.validate(a_case(BOOK, faults=[fault("backend.error", 1, 5)]))[0]
    )
    assert "is not one of" in cases.validate(a_case(BOOK, faults=[fault("backend.explode")]))[0]

    dropped = cases.with_fault(case, "backend.drop", 1, 500_000)
    assert dropped["faults"] == [fault("backend.drop", 1, 500_000)] and case["faults"] == []
    assert cases.with_fault(dropped, "backend.error", 1) is None  # that write has a fault already
    assert (
        cases.with_fault(case, "backend.timeout", 1) is None
    )  # a timeout says how long the caller waits

    wait = Variable("backend.wait", 1)
    assert cases.value(dropped, wait) == 500_000 and cases.value(case, wait) is None
    assert cases.with_value(dropped, wait, 750_000)["faults"] == [fault("backend.drop", 1, 750_000)]
    assert cases.with_value(case, wait, 750_000) is None  # no fault, nothing waits
    assert (
        cases.with_value(cases.with_fault(case, "backend.error", 1), wait, 750_000) is None
    )  # an error has no wait


@pytest.mark.slow
def test_a_search_adds_each_fault_to_each_write_the_run_made():
    run = answers(has_fault("backend.drop"))
    found = explore(a_case(BOOK, WINDOW), run, variables=())
    by_base = {
        t.candidate.base: t.candidate.case for t in found.tried if t.candidate.variable is None
    }
    assert (
        set(by_base)
        == {t.candidate.base for t in found.tried}
        == {
            "backend.error 1",
            "backend.drop 1",
            "backend.timeout 1, commit first",
            "backend.timeout 1, commit later",
        }
    )
    assert by_base["backend.error 1"]["faults"] == [fault("backend.error")]
    assert by_base["backend.drop 1"]["faults"] == [fault("backend.drop", 1, 1_000_000)]
    # A timeout in both orders: the write as the scenario has it, and committing after the
    # caller gave up.
    assert by_base["backend.timeout 1, commit first"]["delays"] == []
    assert by_base["backend.timeout 1, commit later"]["delays"] == [
        {"of": "backend.latency", "nth": 1, "us": 2_000_000}
    ]
    assert found.predicates() == [Predicate("A2", "rule"), Predicate("M2", "rule d")]
    assert found.summary()["found"]["A2/rule"]["changed"] == ["backend.drop 1"]
    # A break that moved nothing has no window to measure.
    assert found.windows(Predicate("A2", "rule")) == []

    # A write as slow as the wait commits later as written. The other order has the caller
    # wait longer.
    slow = explore(a_case(BOOK, WINDOW), run, variables=(), faults=["backend.timeout"], wait_us=0)
    orders = {
        t.candidate.base: t.candidate.case for t in slow.tried if t.candidate.variable is None
    }
    assert orders["backend.timeout 1, commit later"]["faults"] == [fault("backend.timeout", 1, 0)]
    assert orders["backend.timeout 1, commit first"]["faults"] == [
        fault("backend.timeout", 1, 1_000_000)
    ]
    assert all(case["delays"] == [] for case in orders.values())

    assert {
        t.candidate.base
        for t in explore(a_case(BOOK, WINDOW), run, variables=(), faults=["backend.error"]).tried
    } == {"backend.error 1"}
    assert explore(a_case(BOOK, WINDOW), run, variables=(), faults=[]).tried == []
    with pytest.raises(ValueError, match="unknown faults"):
        explore(a_case(BOOK, WINDOW), run, faults=["backend.explode"])


def test_a_search_runs_no_case_twice():
    """The case as given, the runs that candidates are placed against (the case without
    each speech, with each fault, under each profile) and the candidates all go through
    one runner, which runs each distinct case once and keeps the ledgers a search needs."""
    failing = answers(has_fault("backend.drop"))
    ran = []

    def run(case):
        ran.append(cases.canonical(case))
        return failing(case)

    author = a_case(BOOK, WINDOW)
    found = explore(
        author,
        run,
        variables=["speech.position"],
        moments=["interrupt"],
        pairs=True,
        profiles=1,
        words=False,
    )
    assert len(ran) == len(set(ran)) == found.runner.runs
    assert cases.canonical(author) in ran  # the case as given is one of them, once
    assert found.runner.ledger(author) is not None  # kept: asking again runs nothing
    assert len(ran) == len(set(ran)) == found.runner.runs


def test_a_write_that_already_has_a_fault_is_given_no_other():
    run = answers(lambda case: False)
    found = explore(a_case(BOOK, WINDOW, faults=[fault("backend.error")]), run, variables=())
    assert found.tried == []


@pytest.mark.slow
def test_pairs_put_faults_on_two_writes_and_move_speeches_under_each_fault():
    two_writes = answers(lambda case: True)  # the recorded failing run made two writes
    alone = explore(
        a_case(BOOK, WINDOW), two_writes, variables=(), faults=["backend.error", "backend.drop"]
    )
    paired = explore(
        a_case(BOOK, WINDOW),
        two_writes,
        variables=(),
        faults=["backend.error", "backend.drop"],
        pairs=True,
    )
    assert len(alone.tried) == 4
    both = [
        t.candidate.case["faults"] for t in paired.tried if len(t.candidate.case["faults"]) == 2
    ]
    assert len(both) == 4 and all({f["nth"] for f in faults} == {1, 2} for faults in both)

    # A timeout that commits later is a fault and a latency. Paired, it keeps both.
    timeouts = explore(
        a_case(BOOK, WINDOW), two_writes, variables=(), faults=["backend.timeout"], pairs=True
    )
    later = {
        t.candidate.base: t.candidate.case for t in timeouts.tried if t.candidate.variable is None
    }
    both_later = later["backend.timeout 1, commit later + backend.timeout 2, commit later"]
    assert both_later["delays"] == [
        {"of": "backend.latency", "nth": n, "us": 2_000_000} for n in (1, 2)
    ]
    assert later["backend.timeout 1, commit first + backend.timeout 2, commit later"]["delays"] == [
        {"of": "backend.latency", "nth": 2, "us": 2_000_000}
    ]

    moving = explore(
        a_case(BOOK, WINDOW),
        answers(lambda case: False),
        variables=["speech.position"],
        speeches=[1],
        moments=["interrupt"],
        faults=["backend.error"],
        pairs=True,
        words=False,
    )
    under = [
        t.candidate
        for t in moving.tried
        if t.candidate.base == "backend.error 1" and t.candidate.variable == SECOND
    ]
    assert under and all(
        c.case["faults"] == [fault("backend.error")]
        and c.what == "backend.error 1 + speech.position 1"
        for c in under
    )
    assert any(not t.candidate.base and t.candidate.variable == SECOND for t in moving.tried)


@pytest.mark.slow
def test_profiles_redraw_every_delay_from_the_seed_and_move_speeches_under_each():
    def searched(seed=0, profiles=2):
        return explore(
            a_case(BOOK, WINDOW, seed=seed),
            answers(lambda case: False),
            variables=["speech.position"],
            speeches=[1],
            moments=["interrupt"],
            faults=[],
            profiles=profiles,
            words=False,
        )

    first = searched()
    assert [cases.canonical(t.candidate.case) for t in first.tried] == [
        cases.canonical(t.candidate.case) for t in searched().tried
    ]
    profile = {
        t.candidate.base: t.candidate.case["delays"]
        for t in first.tried
        if t.candidate.variable is None
    }
    assert (
        set(profile) == {"profile 1", "profile 2"} and profile["profile 1"] != profile["profile 2"]
    )
    assert all(
        0 <= d["us"] <= 10_000_000 and d["us"] % 10_000 == 0
        for delays in profile.values()
        for d in delays
    )
    assert any(
        t.candidate.base == "profile 1" and t.candidate.variable == SECOND for t in first.tried
    )
    other = {
        t.candidate.base: t.candidate.case["delays"]
        for t in searched(seed=1).tried
        if t.candidate.variable is None
    }
    assert other["profile 1"] != profile["profile 1"]
    assert not any(t.candidate.base for t in searched(profiles=0).tried)


@pytest.mark.slow
def test_a_fault_is_a_difference_and_a_reduction_keeps_it_only_if_the_failure_needs_it():
    author = a_case(BOOK, WINDOW)
    found = cases.with_fault(
        cases.with_value(author, SECOND, 3_140_000), "backend.drop", 1, 700_000
    )
    found = {**found, "delays": [{"of": "llm.ttft", "nth": 1, "us": 400_000}]}
    listed = differences(author, found)
    assert "backend.drop on backend write 1, the caller waiting 700000 us" in [
        d.label for d in listed
    ]
    assert applied(author, listed) == found
    assert applied(author, [d for d in listed if d.target[0] != "fault"])["faults"] == []

    needs_both = lambda case: has_fault("backend.drop")(case) and len(case["speeches"]) == 2
    shrunk = shrink(found, answers(needs_both), Predicate("A2", "rule"), author=author)
    assert shrunk.case == {**author, "faults": [fault("backend.drop", 1, 700_000)]}
    assert shrunk.differences == ["backend.drop on backend write 1, the caller waiting 700000 us"]
    # The failure does not depend on how long the caller waits, so the wait is left alone.
    assert [
        (str(w.variable), w.lo_end, w.hi_end)
        for w in shrunk.windows
        if w.variable.kind == "backend.wait"
    ] == [("backend.wait 1", "blocked", "reach")]

    # A fault the author declared is removed if the failure does not need it.
    faulted = {**author, "faults": [fault("backend.error")]}
    assert (
        shrink(faulted, answers(INTERRUPTS), Predicate("A2", "rule"), author=faulted).case["faults"]
        == []
    )


HANGUP = Variable("user.hangup", 0)


def left_between(lo_us, hi_us):
    """Fails where the user hangs up in a stretch of time, and nowhere else."""
    return lambda case: case["hangup_us"] is not None and lo_us <= case["hangup_us"] <= hi_us


def test_a_hangup_is_a_time_in_the_case_no_earlier_than_the_end_of_the_last_speech():
    case = a_case(BOOK, WINDOW)
    assert cases.value(case, HANGUP) is None  # the user stays
    left = cases.with_value(case, HANGUP, 8_000_000)
    assert left["hangup_us"] == 8_000_000 and case["hangup_us"] is None
    assert cases.with_value(case, HANGUP, 6_000_000) is not None  # as the last speech ends
    assert cases.with_value(case, HANGUP, 5_999_999) is None  # before it is over
    assert "before their last speech is over" in cases.validate({**case, "hangup_us": 3_000_000})[0]
    assert cases.with_value(left, SECOND, 7_500_000) is None  # a speech cannot be moved past it
    assert (
        "never speaks" in cases.validate(a_case(hangup_us=1_000_000))[0]
    )  # the origin is the user's first audio
    assert cases.without_hangup(left) == case


@pytest.mark.slow
def test_a_search_moves_the_hangup_to_every_transition_after_the_last_speech():
    run = answers(left_between(7_000_000, 9_000_000))
    found = explore(a_case(BOOK, WINDOW), run, variables=["user.hangup"], faults=[], words=False)
    assert found.tried and all(t.candidate.variable == HANGUP for t in found.tried)
    assert min(t.candidate.value_us for t in found.tried) >= WINDOW["end_us"]
    # Before the last speech is over there is no such case: counted, and not run.
    assert any(p.handle.variable == HANGUP for p in found.impossible)
    breaking = found.breaking(A2)
    assert breaking and all(7_000_000 <= t.candidate.value_us <= 9_000_000 for t in breaking)
    (measured,) = found.windows(A2)
    assert (measured.variable, measured.lo_us, measured.hi_us) == (HANGUP, 7_000_000, 9_000_000)
    # The hangup is one of the default variables, and is left alone by a search that names others.
    assert "user.hangup" in DEFAULT_VARIABLES
    unasked = explore(
        a_case(BOOK, WINDOW), run, variables=["speech.position"], faults=[], words=False
    )
    assert not [t for t in unasked.tried if t.candidate.variable == HANGUP]


def test_pairs_move_the_hangup_with_each_fault_in_place():
    run = answers(lambda case: False)
    found = explore(
        a_case(BOOK, WINDOW),
        run,
        variables=["user.hangup"],
        faults=["backend.error"],
        pairs=True,
        words=False,
    )
    under = [
        t.candidate
        for t in found.tried
        if t.candidate.base == "backend.error 1" and t.candidate.variable == HANGUP
    ]
    assert under and all(
        c.case["faults"] == [fault("backend.error")] and c.case["hangup_us"] is not None
        for c in under
    )
    assert {c.what for c in under} == {"backend.error 1 + user.hangup 0"}


def test_a_hangup_the_case_declares_is_moved_against_the_run_in_which_the_user_stays():
    seen = []

    def run(case):
        seen.append(case["hangup_us"])
        return ledger("1.8.12", "book_then_interrupt")

    leaving = a_case(BOOK, WINDOW, hangup_us=6_500_000)
    found = explore(leaving, run, variables=["user.hangup"], faults=[])
    assert None in seen  # the run without the hangup supplies the transitions
    assert (
        max(t.candidate.value_us for t in found.tried) > 6_500_000
    )  # so it can be moved later, too


@pytest.mark.slow
def test_a_hangup_is_a_difference_and_a_reduction_keeps_it_only_if_the_failure_needs_it():
    author = a_case(BOOK, WINDOW)
    found = cases.with_value(cases.with_value(author, SECOND, 3_140_000), HANGUP, 8_130_000)
    listed = differences(author, found)
    assert "the user hangs up at 8130000 us" in [d.label for d in listed]
    assert applied(author, listed) == found

    # Needed: kept, and settled on a whole second in the middle of its window.
    needs_both = lambda case: (
        left_between(7_000_000, 9_000_000)(case) and len(case["speeches"]) == 2
    )
    shrunk = shrink(found, answers(needs_both), A2, author=author)
    assert shrunk.case == {**author, "hangup_us": 8_000_000}
    assert shrunk.differences == ["the user hangs up at 8000000 us"]
    assert [(w.lo_us, w.hi_us) for w in shrunk.windows if w.variable == HANGUP] == [
        (7_000_000, 9_000_000)
    ]

    # Not needed: the reduced case has a user who stays.
    assert shrink(found, answers(INTERRUPTS), A2, author=author).case["hangup_us"] is None
    # A hangup the author declared is removed if the failure does not need it.
    leaving = {**author, "hangup_us": 9_000_000}
    reduced = shrink(leaving, answers(lambda case: True), A2, author=leaving)
    assert reduced.case["hangup_us"] is None and "removed the hangup" in reduced.steps


# --------------------------------------------------------------------------- a stretched delay

SLOW_WRITE = {"of": "backend.latency", "nth": 1, "us": 1_500_000}


def slow_write_and(fails):
    """Fails where the first write is declared to take 1.5 s and ``fails`` holds too."""
    return lambda case: SLOW_WRITE in case["delays"] and fails(case)


def test_pairs_stretch_each_write_and_each_generation_shorter_than_the_stretch():
    run = answers(lambda case: False)

    def versions(**options):
        """The label and the declared delays of each candidate, in a search that moves
        nothing: the stretched versions of the case and no others."""
        found = explore(a_case(BOOK, WINDOW), run, variables=(), faults=[], **options)
        assert all(t.candidate.variable is None for t in found.tried)
        return {t.candidate.base: t.candidate.case["delays"] for t in found.tried}

    def stretched(us, *delays):
        return {f"{of} {nth}, stretched": [{"of": of, "nth": nth, "us": us}] for of, nth in delays}

    # The recorded run made one write, which commits at once, and two model calls of 0.3 s.
    every = (("backend.latency", 1), ("llm.duration", 1), ("llm.duration", 2))
    assert versions(pairs=True) == stretched(1_500_000, *every)
    assert versions() == {}
    # A delay already as long as the stretch is left as it is, and is not paired.
    assert versions(pairs=True, stretch_us=300_000) == stretched(300_000, ("backend.latency", 1))
    assert versions(pairs=True, stretch_us=0) == {}
    # No delay is stretched past the limit.
    assert versions(pairs=True, delay_limit_us=1_000_000) == stretched(1_000_000, *every)


def test_pairs_find_a_failure_that_needs_a_slow_write_and_a_speech_during_it():
    run = answers(slow_write_and(INTERRUPTS))
    far = a_case(BOOK, speech(20_000_000, 21_000_000, WINDOW["transcript"]))
    options = {
        "variables": ["speech.position", "backend.latency"],
        "speeches": [1],
        "moments": ["interrupt"],
        "faults": [],
        "words": False,
    }
    # One thing at a time: the write is made slow and the question is moved, never both.
    assert explore(far, run, **options).predicates() == []
    found = explore(far, run, pairs=True, **options)
    breaking = found.breaking(A2)
    assert {t.candidate.what for t in breaking} == {
        "backend.latency 1, stretched + speech.position 1"
    }
    assert all(t.candidate.case["delays"] == [SLOW_WRITE] for t in breaking)
    # The stretched version alone passes, and is a candidate like any other.
    (version,) = [t for t in found.tried if t.candidate.what == "backend.latency 1, stretched"]
    assert version.candidate.case == {**far, "delays": [SLOW_WRITE]}
    assert version.candidate.variable is None and not version.outcome.failures
    # The window is measured along the question, with the write still slow.
    (measured,) = found.windows(A2)
    assert (measured.variable, measured.lo_us, measured.hi_us) == (SECOND, 3_000_000, 7_800_000)


def test_pairs_find_a_failure_that_needs_a_slow_write_and_a_hangup_during_it():
    run = answers(slow_write_and(left_between(7_000_000, 9_000_000)))
    author = a_case(BOOK, WINDOW)
    options = {"variables": ["user.hangup", "backend.latency"], "faults": [], "words": False}
    assert explore(author, run, **options).predicates() == []
    found = explore(author, run, pairs=True, **options)
    breaking = found.breaking(A2)
    assert {t.candidate.what for t in breaking} == {"backend.latency 1, stretched + user.hangup 0"}
    assert all(t.candidate.case["delays"] == [SLOW_WRITE] for t in breaking)
    (measured,) = found.windows(A2)
    assert (measured.variable, measured.lo_us, measured.hi_us) == (HANGUP, 7_000_000, 9_000_000)


def test_under_a_stretched_delay_a_handle_is_put_at_each_transition_and_at_no_word_end():
    found = explore(
        a_case(BOOK, WINDOW),
        answers(lambda case: False),
        variables=["speech.position", "user.hangup"],
        speeches=[1],
        moments=["interrupt"],
        faults=[],
        pairs=True,
    )

    def placed(base):
        """What was moved under ``base``, at which offsets, and whether to a word end."""
        placements = [
            (t.candidate.variable, placement)
            for t in found.tried
            if t.candidate.base == base and t.candidate.variable is not None
            for placement in t.candidate.placements
        ]
        return (
            {variable for variable, _ in placements},
            {placement.offset_us for _, placement in placements},
            any("playout.word_end" in placement.transition.kinds for _, placement in placements),
        )

    assert placed("") == ({SECOND, HANGUP}, {-10_000, 0, 10_000}, True)
    for label in ("backend.latency 1, stretched", "llm.duration 2, stretched"):
        assert placed(label) == ({SECOND, HANGUP}, {0}, False)


def test_a_case_found_under_a_stretched_delay_reduces_to_the_delay_and_the_hangup():
    """The recorded write is requested at 3.0 s. The stand-in fails where the user hangs
    up while a write that was declared slow is still under way."""

    def left_during_the_write(case):
        took = cases.value(case, Variable("backend.latency", 1))
        left = case["hangup_us"]
        return took is not None and left is not None and 3_000_000 <= left < 3_000_000 + took

    author = a_case(BOOK)
    run = answers(left_during_the_write)
    found = explore(author, run, variables=["user.hangup"], faults=[], pairs=True, words=False)
    assert {t.candidate.what for t in found.breaking(A2)} == {
        "backend.latency 1, stretched + user.hangup 0"
    }
    case = nearest(author, [t.candidate.case for t in found.breaking(A2)])
    padded = {**cases.with_value(case, Variable("llm.ttft", 2), 250_000), "seed": 7}
    shrunk = shrink(padded, run, A2, author=author)
    assert shrunk.complete
    assert shrunk.steps[0] == "kept 2 of 4 differences from the author's scenario"
    # Each time is settled inside its window: the write, then the hangup during it.
    assert shrunk.case == {
        **author,
        "hangup_us": 3_500_000,
        "delays": [{"of": "backend.latency", "nth": 1, "us": 1_000_000}],
    }
    assert shrunk.differences == [
        "the user hangs up at 3500000 us",
        "backend.latency 1 declared as 1000000 us",
    ]
    assert run(shrunk.case) == ledger("1.5.6", "book_then_interrupt")  # it still fails
    for undone in (cases.without_hangup(shrunk.case), {**shrunk.case, "delays": []}):
        assert run(undone) == ledger("1.8.12", "book_then_interrupt")  # and needs both


def test_a_window_is_found_by_doubling_out_and_halving_back():
    run = answers(asked_about_the_window_between(3_000_000, 7_800_000))
    runner = Runner(run)
    found = window(runner, a_case(BOOK, WINDOW), SECOND, A2)
    assert (found.lo_us, found.hi_us) == (3_000_000, 7_800_000)
    assert (
        (found.lo_end, found.hi_end) == ("passes", "passes")
        and not found.instant
        and found.resolution_us == 1_000
    )
    assert runner.runs < 60  # a 4.8 s window, located to a millisecond at each end
    assert found.to_json()["width_us"] == 4_800_000


def test_a_window_narrower_than_a_millisecond_is_located_to_the_microsecond():
    narrow = window(
        Runner(answers(asked_about_the_window_between(4_999_800, 5_000_300))),
        a_case(BOOK, WINDOW),
        SECOND,
        A2,
    )
    assert (narrow.lo_us, narrow.hi_us, narrow.resolution_us) == (
        4_999_800,
        5_000_300,
        1,
    ) and not narrow.instant
    tie = window(
        Runner(answers(asked_about_the_window_between(5_000_000, 5_000_000))),
        a_case(BOOK, WINDOW),
        SECOND,
        A2,
    )
    assert tie.instant and tie.width_us == 0 and tie.to_json()["instant"]


def test_each_end_of_a_window_says_why_the_search_stopped_there():
    """Fails however late the question comes: upwards the search gives up at its reach.
    Fails however early: downwards it is blocked by the speech before it, which it cannot
    enter. Neither is an edge of the failure."""
    always = window(
        Runner(answers(asked_about_the_window_between(0, 10**12))),
        a_case(BOOK, WINDOW),
        SECOND,
        A2,
        reach_us=8_000_000,
    )
    assert (always.lo_end, always.hi_end) == ("blocked", "reach") and not always.instant
    assert always.lo_us == 2_500_000  # touching the first speech
    assert (
        5_000_000 + 8_000_000 <= always.hi_us <= 5_000_000 + 16_000_000
    )  # it stopped at its reach
    assert always.to_json()["lo_end"] == "blocked"
    # blocked at the very first step: the question already touches the booking
    touching = window(
        Runner(answers(asked_about_the_window_between(0, 10**12))),
        a_case(BOOK, speech(2_500_000, 3_500_000, WINDOW["transcript"])),
        SECOND,
        A2,
        reach_us=1_000_000,
    )
    assert (touching.lo_us, touching.lo_end) == (2_500_000, "blocked")


def test_a_window_needs_a_case_that_breaks_and_a_variable_it_has():
    runner = Runner(answers(INTERRUPTS))
    with pytest.raises(ValueError, match="does not break"):
        window(
            runner, a_case(BOOK, speech(20_000_000, 21_000_000, WINDOW["transcript"])), SECOND, A2
        )
    with pytest.raises(ValueError, match="does not break"):
        window(
            runner, a_case(BOOK, WINDOW), Variable("backend.latency", 1), A2
        )  # not declared in the case


# --------------------------------------------------------------------------- reducing


def test_ddmin_finds_a_one_minimal_subset():
    assert ddmin(range(8), lambda kept: 2 in kept and 6 in kept) == [2, 6]
    assert ddmin(range(8), lambda kept: 5 in kept) == [5]
    assert ddmin(range(8), lambda kept: True) == []
    assert ddmin([1, 2, 3], lambda kept: kept == [1, 2, 3]) == [1, 2, 3]
    # nothing at all fails, though neither half does: the answer is nothing
    assert ddmin([1, 2], lambda kept: kept in ([], [1, 2])) == []
    tried = []
    assert ddmin(list("abcdefgh"), lambda kept: tried.append(kept) or {"a", "h"} <= set(kept)) == [
        "a",
        "h",
    ]
    assert len(tried) < 40  # far fewer than the 256 subsets
    with pytest.raises(ValueError, match="failing start"):
        ddmin(range(3), lambda kept: False)


AUTHOR = a_case(
    BOOK, speech(20_000_000, 21_000_000, WINDOW["transcript"])
)  # as written, the question comes late
FOUND = a_case(
    BOOK,
    speech(3_140_000, 4_270_000, WINDOW["transcript"], stt_delay_us=350_000, backchannel=True),
    speech(9_010_000, 9_600_000, ""),
    speech(12_000_000, 12_900_000, "Thanks."),
    delays=[{"of": "llm.duration", "nth": 2, "us": 450_000}],
    settings={"tts_ttfb_us": 250_000, "sink_can_pause": False},
    contracts={"L1": {"slack_us": 2_000_000}},
    seed=7,
)


def test_the_differences_between_two_cases_can_each_be_undone_alone():
    found = differences(AUTHOR, FOUND)
    labels = [d.label for d in found]
    assert labels == [
        "speech 'Is the table by the window?' moved to 3140000 us",
        "speech 'Is the table by the window?' lasting 1130000 us",
        "speech 'Is the table by the window?' with stt_delay_us 350000",
        "speech 'Is the table by the window?' with backchannel True",
        "speech '(noise)' added at 9010000 us",
        "speech 'Thanks.' added at 12000000 us",
        "llm.duration 2 declared as 450000 us",
        "setting sink_can_pause = False",
        "setting tts_ttfb_us = 250000",
        "contract policy of L1 changed",
        "seed 7",
    ]
    assert applied(AUTHOR, found) == FOUND and applied(AUTHOR, []) == AUTHOR
    only_moved = applied(AUTHOR, found[:1])
    assert differences(AUTHOR, only_moved) == found[:1]
    assert (only_moved["speeches"][1]["start_us"], only_moved["speeches"][1]["end_us"]) == (
        3_140_000,
        4_140_000,
    )
    dropped = differences(a_case(BOOK, WINDOW), a_case(WINDOW))
    assert [d.label for d in dropped] == ["speech 'Book a table for six tonight.' dropped"]
    assert applied(a_case(BOOK, WINDOW), dropped) == a_case(WINDOW)
    assert differences(AUTHOR, AUTHOR) == []


def test_shrinking_keeps_only_the_differences_the_failure_needs_and_settles_them():
    run = answers(INTERRUPTS)
    shrunk = shrink(FOUND, run, A2, author=AUTHOR)
    assert shrunk.complete and shrunk.runs == run.runs
    assert shrunk.steps[0] == "kept 1 of 11 differences from the author's scenario"
    # the author's scenario with the question moved, and nothing else
    assert {**shrunk.case, "speeches": AUTHOR["speeches"]} == AUTHOR
    moved = shrunk.case["speeches"][1]
    assert shrunk.case["speeches"][0] == BOOK and moved["transcript"] == WINDOW["transcript"]
    # in the middle of the 3.0-7.8 s window, on a whole second
    assert (moved["start_us"], moved["end_us"]) == (5_000_000, 6_000_000)
    assert shrunk.differences == ["speech 'Is the table by the window?' moved to 5000000 us"]
    assert shrunk.steps[-1] == "speech.position 1: settled at 5000000 us, inside 3000000..7800000"
    by = {str(w.variable): (w.lo_us, w.hi_us) for w in shrunk.windows}
    assert by["speech.position 1"] == (3_000_000, 7_800_000)
    assert "speech.position 0" in by  # how far the speech left where the author put it can move
    assert FOUND["seed"] == 7  # the case given is not changed


def test_shrinking_removes_a_speech_of_the_authors_that_the_failure_does_not_need():
    def fails(case):  # the booking plays no part
        return any(
            3_000_000 <= s["start_us"] <= 7_800_000
            for s in case["speeches"]
            if s["transcript"] == WINDOW["transcript"]
        )

    shrunk = shrink(FOUND, answers(fails), A2, author=AUTHOR)
    assert [s["transcript"] for s in shrunk.case["speeches"]] == [WINDOW["transcript"]]
    assert "removed 1 of 2 speeches" in shrunk.steps
    assert "speech 'Book a table for six tonight.' dropped" in shrunk.differences


def test_a_case_whose_authors_scenario_fails_as_written_reduces_to_it():
    shrunk = shrink(
        FOUND,
        answers(lambda case: BOOK["transcript"] in [s["transcript"] for s in case["speeches"]]),
        A2,
        author=AUTHOR,
    )
    assert shrunk.steps[0] == "kept 0 of 11 differences from the author's scenario"
    assert shrunk.differences == [
        "speech 'Is the table by the window?' dropped"
    ]  # nor is the question needed


def test_a_settled_time_is_the_roundest_in_the_middle_half_of_its_window():
    def at(lo, hi, lo_end="passes", hi_end="passes", was=None):
        return settled(Window(SECOND, lo, hi, lo_end, hi_end, 1_000), lo if was is None else was)

    assert at(3_000_000, 7_800_000) == 5_000_000  # a whole second, nearest the centre
    assert at(3_140_000, 3_260_000) == 3_200_000  # a tenth of a second
    assert (
        at(3_141_000, 3_148_000) == 3_144_000
    )  # a millisecond, when that is all there is room for
    assert at(3_240_000, 3_300_000) == 3_270_000  # never an edge: 3.3 s is rounder, and is the edge
    assert at(0, 1_140_000, "blocked", "blocked") == 500_000  # a blocked end is an end all the same
    assert at(5_000_000, 5_000_000) is None  # an instant has no inside
    # an end the search gave up on is no edge: just inside the one that is
    assert at(1_200_000, 31_200_000, hi_end="reach") == 2_000_000
    assert at(0, 2_500_000, lo_end="reach") == 2_000_000
    assert (
        at(0, 60_000_000, "reach", "reach", was=5_030_000) == 5_000_000
    )  # no edge at all: where it was, rounded
    for lo, hi in ((3_000_000, 7_800_000), (3_140_000, 3_260_000), (3_240_000, 3_300_000)):
        assert at(lo, hi, was=at(lo, hi)) == at(lo, hi)  # settling what is settled changes nothing


@pytest.mark.slow
def test_shrinking_without_an_authors_scenario_removes_and_settles():
    shrunk = shrink(FOUND, answers(INTERRUPTS), A2)
    assert [s["transcript"] for s in shrunk.case["speeches"]] == [
        BOOK["transcript"],
        WINDOW["transcript"],
    ]
    assert (
        shrunk.case["delays"] == [] and shrunk.case["settings"] == FOUND["settings"]
    )  # nothing to restore them to
    assert shrunk.differences == []
    assert shrunk.case["speeches"][1]["start_us"] == 5_000_000
    assert shrunk.case["speeches"][0] == BOOK  # the failure has no edge along it: it stays as found
    moved = shrunk.case["speeches"][1]
    assert moved["end_us"] - moved["start_us"] == 1_130_000  # nor along the length of the question


def test_shrinking_needs_a_failing_case_and_says_when_its_budget_ran_out():
    with pytest.raises(ValueError, match="does not break A2/rule"):
        shrink(a_case(BOOK), answers(lambda case: False), A2)
    run = answers(INTERRUPTS)
    stopped = shrink(FOUND, run, A2, author=AUTHOR, max_runs=3)
    assert not stopped.complete and stopped.runs == 3 == run.runs
    assert A2.holds(check(run(stopped.case)))  # what it returns still fails


def test_the_case_to_reduce_is_the_one_nearest_the_authors():
    near = applied(AUTHOR, differences(AUTHOR, FOUND)[:1])
    nearer = cases.with_value(AUTHOR, SECOND, 19_000_000)
    assert nearest(AUTHOR, [FOUND, near]) == near  # fewer differences
    assert nearest(AUTHOR, [near, nearer]) == nearer  # as many, moved less


# --------------------------------------------------------------------------- keeping


def test_a_bundle_holds_the_failure_and_replays(tmp_path):
    run = answers(INTERRUPTS)
    manifest = bundle.capture(
        tmp_path / "kept",
        FOUND,
        run,
        A2,
        author=AUTHOR,
        environment={"python": "3"},
        scenario_digest="a" * 64,
    )
    assert bundle.validate(manifest) == []
    assert manifest["predicate"] == {"contract": "A2", "rule": "rule"}
    assert (
        manifest["framework"] == {"name": "livekit-agents", "version": "1.5.6"}
        and manifest["clock"] == "virtual"
    )
    assert manifest["formats"] == {
        "ledger": "earful.ledger/5",
        "contracts": "earful.contracts/1",
        "case": "earful.case/3",
    }
    assert (manifest["original"]["speeches"], manifest["minimal"]["speeches"]) == (4, 2)
    assert (manifest["original"]["delays"], manifest["minimal"]["delays"]) == (1, 0)
    assert (
        manifest["original"]["findings"]
        == manifest["minimal"]["findings"]
        == ["2 durable effects for 'book-table-1'; the scenario authorized 1"]
    )
    assert manifest["reduction"]["complete"] and len(manifest["reduction"]["differences"]) == 1
    assert [w["variable"] for w in manifest["windows"]] == [
        "speech.position 1",
        "speech.position 0",
    ]
    assert manifest["replay"] == {"repeats": 3, "distinct_hashes": 1}
    assert sorted(p.name for p in (tmp_path / "kept").iterdir()) == [
        "manifest.json",
        "minimal.case.json",
        "minimal.ledger.jsonl",
        "original.case.json",
        "original.ledger.jsonl",
    ]

    kept = bundle.load(tmp_path / "kept")
    assert kept.predicate == A2 and len(kept.minimal["speeches"]) == 2 and kept.original == FOUND
    assert ledger_hash(kept.ledger()) == manifest["minimal"]["hash"]
    again = bundle.replay(kept, run, fingerprint=lambda reference: "a" * 64)
    assert (
        again.reproduced
        and again.repeatable
        and again.identical
        and again.scenario_changed is False
    )
    assert bundle.replay(kept, run).scenario_changed is None  # nothing to compare it with
    assert bundle.replay(kept, run, fingerprint=lambda reference: "b" * 64).scenario_changed is True
    repaired = bundle.replay(kept, answers(lambda case: False))  # a release that repaired it
    assert not repaired.reproduced and repaired.repeatable and not repaired.identical
    assert repaired.framework["version"] == "1.8.12"


def test_a_case_that_does_not_repeat_is_not_kept(tmp_path):
    def unsteady(case):
        unsteady.runs += 1
        events = copy.deepcopy(ledger("1.5.6", "book_then_interrupt"))
        events[5]["t_us"] += unsteady.runs % 2  # a different ledger every other run
        return events

    unsteady.runs = 0
    with pytest.raises(ValueError, match="different ledgers"):
        bundle.capture(tmp_path / "kept", a_case(BOOK, WINDOW), unsteady, A2)
    assert not (tmp_path / "kept").exists()


def test_a_bundle_that_is_not_one_is_refused(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"schema": "something/1"}))
    with pytest.raises(ValueError, match="not an earful.bundle/2 manifest"):
        bundle.load(tmp_path)


@pytest.mark.slow
def test_a_manifest_cannot_point_outside_its_bundle(tmp_path):
    """The file names are fixed. A manifest that tries to name another is not a manifest."""
    bundle.capture(tmp_path / "kept", a_case(BOOK, WINDOW), answers(INTERRUPTS), A2)
    path = tmp_path / "kept" / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["minimal"]["case"] = "../elsewhere.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="'case' was unexpected"):
        bundle.load(tmp_path / "kept")


@pytest.mark.parametrize(
    "directory", sorted((Path(__file__).parent / "bundles").iterdir()), ids=lambda d: d.name
)
def test_every_reference_bundle_is_what_its_manifest_says(directory):
    kept = bundle.load(directory)
    for which, case in (("original", kept.original), ("minimal", kept.minimal)):
        events = kept.ledger(which)
        assert ledger_hash(events) == kept.manifest[which]["hash"]
        assert len(case["speeches"]) == kept.manifest[which]["speeches"]
        assert kept.predicate.holds(check(events))  # the recorded run breaks the frozen rule
        assert events[0]["data"]["framework"] == kept.manifest["framework"]
    assert kept.manifest["reduction"]["complete"] and kept.manifest["windows"]
    assert not any(w["instant"] for w in kept.manifest["windows"])  # neither sits on a tie
