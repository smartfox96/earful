"""Tests of the boundary scheduler, the shrinker and the bundle, without a framework."""

import copy
import json
import shutil
from pathlib import Path

import pytest

from earful.core import bundle
from earful.core import case as cases
from earful.core.case import Variable
from earful.core.checker import ContractFailure, check
from earful.core.ledger import ledger_hash, read_jsonl
from earful.core.predicate import Predicate, failures
from earful.core.search import (
    DEFAULT_VARIABLES,
    Candidate,
    CaseRefused,
    Exploration,
    Handle,
    Runner,
    Spent,
    Transition,
    Tried,
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
        "schema": "earful.case/1",
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
    """A stand-in system: the failing ledger where ``fails(case)``, the passing one elsewhere."""
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
    def fails(case):
        said = [s["transcript"] for s in case["speeches"]]
        return BOOK["transcript"] in said and any(
            lo <= s["start_us"] <= hi
            for s in case["speeches"]
            if s["transcript"] == WINDOW["transcript"]
        )

    return fails


INTERRUPTS = asked_about_the_window_between(3_000_000, 7_800_000)


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
    )
    assert case["speeches"][1]["start_us"] == 5_000_000

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
    assert again["delays"] == [{"of": "backend.latency", "nth": 2, "us": 700_000}]
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
    assert cases.with_value(case, Variable("speech.end", 1), 5_000_000) is None
    # speeches keep their order: one cannot be moved past another
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
    for nothing in ("L2", "L2/rule z", "Z9/rule"):
        with pytest.raises(ValueError, match="no contract rule .* CONTRACT/RULE, one of: A1/rule"):
            Predicate.parse(nothing)


def test_transitions_are_the_events_of_a_run_and_the_moments_the_ledger_states():
    events = ledger("1.8.12", "barge_in_mid_word")
    found = transitions(events)
    assert [t.t_us for t in found] == sorted({t.t_us for t in found})
    at = {t.t_us: t.kinds for t in found}
    assert "llm.request" in at[2_700_000]
    assert (
        "llm.first_token" in at[2_800_000] and "llm.done" in at[3_100_000]
    )  # 0.1 s and 0.4 s later
    assert "tts.first_audio" in at[3_200_000] and "playout.started" in at[3_200_000]
    assert "user.turn_end" in at[3_000_000]  # the first speech ends at 2.5 s; 0.5 s of silence
    kinds = {kind for t in found for kind in t.kinds}
    assert {"playout.mid_word", "playout.word_end", "sink.pause", "fw.agent_state_changed"} <= kinds
    assert not kinds & {"run.start", "run.end", "history.final", "user.origin"}
    # nine words of the long reply played before the interruption, two of the short one
    assert sum("playout.word_end" in t.kinds for t in found) == 11
    assert not any("playout.word_end" in t.kinds for t in transitions(events, words=False))


def test_a_turn_ends_when_the_header_says_it_does_and_as_livekit_ends_it_where_it_says_nothing():
    events = copy.deepcopy(ledger("1.8.12", "barge_in_mid_word"))
    header = events[0]["data"]["scenario"]
    session, vad = header["session"], header["providers"]["vad"]
    stop = speech(0, 1_000_000, "Stop.")
    # A header from before the field: the longer of the VAD's silence and the endpointing delay.
    session.pop("turn_end_delay_us", None)
    for silence_us, delay_us in ((500_000, 800_000), (800_000, 300_000)):
        vad["min_silence_us"], session["min_endpointing_delay_us"] = silence_us, delay_us
        assert anchors(header, stop)["turn_end"] == 1_800_000
    # Pipecat's turn ends after both, one after the other, and its header says so.
    session["turn_end_delay_us"] = 1_100_000
    assert anchors(header, stop)["turn_end"] == 2_100_000
    assert anchors(header, stop)["vad_end"] == 1_800_000
    at = {t.t_us: t.kinds for t in transitions(events)}
    assert "user.turn_end" in at[2_500_000 + 1_100_000]  # the first speech ends at 2.5 s


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
    assert "transcript" not in anchors(header, speech(0, 1_000_000, ""))

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
    }
    assert set(by["speech.stt_delay"]) == {"transcript"}
    assert by["speech.position"]["interrupt"] == (
        5_500_000,
        5_000_000,
    )
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
    assert handles["tts.ttfb 2"] == (6_900_000, 200_000)
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
    assert len({cases.canonical(c.case) for c in found}) == len(found)
    # the commit cannot come before its request, and a speech cannot sit inside another
    assert all(c.value_us >= 0 for c in found)
    assert any(
        p.handle.variable.kind == "backend.latency" and p.transition.t_us < 3_000_000
        for p in impossible
    )
    assert any(p.handle.variable.kind == "speech.position" for p in impossible)


def test_a_handle_that_sits_on_a_transition_of_another_run_is_put_either_side_of_it():
    events = ledger("1.8.12", "book_then_interrupt")
    case = a_case(BOOK, WINDOW)
    (interrupt,) = [
        h for h in speech_handles(case, 1, events, ["speech.position"]) if h.moment == "interrupt"
    ]
    on_it = [Transition(interrupt.t_us, ("tool.returned",))]
    found, impossible = candidates(case, [interrupt], on_it, apart=True)
    assert sorted(c.value_us - interrupt.value_us for c in found) == [-10_000, 10_000]
    assert [p.offset_us for c in found for p in c.placements] == [-10_000, 10_000]
    assert impossible == []
    # In a run that holds the handle, a transition at its instant is the handle: no target.
    assert candidates(case, [interrupt], on_it) == ([], [])


def test_a_speech_or_a_hangup_written_on_a_transition_is_moved_to_either_side_of_it():
    """The tool returns at 3.0 s, where the second speech as written could first interrupt."""
    on_the_return = a_case(BOOK, speech(2_500_000, 3_500_000, WINDOW["transcript"]))
    just_after = answers(lambda case: case["speeches"][-1]["start_us"] == 2_510_000)
    found = explore(
        on_the_return,
        just_after,
        variables=["speech.position"],
        speeches=[1],
        moments=["interrupt"],
        words=False,
    )
    assert [t.candidate.value_us for t in found.breaking(A2)] == [2_510_000]
    # 10 ms before it the speech would lie inside the booking.
    assert (3_000_000, -10_000) in {(p.transition.t_us, p.offset_us) for p in found.impossible}

    stays = ledger("1.8.12", "book_then_interrupt")
    at = next(t.t_us for t in transitions(stays, words=False) if t.t_us > 7_000_000)
    leaving = explore(
        a_case(BOOK, WINDOW, hangup_us=at), lambda case: stays, variables=["user.hangup"], faults=[]
    )
    assert {at - 10_000, at + 10_000} <= {t.candidate.value_us for t in leaving.tried}


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


def test_a_runner_runs_each_case_once_and_one_bad_run_does_not_end_a_search():
    def run(case):
        run.calls += 1
        first = case["speeches"][0]["start_us"]
        if first == 1:
            raise CaseRefused("too close together")
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
    # Only the adapter's own refusal is one: a ValueError a run raises is an error like any other.
    mistaken = evaluate(lambda case: int("six"), wanted[0])
    assert mistaken.why_not == "error" and mistaken.refused is None
    assert mistaken.error == "ValueError: invalid literal for int() with base 10: 'six'"


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
    # the interruption point cannot go where the question would lie inside the booking
    assert summary["impossible_placements"] == len(found.impossible) > 0
    assert all(p.transition.t_us < 3_500_000 for p in found.impossible)


def test_a_search_requires_a_contract_of_the_case_as_given():
    """The stand-in books a table only in the case as given: no moved case exercises A2."""
    booked, greeted = (
        ledger("1.8.12", "book_then_interrupt"),
        ledger("1.8.12", "barge_in_mid_word"),
    )
    case = a_case(BOOK, WINDOW)
    found = explore(
        case,
        lambda moved: booked if moved == case else greeted,
        variables=["speech.position"],
        speeches=[1],
        moments=["interrupt"],
        words=False,
    )
    assert found.tried and all(t.outcome.outcomes["A2"] == "not_exercised" for t in found.tried)
    found.assert_contracts(require=["A2"])
    assert list(found.unjudged(["A2", "A1"])) == ["A1"]
    with pytest.raises(ContractFailure) as caught:
        found.assert_contracts(require=["A2", "A1"])
    head, required = str(caught.value).split("\n\n")
    tried = len(found.tried)
    summary = f"{tried} cases tried, {tried} judged, 0 contract rule(s) broken"
    read, ran = found.runner.by_purpose["read"], found.runner.by_purpose["cases"]
    assert ran < tried  # a case already run to read the boundaries is not run again
    assert head.splitlines() == [
        f"book_then_interrupt on livekit-agents 1.8.12: {summary}",
        f"    {read + ran} run(s): {read} to find where to put things, {ran} for the cases",
        "    by contract: pass, fail, nothing to judge, evidence lost",
        *(f"    {name}  {0:>5} {0:>5} {tried:>5} {0:>5}" for name in ("A1", "A2", "A3")),
        *(f"    {name}  {tried:>5} {0:>5} {0:>5} {0:>5}" for name in ("M1", "M2", "L1", "L2")),
    ]
    assert found.summary()["by_contract"]["A1"] == {
        "pass": 0,
        "fail": 0,
        "not_exercised": tried,
        "unobservable": 0,
    }
    assert required == (
        "required, with no pass or fail in the case as given:\n"
        "    A1 was not exercised: the run has no speech that retires an intent and whose "
        "transcript was delivered"
    )
    with pytest.raises(ValueError, match="no contract rule 'A3'"):
        found.assert_contracts(allow=["A3"])
    with pytest.raises(ValueError, match="no contract 'A9'"):
        found.assert_contracts(require=["A9"])


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
    assert found.errored == 2 and not found.nothing_judged
    with pytest.raises(ContractFailure) as caught:  # no rule broke, and two runs raised
        found.assert_contracts()
    assert str(caught.value) == found.describe()
    assert found.describe().endswith(
        f"\n\nnot judged: 2 of {summary['candidates']} cases; the search fails, since 2 raised\n"
        "    2 error: RuntimeError: boom\n"
        "        such as speech.position 1 at 2.500 s (as written 20.000 s), "
        "to put interrupt at llm.done"
    )
    # before the return the question lies inside the booking; at and after it the run raised
    never = found.coverage()["speech.position 1 interrupt"]["never_reached"]
    assert never["tool.returned"] == ["error", "impossible"]
    assert "playout.started" not in never


def test_cases_that_were_refused_or_not_valid_evidence_are_said_and_do_not_fail_a_search():
    def run(case):
        moved = case["speeches"][-1]["start_us"]
        if len(case["speeches"]) == 2 and moved == 2_510_000:
            raise CaseRefused("the speeches are too close together")
        events = ledger("1.8.12", "book_then_interrupt")
        if len(case["speeches"]) == 2 and moved == 2_500_000:
            events[-1]["data"]["underruns"] = 1
        return events

    far = a_case(BOOK, speech(20_000_000, 21_000_000, WINDOW["transcript"]))
    found = explore(
        far, run, variables=["speech.position"], speeches=[1], moments=["interrupt"], words=False
    )
    summary = found.summary()
    assert (summary["refused"], summary["invalid"], summary["error"]) == (1, 1, 0)
    assert found.errored == 0 and summary["ran"] == summary["candidates"] - 2
    found.assert_contracts()
    assert f"\n\nnot judged: 2 of {summary['candidates']} cases\n" in found.describe()


def test_a_search_that_could_judge_nothing_it_tried_fails_and_says_why_with_an_example():
    case = a_case(BOOK, speech(20_000_000, 21_000_000, WINDOW["transcript"]))

    def run(moved):
        """Only the case as given, and the booking on its own, can be judged."""
        if moved == case or len(moved["speeches"]) == 1:
            return ledger("1.8.12", "book_then_interrupt")
        at = moved["speeches"][1]["start_us"]
        if at < 5_000_000:
            raise CaseRefused("the speeches are too close together")
        invalid = ledger("1.8.12", "book_then_interrupt")
        invalid[-1]["data"]["underruns"] = at // 1_000_000
        return invalid

    found = explore(
        case,
        run,
        variables=["speech.position"],
        speeches=[1],
        moments=["interrupt"],
        words=False,
        faults=[],
    )
    assert found.nothing_judged and found.summary()["ran"] == 0 and found.predicates() == []
    late, early = found.not_judged()  # runs that differ only in a number are one reason
    assert (late.why, early.why) == ("invalid", "refused") and late.cases > early.cases
    assert found.errored == 0  # no case raised: the search fails for having judged none
    assert late.cases + early.cases == len(found.tried)
    moved = "speech.position 1 at {} s (as written 20.000 s), to put interrupt at {}"
    assert found.summary()["not_judged"][1] == {
        "why": "refused",
        "reason": "the speeches are too close together",
        "cases": early.cases,
        "example": moved.format("2.500", "llm.done"),
    }
    with pytest.raises(ContractFailure) as caught:
        found.assert_contracts()
    head, said = str(caught.value).split("\n\n")
    tried = len(found.tried)
    first, runs = head.splitlines()
    assert first.endswith(f": {tried} cases tried, 0 judged, 0 contract rule(s) broken")
    assert runs == f"    {tried + 2} run(s): 2 to find where to put things, {tried} for the cases"
    assert said.splitlines() == [
        f"not judged: {tried} of {tried} cases, which leaves the search with no result",
        f"    {late.cases} invalid: playout ran ahead of its audio 5 times",
        "        such as " + moved.format("5.000", "sink.pause"),
        f"    {early.cases} refused: the speeches are too close together",
        "        such as " + moved.format("2.500", "llm.done"),
    ]


def test_a_rule_the_case_breaks_as_given_is_found_where_no_case_tried_breaks_it():
    case = a_case(BOOK, WINDOW)
    options = {"speeches": [1], "moments": ["interrupt"], "words": False}
    found = explore(
        case, answers(lambda moved: moved == case), variables=["speech.position"], **options
    )
    assert found.tried and found.breaking(A2) == [] and A2 in found.predicates()
    one = found.breaks()[0]
    assert (one.predicate, one.candidates, one.example) == (A2, 0, case)
    assert one.what == "nothing: it breaks as written" and not one.everywhere
    with pytest.raises(ContractFailure, match="A2/rule  in 0 case"):
        found.assert_contracts()
    alone = explore(case, answers(lambda moved: True), variables=[], faults=[])
    assert alone.tried == [] and alone.nothing_judged
    with pytest.raises(ContractFailure, match="0 cases tried, 0 judged, 2 contract rule"):
        alone.assert_contracts()


def test_a_search_that_tried_no_case_fails():
    case = a_case(BOOK, WINDOW)
    clean = explore(case, answers(lambda moved: False), variables=[], faults=[])
    assert clean.tried == [] and clean.nothing_judged and clean.no_verdict
    with pytest.raises(ContractFailure) as caught:
        clean.assert_contracts()
    head, said = str(caught.value).split("\n\n")
    assert head.endswith(
        ": 0 cases tried, 0 judged, 0 contract rule(s) broken\n"
        "    1 run(s): 1 to find where to put things"
    )
    assert said == "no case was tried, which leaves the search with no result"


def test_a_run_made_to_find_where_to_put_a_speech_that_is_not_valid_fails_the_search():
    """Without the question the run is invalid, so the question is never moved."""
    case = a_case(BOOK, speech(20_000_000, 21_000_000, WINDOW["transcript"]))

    def run(moved):
        events = ledger("1.8.12", "book_then_interrupt")
        if [s["transcript"] for s in moved["speeches"]] == [BOOK["transcript"]]:
            events[-1]["data"]["underruns"] = 3
        return events

    found = explore(case, run, variables=["speech.position"], words=False, faults=[])
    (lost,) = found.unread
    assert (lost.run, lost.outcome.why_not) == ("the case as given, without speech 1", "invalid")
    assert found.unreadable == 1 and found.no_verdict
    assert found.tried and not found.nothing_judged and found.errored == 0
    assert {t.candidate.variable for t in found.tried} == {Variable("speech.position", 0)}
    assert found.summary()["not_read"] == [
        {
            "run": "the case as given, without speech 1",
            "why": "invalid",
            "reason": "playout ran ahead of its audio 3 times",
        }
    ]
    with pytest.raises(ContractFailure) as caught:
        found.assert_contracts()
    assert str(caught.value).endswith(
        "\n\nnot read: 1 run(s) made to find where to put things could not be judged, so the "
        "cases they would place were not tried; the search fails\n"
        "    the case as given, without speech 1: invalid: playout ran ahead of its audio 3 times"
    )


@pytest.mark.parametrize(
    ("raised", "fails"), [(RuntimeError("boom"), True), (CaseRefused("no hangup here"), False)]
)
def test_a_run_made_to_find_where_to_put_the_hangup_that_raises_fails_the_search(raised, fails):
    """A refused run is said and does not fail the search, as a refused case does not."""
    case = a_case(BOOK, WINDOW, hangup_us=30_000_000)

    def run(moved):
        if moved["hangup_us"] is None:
            raise raised
        return ledger("1.8.12", "book_then_interrupt")

    found = explore(
        case, run, variables=["speech.position", cases.HANGUP], speeches=[1], words=False, faults=[]
    )
    (lost,) = found.unread
    assert lost.run == "the case as given, without the hangup" and lost.fails is fails
    assert found.unreadable == fails and found.no_verdict is fails and found.errored == 0
    assert all(t.candidate.variable.kind != cases.HANGUP for t in found.tried)
    why = "error: RuntimeError: boom" if fails else "refused: no hangup here"
    assert found.describe().endswith(
        "were not tried" + "; the search fails" * fails + f"\n    {lost.run}: {why}"
    )
    if not fails:
        found.assert_contracts()


def test_a_run_made_to_find_where_to_put_things_under_a_fault_is_named_by_the_fault():
    case = a_case(BOOK, WINDOW)

    def run(moved):
        if moved["faults"]:
            raise RuntimeError("the reservation service is down")
        return ledger("1.8.12", "book_then_interrupt")

    found = explore(case, run, variables=[], faults=["backend.error"])
    assert [(one.run, one.outcome.why_not) for one in found.unread] == [
        ("backend.error 1", "error")
    ]


def test_speeches_moments_and_profiles_are_checked_before_anything_runs():
    run = answers(lambda case: False)
    with pytest.raises(ValueError, match=r"^no speech 'x', 2: the case has 2, numbered from 0$"):
        explore(a_case(BOOK, WINDOW), run, speeches=[2, "x", 1])
    with pytest.raises(ValueError, match=r"no speech -1: the case has 2"):
        explore(a_case(BOOK, WINDOW), run, speeches=[-1])
    moments = "'interrupt', 'transcript', 'turn_end', 'vad_end', 'vad_start'"
    with pytest.raises(ValueError, match=rf"^unknown moments 'start'; choose from \[{moments}\]$"):
        explore(a_case(BOOK, WINDOW), run, moments=["start", "interrupt"])
    for profiles in (-1, 1.5, True):
        with pytest.raises(ValueError, match="profiles is a number of profiles, 0 for none"):
            explore(a_case(BOOK, WINDOW), run, profiles=profiles)
    assert run.runs == 0


def _blind(events):
    """The run as if the tool had written outside Earful's backend."""
    from test_checker import seal

    return seal([e for e in copy.deepcopy(events) if not e["kind"].startswith("effect.")])


def test_evidence_a_case_loses_is_a_rule_of_its_own_with_its_cases_and_what_changed():
    clean = ledger("1.8.12", "book_then_interrupt")
    blind = _blind(clean)

    def run(case):
        """The write leaves the backend once the question comes before 4 s."""
        moved = case["speeches"][1:]
        return blind if moved and moved[0]["start_us"] < 4_000_000 else clean

    found = explore(
        a_case(BOOK, WINDOW),
        run,
        variables=["speech.position"],
        speeches=[1],
        moments=["interrupt"],
        words=False,
        faults=[],
    )
    lost = {str(one.predicate): one for one in found.breaks()}
    assert set(lost) == {"A2/outside-backend", "A3/outside-backend"}
    a2 = lost["A2/outside-backend"]
    assert a2.details == ("a tool for the intent was called but nothing reached the backend",)
    assert a2.candidates == found.by_contract()["A2"]["unobservable"] > 0
    assert a2.what.startswith("speech.position 1 at") and not a2.everywhere
    with pytest.raises(ContractFailure, match="A2/outside-backend  in"):
        found.assert_contracts()
    found.assert_contracts(allow=["A2/outside-backend", "A3/outside-backend"])


def test_a_case_that_cannot_be_searched_is_refused_up_front():
    run = answers(lambda case: False)
    with pytest.raises(ValueError, match="not an earful.case/1 document"):
        explore({**a_case(BOOK), "schema": "x"}, run)
    with pytest.raises(ValueError, match="unknown variables"):
        explore(a_case(BOOK), run, variables=["speech.volume"])
    with pytest.raises(ValueError, match="cannot be searched: too close"):
        explore(a_case(BOOK), lambda case: (_ for _ in ()).throw(CaseRefused("too close")))
    with pytest.raises(ValueError, match="cannot be searched: RuntimeError: deadlock"):
        explore(a_case(BOOK), lambda case: (_ for _ in ()).throw(RuntimeError("deadlock")))
    nothing = explore(
        a_case(), run, variables=["speech.position", "backend.latency"], faults=[], words=False
    )
    assert nothing.predicates() == [] and all(
        t.candidate.variable.kind in cases.DELAY_KINDS for t in nothing.tried
    )


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
    assert cases.with_fault(dropped, "backend.error", 1) is None
    assert cases.with_fault(case, "backend.timeout", 1) is None

    wait = Variable("backend.wait", 1)
    assert cases.value(dropped, wait) == 500_000 and cases.value(case, wait) is None
    assert cases.with_value(dropped, wait, 750_000)["faults"] == [fault("backend.drop", 1, 750_000)]
    assert cases.with_value(case, wait, 750_000) is None
    assert cases.with_value(cases.with_fault(case, "backend.error", 1), wait, 750_000) is None


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
    # A timeout in both orders: commit as written, and commit after the caller gave up.
    assert by_base["backend.timeout 1, commit first"]["delays"] == []
    assert by_base["backend.timeout 1, commit later"]["delays"] == [
        {"of": "backend.latency", "nth": 1, "us": 2_000_000}
    ]
    assert found.predicates() == [Predicate("A2", "rule"), Predicate("M2", "rule d")]
    assert found.summary()["found"]["A2/rule"]["changed"] == ["backend.drop 1"]
    # A break that moved nothing has no window to measure.
    assert found.windows(Predicate("A2", "rule")) == []

    # With a zero wait the write commits later as written; the other order waits longer.
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


@pytest.mark.slow
def test_a_search_runs_no_case_twice():
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
        stretch_us=0,
    )
    assert len(ran) == len(set(ran)) == found.runner.runs
    assert cases.canonical(author) in ran
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


def test_a_second_fault_goes_on_a_write_made_only_under_the_first():
    """The stand-in writes once as given, and a second time only if the first write errors."""
    retries = answers(has_fault("backend.error", 1))
    found = explore(
        a_case(BOOK),
        retries,
        variables=(),
        faults=["backend.error", "backend.timeout"],
        pairs=True,
        stretch_us=0,
    )
    two = {
        t.candidate.base: t.candidate.case["faults"]
        for t in found.tried
        if len(t.candidate.case["faults"]) == 2
    }
    # An error, then the retry, then a fault on the retry. No other run makes a second write.
    assert set(two) == {
        "backend.error 1 + backend.error 2",
        "backend.error 1 + backend.timeout 2, commit first",
        "backend.error 1 + backend.timeout 2, commit later",
    }
    assert two["backend.error 1 + backend.timeout 2, commit first"] == [
        fault("backend.error"),
        fault("backend.timeout", 2, 1_000_000),
    ]


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

    faulted = {**author, "faults": [fault("backend.error")]}
    assert (
        shrink(faulted, answers(INTERRUPTS), Predicate("A2", "rule"), author=faulted).case["faults"]
        == []
    )


HANGUP = Variable("user.hangup", 0)


def left_between(lo_us, hi_us):
    return lambda case: case["hangup_us"] is not None and lo_us <= case["hangup_us"] <= hi_us


def test_a_hangup_is_a_time_in_the_case_no_earlier_than_the_end_of_the_last_speech():
    case = a_case(BOOK, WINDOW)
    assert cases.value(case, HANGUP) is None
    left = cases.with_value(case, HANGUP, 8_000_000)
    assert left["hangup_us"] == 8_000_000 and case["hangup_us"] is None
    assert cases.with_value(case, HANGUP, 6_000_000) is not None
    assert cases.with_value(case, HANGUP, 5_999_999) is None
    assert "before their last speech is over" in cases.validate({**case, "hangup_us": 3_000_000})[0]
    assert cases.with_value(left, SECOND, 7_500_000) is None
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
    assert any(p.handle.variable == HANGUP for p in found.impossible)
    breaking = found.breaking(A2)
    assert breaking and all(7_000_000 <= t.candidate.value_us <= 9_000_000 for t in breaking)
    (measured,) = found.windows(A2)
    assert (measured.variable, measured.lo_us, measured.hi_us) == (HANGUP, 7_000_000, 9_000_000)
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
    assert max(t.candidate.value_us for t in found.tried) > 6_500_000


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

    assert shrink(found, answers(INTERRUPTS), A2, author=author).case["hangup_us"] is None
    leaving = {**author, "hangup_us": 9_000_000}
    reduced = shrink(leaving, answers(lambda case: True), A2, author=leaving)
    assert reduced.case["hangup_us"] is None and "removed the hangup" in reduced.steps


def test_a_hangup_the_failure_does_not_need_keeps_no_speech_in_the_reduced_case():
    # A user who hangs up must have spoken, so the hangup goes before the speeches are tried.
    leaving = a_case(BOOK, hangup_us=9_000_000)
    reduced = shrink(leaving, answers(lambda case: True), A2)
    assert reduced.case["speeches"] == [] and reduced.case["hangup_us"] is None
    assert reduced.steps == ["removed the hangup", "removed 1 of 1 speeches"]


SLOW_WRITE = {"of": "backend.latency", "nth": 1, "us": 1_500_000}


def slow_write_and(fails):
    return lambda case: SLOW_WRITE in case["delays"] and fails(case)


def test_pairs_stretch_each_write_and_each_generation_shorter_than_the_stretch():
    run = answers(lambda case: False)

    def versions(**options):
        found = explore(a_case(BOOK, WINDOW), run, variables=(), faults=[], **options)
        assert all(t.candidate.variable is None for t in found.tried)
        return {t.candidate.base: t.candidate.case["delays"] for t in found.tried}

    def stretched(us, *delays):
        return {f"{of} {nth}, stretched": [{"of": of, "nth": nth, "us": us}] for of, nth in delays}

    # The recorded run made one write, which commits at once, and two model calls of 0.3 s.
    every = (("backend.latency", 1), ("llm.duration", 1), ("llm.duration", 2))
    assert versions(pairs=True) == stretched(1_500_000, *every)
    assert versions() == {}
    assert versions(pairs=True, stretch_us=300_000) == stretched(300_000, ("backend.latency", 1))
    assert versions(pairs=True, stretch_us=0) == {}
    assert versions(pairs=True, delay_limit_us=1_000_000) == stretched(1_000_000, *every)


@pytest.mark.slow
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
    assert explore(far, run, **options).predicates() == []
    found = explore(far, run, pairs=True, **options)
    breaking = found.breaking(A2)
    assert {t.candidate.what for t in breaking} == {
        "backend.latency 1, stretched + speech.position 1"
    }
    assert all(t.candidate.case["delays"] == [SLOW_WRITE] for t in breaking)
    (version,) = [t for t in found.tried if t.candidate.what == "backend.latency 1, stretched"]
    assert version.candidate.case == {**far, "delays": [SLOW_WRITE]}
    assert version.candidate.variable is None and not version.outcome.failures
    (measured,) = found.windows(A2)
    assert (measured.variable, measured.lo_us, measured.hi_us) == (SECOND, 3_000_000, 7_800_000)


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
def test_a_case_found_under_a_stretched_delay_reduces_to_the_delay_and_the_hangup():
    """The recorded write is requested at 3.0 s. The stand-in fails if the user leaves during it."""

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
        assert run(undone) == ledger("1.8.12", "book_then_interrupt")


def test_a_reduction_makes_no_delay_longer_than_a_search_does():
    """The failure holds while the write takes 0.2 to 30 s, and a search makes none longer than
    10 s. The window has no edge at the limit, so the write settles just inside its one edge."""

    def slow_enough(case):
        took = cases.value(case, Variable("backend.latency", 1))
        return took is not None and 200_000 <= took <= 30_000_000

    write = Variable("backend.latency", 1)
    author = a_case(BOOK)
    found = cases.with_value(author, write, 1_500_000)
    shrunk = shrink(found, answers(slow_enough), A2, author=author)
    assert cases.value(shrunk.case, write) == 1_000_000
    (measured,) = [w for w in shrunk.windows if w.variable == write]
    assert (measured.lo_us, measured.hi_us) == (200_000, 30_000_000)
    # With the limit above the window, the write settles in its middle.
    roomy = shrink(found, answers(slow_enough), A2, author=author, delay_limit_us=60_000_000)
    assert cases.value(roomy.case, write) == 15_000_000
    long = cases.with_value(author, write, 20_000_000)
    assert cases.value(shrink(long, answers(slow_enough), A2, author=author).case, write) <= (
        20_000_000
    )


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


def test_a_break_is_at_instants_only_if_nothing_else_reaches_it():
    def fails(case):
        return bool(case["faults"]) or case["speeches"][1]["start_us"] == 5_000_000

    start = a_case(BOOK, speech(9_000_000, 10_000_000, WINDOW["transcript"]))
    runner = Runner(answers(fails))
    moved = cases.with_value(start, SECOND, 5_000_000)
    faulted = cases.with_fault(start, "backend.error", 1)
    by_time = Tried(Candidate(moved, SECOND, 5_000_000), runner.outcome(moved))
    by_fault = Tried(
        Candidate(faulted, None, None, base="backend.error 1"), runner.outcome(faulted)
    )

    def found(*tried):
        return Exploration(start, runner.outcome(start), list(tried), [], (0,), runner)

    (alone,) = [b for b in found(by_time).breaks() if b.predicate == A2]
    assert [w.instant for w in alone.windows] == [True] and alone.instants_only
    assert "single instants only" in found(by_time).describe()
    found(by_time).assert_contracts(allow=["M2/rule d"])  # the instant is not counted
    (both,) = [b for b in found(by_time, by_fault).breaks() if b.predicate == A2]
    assert [w.instant for w in both.windows] == [True] and not both.instants_only
    assert both.what == "backend.error 1"  # the example is the case with the least changed
    with pytest.raises(ContractFailure, match="A2/rule  in 2 case"):
        found(by_time, by_fault).assert_contracts(allow=["M2/rule d"])


def moved_to(fails, *values):
    """A search whose candidates put the second speech at each of ``values``."""
    start = a_case(BOOK, speech(9_000_000, 10_000_000, WINDOW["transcript"]))
    runner = Runner(answers(lambda case: fails(case["speeches"][1]["start_us"])))
    tried = []
    for at in values:
        moved = cases.with_value(start, SECOND, at)
        tried.append(Tried(Candidate(moved, SECOND, at), runner.outcome(moved)))
    return Exploration(start, runner.outcome(start), tried, [], (0,), runner)


def test_a_wide_window_beyond_any_number_of_instants_is_measured_and_counted():
    instants = (3_000_000, 3_100_000, 3_200_000)
    found = moved_to(
        lambda at: at in instants or 4_000_000 <= at <= 4_200_000,
        *instants,
        4_000_000,
        4_100_000,
        4_200_000,
    )
    one = found.breaks()[0]
    assert [(w.lo_us, w.hi_us) for w in one.windows] == [
        *((at, at) for at in instants),
        (4_000_000, 4_200_000),
    ]
    assert one.predicate == A2 and not one.instants_only and one.unmeasured == 0
    with pytest.raises(ContractFailure, match="A2/rule  in 6 case"):
        found.assert_contracts()

    five = tuple(range(3_000_000, 3_500_000, 100_000))
    ties = moved_to(lambda at: at in five, *five)
    one = ties.breaks()[0]
    assert [w.instant for w in one.windows] == [True] * 5 and one.instants_only
    ties.assert_contracts()


def test_windows_beyond_three_along_one_time_are_counted_and_not_measured():
    starts = (3_000_000, 3_200_000, 3_400_000, 3_600_000)
    found = moved_to(
        lambda at: any(start <= at <= start + 50_000 for start in starts), *starts, 3_650_000
    )
    one = found.breaks()[0]
    assert [w.lo_us for w in one.windows] == list(starts[:3]) and one.unmeasured == 2
    assert not one.instants_only
    assert "\n    breaks at 2 more value(s), which were not measured\n" in found.describe()
    assert [w.lo_us for w in found.windows(A2, most=4)] == list(starts)


FAR = a_case(BOOK, speech(20_000_000, 21_000_000, WINDOW["transcript"]))
NARROW = {
    "variables": ["speech.position"],
    "faults": [],
    "speeches": [1],
    "moments": ["interrupt"],
    "words": False,
}


def test_a_search_says_what_it_is_about_to_run_before_it_runs_it():
    run = answers(INTERRUPTS)
    plans = []
    found = explore(FAR, run, planned=lambda plan: plans.append((plan, run.runs)), **NARROW)
    ((plan, runs_then),) = plans
    assert plan.runs == runs_then > 0 and plan.max_runs is None
    assert plan.cases == plan.to_run == plan.covered == len(found.tried)
    assert plan.placements == sum(len(t.candidate.placements) for t in found.tried) >= plan.cases
    assert found.runner.runs == plan.runs + plan.to_run and found.complete
    assert str(plan) == (
        f"{plan.cases} cases ({plan.placements} placements of a time, and the faults); "
        f"{plan.to_run} to run, after {plan.runs} run(s) to find them. "
        "Measuring where each failure starts and ends takes more runs"
    )


def test_a_search_counts_its_runs_by_purpose_and_a_reduction_is_not_one_of_its_runs():
    found = explore(FAR, answers(INTERRUPTS), **NARROW)
    predicate = found.predicates()[0]
    found.windows(predicate)
    made = found.runner.by_purpose
    assert made["measure"] > 0 and found.runs == found.runner.runs == sum(made.values())
    with found.runner.counting("reduce"):
        shrink(found.breaking(predicate)[0].candidate.case, found.runner, predicate)
    assert found.runner.runs > found.runs == sum(n for p, n in made.items() if p != "reduce")
    assert (
        found.summary()["runs"] == found.runs and "reduce" not in found.summary()["runs_by_purpose"]
    )


def test_a_search_stops_at_its_budget_and_its_result_says_it_is_incomplete():
    whole = explore(FAR, answers(lambda case: False), **NARROW)
    budget, plans = whole.runner.runs - 5, []
    cut = explore(FAR, answers(lambda case: False), max_runs=budget, planned=plans.append, **NARROW)
    assert cut.runner.runs == budget and not cut.complete and cut.not_run == 5
    assert len(cut.tried) == len(whole.tried) - 5
    (plan,) = plans
    assert f". The budget of {budget} runs covers {plan.to_run - 5} of them. " in str(plan)
    summary = cut.summary()
    assert (summary["complete"], summary["not_run"], summary["max_runs"]) == (False, 5, budget)
    with pytest.raises(ContractFailure) as caught:  # no rule broke, and that is not the result
        cut.assert_contracts()
    head, said = str(caught.value).split("\n\n")
    tried = len(cut.tried)
    assert head.splitlines()[0].endswith(
        f": {tried} of {tried + 5} cases tried, {tried} judged, 0 contract rule(s) broken"
    )
    assert said == (
        f"incomplete: the budget of {budget} runs was spent with 5 case(s) not run. "
        "Nothing is concluded about them"
    )
    roomy = explore(FAR, answers(lambda case: False), max_runs=whole.runner.runs, **NARROW)
    assert roomy.complete and roomy.summary()["complete"] and len(roomy.tried) == len(whole.tried)
    roomy.assert_contracts()
    # The runs that read the boundaries are made whatever the budget is.
    least = explore(FAR, answers(lambda case: False), max_runs=1, **NARROW)
    assert least.tried == [] and least.not_run == len(whole.tried)
    assert least.runner.runs == plan.runs and not least.nothing_judged and not least.complete
    with pytest.raises(ValueError, match="max_runs is a number of runs, at least 1; got 0"):
        explore(FAR, answers(lambda case: False), max_runs=0, **NARROW)


def test_a_budget_spent_while_failures_are_measured_leaves_the_search_incomplete():
    whole = explore(FAR, answers(INTERRUPTS), **NARROW)
    budget = whole.runner.runs + 10  # every case, and ten of the runs that measure
    cut = explore(FAR, answers(INTERRUPTS), max_runs=budget, **NARROW)
    assert cut.complete and cut.not_run == 0
    broke = cut.breaks()
    assert not cut.complete and cut.unmeasured == sum(one.unmeasured for one in broke) > 0
    assert all(not one.instants_only for one in broke)
    assert cut.runner.runs <= budget + len(broke)  # an example is run once more for its ledger
    assert cut.breaks() == broke  # asking again runs nothing and measures no more
    assert (
        f"\n\nincomplete: the budget of {budget} runs was spent with {cut.unmeasured} "
        "failure(s) not measured. Nothing is concluded about them"
    ) in cut.describe(broke)
    with pytest.raises(ContractFailure, match="incomplete: the budget"):
        cut.assert_contracts(allow=["A2/rule", "M2/rule d"])
    assert whole.breaks() and whole.complete and "incomplete" not in whole.describe()


def test_a_rule_with_a_value_the_budget_left_unmeasured_is_not_at_instants_only():
    instants = (3_000_000, 3_100_000)
    found = moved_to(lambda at: at in instants, *instants)
    found.max_runs = found.runner.runs + 4  # the four runs that measure one instant
    one = found.breaks()[0]
    assert [w.instant for w in one.windows] == [True] and one.unmeasured == 1
    assert not one.instants_only and not found.complete
    # The other rule the same runs break is measured from them, at no further run.
    assert [[w.instant for w in other.windows] for other in found.breaks()] == [[True], [True]]
    with pytest.raises(ContractFailure, match="A2/rule  in 2 case"):
        found.assert_contracts()


def test_an_end_of_a_window_at_a_run_that_could_not_be_judged_says_why():
    """The question breaks the rule from 3 s on, and a run of it after 9 s is invalid."""
    stand_in = answers(asked_about_the_window_between(3_000_000, 10**12))

    def run(case):
        events = stand_in(case)
        if case["speeches"][-1]["start_us"] > 9_000_000:
            events = copy.deepcopy(events)
            events[-1]["data"]["underruns"] = 2
        return events

    found = window(Runner(run), a_case(BOOK, WINDOW), SECOND, A2)
    invalid = "invalid: playout ran ahead of its audio 2 times"
    assert (found.lo_end, found.lo_unjudged, found.lo_us) == ("passes", None, 3_000_000)
    assert (found.hi_end, found.hi_unjudged, found.hi_us) == ("blocked", invalid, 9_000_000)
    assert found.to_json()["hi_unjudged"] == invalid and "lo_unjudged" not in found.to_json()
    searched = explore(
        a_case(BOOK, WINDOW),
        run,
        variables=["speech.position"],
        speeches=[1],
        moments=["interrupt"],
        words=False,
        faults=[],
    )
    said = searched.describe([b for b in searched.breaks() if b.predicate == A2])
    assert f"to 9.000 s (a run beyond it could not be judged, {invalid})" in said


def test_each_end_of_a_window_says_why_the_search_stopped_there():
    """The stand-in fails everywhere, so neither end of the window is an edge of the failure."""
    always = window(
        Runner(answers(asked_about_the_window_between(0, 10**12))),
        a_case(BOOK, WINDOW),
        SECOND,
        A2,
        reach_us=8_000_000,
    )
    assert (always.lo_end, always.hi_end) == ("blocked", "reach") and not always.instant
    assert always.lo_us == 2_500_000  # touching the first speech
    assert 5_000_000 + 8_000_000 <= always.hi_us <= 5_000_000 + 16_000_000
    assert always.to_json()["lo_end"] == "blocked"
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
        window(runner, a_case(BOOK, WINDOW), Variable("backend.latency", 1), A2)


def test_ddmin_finds_a_one_minimal_subset():
    assert ddmin(range(8), lambda kept: 2 in kept and 6 in kept) == [2, 6]
    assert ddmin(range(8), lambda kept: 5 in kept) == [5]
    assert ddmin(range(8), lambda kept: True) == []
    assert ddmin([1, 2, 3], lambda kept: kept == [1, 2, 3]) == [1, 2, 3]
    # the empty subset fails though neither half does, so the answer is empty
    assert ddmin([1, 2], lambda kept: kept in ([], [1, 2])) == []
    tried = []
    assert ddmin(list("abcdefgh"), lambda kept: tried.append(kept) or {"a", "h"} <= set(kept)) == [
        "a",
        "h",
    ]
    assert len(tried) < 40  # far fewer than the 256 subsets
    with pytest.raises(ValueError, match="failing start"):
        ddmin(range(3), lambda kept: False)


AUTHOR = a_case(BOOK, speech(20_000_000, 21_000_000, WINDOW["transcript"]))
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
    assert {**shrunk.case, "speeches": AUTHOR["speeches"]} == AUTHOR
    moved = shrunk.case["speeches"][1]
    assert shrunk.case["speeches"][0] == BOOK and moved["transcript"] == WINDOW["transcript"]
    # in the middle of the 3.0-7.8 s window, on a whole second
    assert (moved["start_us"], moved["end_us"]) == (5_000_000, 6_000_000)
    assert shrunk.differences == ["speech 'Is the table by the window?' moved to 5000000 us"]
    assert shrunk.steps[-1] == "speech.position 1: settled at 5000000 us, inside 3000000..7800000"
    by = {str(w.variable): (w.lo_us, w.hi_us) for w in shrunk.windows}
    assert by["speech.position 1"] == (3_000_000, 7_800_000)
    assert "speech.position 0" in by
    assert FOUND["seed"] == 7


def test_shrinking_removes_a_speech_of_the_authors_that_the_failure_does_not_need():
    def fails(case):
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
    assert shrunk.differences == ["speech 'Is the table by the window?' dropped"]


def test_a_settled_time_is_the_roundest_in_the_middle_half_of_its_window():
    def at(lo, hi, lo_end="passes", hi_end="passes", was=None):
        return settled(Window(SECOND, lo, hi, lo_end, hi_end, 1_000), lo if was is None else was)

    assert at(3_000_000, 7_800_000) == 5_000_000  # a whole second, nearest the centre
    assert at(3_140_000, 3_260_000) == 3_200_000
    assert (
        at(3_141_000, 3_148_000) == 3_144_000
    )  # a millisecond, when that is all there is room for
    assert at(3_240_000, 3_300_000) == 3_270_000  # never an edge: 3.3 s is rounder, and is the edge
    assert at(0, 1_140_000, "blocked", "blocked") == 500_000
    assert at(5_000_000, 5_000_000) is None
    # an end the search gave up on is no edge: just inside the one that is
    assert at(1_200_000, 31_200_000, hi_end="reach") == 2_000_000
    assert at(0, 2_500_000, lo_end="reach") == 2_000_000
    assert (
        at(0, 60_000_000, "reach", "reach", was=5_030_000) == 5_000_000
    )  # no edge at all: where it was, rounded
    for lo, hi in ((3_000_000, 7_800_000), (3_140_000, 3_260_000), (3_240_000, 3_300_000)):
        assert at(lo, hi, was=at(lo, hi)) == at(lo, hi)


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
    assert shrunk.case["speeches"][0] == BOOK  # no edge along it, so it stays as found
    moved = shrunk.case["speeches"][1]
    assert moved["end_us"] - moved["start_us"] == 1_130_000  # nor along the length of the question


def test_shrinking_needs_a_failing_case_and_says_when_its_budget_ran_out():
    with pytest.raises(ValueError, match="does not break A2/rule"):
        shrink(a_case(BOOK), answers(lambda case: False), A2)
    run = answers(INTERRUPTS)
    stopped = shrink(FOUND, run, A2, author=AUTHOR, max_runs=3)
    assert not stopped.complete and stopped.runs == 3 == run.runs
    assert A2.holds(check(run(stopped.case)))


def test_the_runs_that_measure_a_window_count_against_the_budget_of_a_reduction():
    whole = shrink(FOUND, answers(INTERRUPTS), A2, author=AUTHOR)
    assert whole.complete and whole.runs > 60 and len(whole.windows) == 2
    run = answers(INTERRUPTS)
    stopped = shrink(FOUND, run, A2, author=AUTHOR, max_runs=20)
    assert not stopped.complete and stopped.runs == 20 == run.runs
    # The budget ran out while the first window was measured: none is reported, nothing settled.
    assert stopped.windows == [] and stopped.steps == whole.steps[:1]
    assert A2.holds(check(run(stopped.case)))
    runner = Runner(answers(INTERRUPTS))
    with pytest.raises(Spent):
        window(runner, a_case(BOOK, WINDOW), SECOND, A2, spent=lambda: runner.runs >= 5)
    assert runner.runs == 5


def test_the_case_to_reduce_is_the_one_nearest_the_authors():
    near = applied(AUTHOR, differences(AUTHOR, FOUND)[:1])
    nearer = cases.with_value(AUTHOR, SECOND, 19_000_000)
    assert nearest(AUTHOR, [FOUND, near]) == near  # fewer differences
    assert nearest(AUTHOR, [near, nearer]) == nearer  # as many, moved less


@pytest.mark.slow
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
        "ledger": "earful.ledger/1",
        "contracts": "earful.contracts/2",
        "case": "earful.case/1",
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
    assert manifest["schema_minor"] == 1 and kept.unverified == ()
    assert manifest["minimal"]["case_digest"] == bundle.case_digest(kept.minimal)
    assert manifest["original"]["case_digest"] == bundle.case_digest(FOUND)
    again = bundle.replay(kept, run, fingerprint=lambda reference: "a" * 64)
    assert (
        again.reproduced
        and again.repeatable
        and again.identical
        and again.scenario_changed is False
    )
    assert bundle.replay(kept, run).scenario_changed is None  # nothing to compare it with
    assert bundle.replay(kept, run, fingerprint=lambda reference: "b" * 64).scenario_changed is True
    repaired = bundle.replay(kept, answers(lambda case: False))
    assert not repaired.reproduced and repaired.repeatable and not repaired.identical
    assert repaired.framework["version"] == "1.8.12"


def held(run, case):
    """A runner that already holds the ledger of ``case``, as a search does of what it found."""
    runner = Runner(run)
    assert runner.ledger(case) is not None
    return runner


def test_a_case_that_does_not_repeat_is_not_kept(tmp_path):
    def unsteady(case):
        unsteady.runs += 1
        events = copy.deepcopy(ledger("1.5.6", "book_then_interrupt"))
        said = next(e for e in events if e["source"] == "framework")
        said["data"]["created_us"] += unsteady.runs  # a different ledger every run
        return events

    unsteady.runs = 0
    case = a_case(BOOK, WINDOW)
    with pytest.raises(ValueError) as refused:
        bundle.capture(tmp_path / "kept", case, held(unsteady, case), A2)
    assert str(refused.value).startswith(
        "the reduced case gave the ledger that broke A2/rule in 0 of the 3 runs made after "
    )  # every run broke the rule, and that is not enough
    assert str(refused.value).endswith("a case that does not repeat is not kept as a bundle")
    assert not (tmp_path / "kept").exists()
    with pytest.raises(ValueError, match="at least once; got repeats=0"):
        bundle.capture(tmp_path / "kept", case, answers(INTERRUPTS), A2, repeats=0)


@pytest.mark.slow
def test_a_failure_that_does_not_come_back_is_not_kept(tmp_path):
    failing, passing = (ledger(release, "book_then_interrupt") for release in ("1.5.6", "1.8.12"))
    seen = []

    def once_per_case(case):
        seen.append(cases.canonical(case))
        return failing if INTERRUPTS(case) and seen.count(seen[-1]) == 1 else passing

    case = a_case(BOOK, WINDOW)
    # Three runs after the reduction that agree with each other, and none breaks the rule.
    with pytest.raises(ValueError, match="broke A2/rule in 0 of the 3 runs made after"):
        bundle.capture(tmp_path / "kept", case, held(once_per_case, case), A2)
    seen.clear()
    with pytest.raises(ValueError, match="the case as found did not give the ledger that broke"):
        bundle.capture(tmp_path / "kept", case, once_per_case, A2)
    assert not (tmp_path / "kept").exists()


def test_a_bundle_that_is_not_one_is_refused(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"schema": "something/1"}))
    with pytest.raises(ValueError, match="not an earful.bundle/1 manifest"):
        bundle.load(tmp_path)


REFERENCE = Path(__file__).parent / "bundles" / "lk-1.5.6.book_then_interrupt.A2-rule"


def rewrite(path, change):
    """Change the JSON document in ``path``."""
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))


def a_copy(tmp_path):
    """A copy of a reference bundle, to change."""
    return Path(shutil.copytree(REFERENCE, tmp_path / "kept"))


def another_run(directory):
    shutil.copy(
        GOLDEN / "lk-1.8.12.book_then_interrupt.ledger.normalized.jsonl",
        directory / "minimal.ledger.jsonl",
    )


def cut_short(directory):
    path = directory / "original.ledger.jsonl"
    path.write_text("".join(path.read_text().splitlines(keepends=True)[:-1]))


def asked_later(directory):
    def move(case):
        case["speeches"][1].update(start_us=9_000_000, end_us=10_000_000)

    rewrite(directory / "minimal.case.json", move)


def in_manifest(**formats):
    return lambda directory: rewrite(
        directory / "manifest.json", lambda manifest: manifest["formats"].update(formats)
    )


L2_RULE_B = {"contract": "L2", "rule": "rule b"}

# What is done to a copy of the bundle, and what the refusal says after the directory.
TAMPERED = {
    "a ledger that is no ledger": (
        lambda d: (d / "minimal.ledger.jsonl").write_text("not a ledger\n"),
        "/minimal.ledger.jsonl is not a ledger file: ",
    ),
    "a ledger that is missing": (
        lambda d: (d / "original.ledger.jsonl").unlink(),
        "/original.ledger.jsonl: No such file",
    ),
    "the ledger of another run": (
        another_run,
        "/minimal.ledger.jsonl is not the ledger the manifest records: its hash is ",
    ),
    "a ledger cut short": (
        cut_short,
        "/original.ledger.jsonl is not an earful.ledger/1 ledger: the ledger must end with",
    ),
    "a case that was edited": (
        asked_later,
        "/minimal.case.json is not the case the manifest records: its digest is ",
    ),
    "a case that is no case": (
        lambda d: rewrite(d / "original.case.json", lambda case: case.update(schema="x/0")),
        "/original.case.json is not an earful.case/1 document: schema: ",
    ),
    "a manifest cut short": (
        lambda d: (d / "manifest.json").write_text('{"schema": "ear'),
        "/manifest.json is not JSON: Unterminated string",
    ),
    "no manifest": (
        lambda d: (d / "manifest.json").unlink(),
        "/manifest.json: No such file",
    ),
    "a ledger format this Earful does not read": (
        in_manifest(ledger="earful.ledger/99"),
        "/manifest.json names the ledger format earful.ledger/99; this Earful reads earful.led",
    ),
    "contracts this Earful does not judge by": (
        in_manifest(contracts="earful.contracts/3"),
        "/manifest.json names the contracts format earful.contracts/3; this Earful reads ",
    ),
    "a case format this Earful does not read": (
        in_manifest(case="earful.case/2"),
        "/manifest.json names the case format earful.case/2; this Earful reads earful.case/1",
    ),
    "a rule no contract has": (
        lambda d: rewrite(d / "manifest.json", lambda m: m["predicate"].update(rule="rule z")),
        "/manifest.json names A2/rule z, a rule no contract has",
    ),
    "a rule its ledger does not break": (
        lambda d: rewrite(d / "manifest.json", lambda m: m["predicate"].update(L2_RULE_B)),
        "/minimal.ledger.jsonl does not break L2/rule b under earful.contracts/2",
    ),
}


def test_a_bundle_is_loaded_with_every_file_of_it_checked(tmp_path):
    kept = bundle.load(a_copy(tmp_path))
    assert kept.unverified == ()
    assert kept.ledger() == read_jsonl(kept.directory / "minimal.ledger.jsonl")
    assert kept.ledger("original") == read_jsonl(kept.directory / "original.ledger.jsonl")
    assert kept.minimal == json.loads((kept.directory / "minimal.case.json").read_text())
    assert kept.original == json.loads((kept.directory / "original.case.json").read_text())
    # The digest is of the case, however its file is laid out.
    path = kept.directory / "minimal.case.json"
    path.write_text(json.dumps(dict(reversed(kept.minimal.items())), indent=4))
    assert bundle.load(kept.directory).minimal == kept.minimal


@pytest.mark.parametrize("tamper, wrong", TAMPERED.values(), ids=list(TAMPERED))
def test_a_bundle_with_a_file_that_is_not_what_its_manifest_records_is_refused(
    tmp_path, tamper, wrong
):
    directory = a_copy(tmp_path)
    tamper(directory)
    with pytest.raises(ValueError) as refused:
        bundle.load(directory)
    message = str(refused.value)
    assert f"{directory}{wrong}" in message and len(message.splitlines()) == 1


def test_a_bundle_kept_under_the_contracts_before_these_is_read(tmp_path):
    directory = a_copy(tmp_path)
    in_manifest(contracts="earful.contracts/1")(directory)
    assert bundle.load(directory).manifest["formats"]["contracts"] == "earful.contracts/1"
    assert bundle.FORMATS["contracts"] == "earful.contracts/2"
    rewrite(directory / "manifest.json", lambda manifest: manifest["predicate"].update(L2_RULE_B))
    with pytest.raises(
        ValueError,
        match=r"under earful.contracts/2, and the bundle was kept under "
        r"earful.contracts/1: the bundle does not show the failure it names$",
    ):
        bundle.load(directory)


def test_a_replay_is_reproduced_or_clean_only_if_every_run_was_judged(tmp_path):
    kept = bundle.load(a_copy(tmp_path))
    failing, passing = (ledger(release, "book_then_interrupt") for release in ("1.5.6", "1.8.12"))
    not_evidence = passing[:-1]  # no run.end: the checker gives no verdict on it

    def in_turn(*ledgers):
        runs = iter(ledgers)
        return lambda case: next(runs)

    def shows(again):
        return (again.judged, again.reproduced, again.clean, again.repeatable)

    broken = bundle.replay(kept, in_turn(failing, failing, failing))
    assert shows(broken) == (True, True, False, True) and broken.unverified == ()
    assert shows(bundle.replay(kept, in_turn(passing, passing, passing))) == (
        True,
        False,
        True,
        True,
    )
    unjudged = bundle.replay(kept, in_turn(not_evidence, not_evidence, not_evidence))
    assert shows(unjudged) == (False, False, False, True)  # three equal ledgers that show nothing
    assert "run.end" in unjudged.invalid[0]
    one_unjudged = bundle.replay(kept, in_turn(passing, not_evidence, passing))
    assert shows(one_unjudged) == (False, False, False, False)
    in_some = bundle.replay(kept, in_turn(failing, passing, failing))
    assert shows(in_some) == (True, False, False, False)
    assert shows(bundle.replay(kept, in_turn(passing), repeats=1)) == (True, False, True, True)
    assert (broken.outcomes, broken.others, broken.broke) == (("fail",) * 3, ("M2/rule d",), 3)
    assert (unjudged.outcomes, in_some.broke) == ((None,) * 3, 2)
    for repeats in (0, -1):
        with pytest.raises(ValueError, match=f"at least one run; got repeats={repeats}"):
            bundle.replay(kept, in_turn(), repeats=repeats)


def test_a_replay_is_clean_only_if_the_contract_of_the_bundle_passed_in_every_run(tmp_path):
    """A run in which A2 has nothing to judge shows nothing about A2, whatever else broke."""
    kept = bundle.load(a_copy(tmp_path))
    passing = ledger("1.8.12", "book_then_interrupt")
    elsewhere = ledger("1.5.6", "speak_over_new_turn")  # A2 not exercised, L2 rule b broken

    def in_turn(*ledgers):
        runs = iter(ledgers)
        return lambda case: next(runs)

    nothing = bundle.replay(kept, in_turn(elsewhere, elsewhere, elsewhere))
    assert (nothing.judged, nothing.reproduced, nothing.clean) == (False, False, False)
    assert nothing.outcomes == ("not_exercised",) * 3 and nothing.others == ("L2/rule b",)
    assert nothing.invalid == () and nothing.repeatable
    mixed = bundle.replay(kept, in_turn(passing, elsewhere, passing))
    assert (mixed.judged, mixed.clean, mixed.outcomes[1]) == (False, False, "not_exercised")
    clean = bundle.replay(kept, in_turn(passing), repeats=1)
    assert (clean.judged, clean.clean, clean.outcomes, clean.others) == (True, True, ("pass",), ())
    assert (nothing.codes, clean.codes) == ((), ())
    blind = bundle.replay(kept, in_turn(_blind(passing)), repeats=1)
    assert (blind.judged, blind.outcomes, blind.codes) == (
        False,
        ("unobservable",),
        ("outside-backend",),
    )


def test_a_bundle_written_before_manifests_recorded_its_cases_loads_and_says_so(tmp_path):
    directory = a_copy(tmp_path)

    def as_before(manifest):
        manifest["schema_minor"] = 0
        for which in ("original", "minimal"):
            del manifest[which]["case_digest"]

    rewrite(directory / "manifest.json", as_before)
    assert bundle.load(directory).unverified == ("original.case.json", "minimal.case.json")
    asked_later(directory)  # nothing records what the case was
    kept = bundle.load(directory)
    assert kept.minimal["speeches"][1]["start_us"] == 9_000_000
    again = bundle.replay(kept, lambda case: kept.ledger(), repeats=1)
    assert again.reproduced and again.unverified == kept.unverified


@pytest.mark.slow
def test_a_manifest_cannot_point_outside_its_bundle(tmp_path):
    bundle.capture(tmp_path / "kept", a_case(BOOK, WINDOW), answers(INTERRUPTS), A2)
    recorded = bundle.load(tmp_path / "kept")
    path = tmp_path / "kept" / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["minimal"]["case"] = "../elsewhere.json"
    path.write_text(json.dumps(manifest))
    loaded = bundle.load(tmp_path / "kept")
    assert loaded.minimal == recorded.minimal
    assert loaded.ledger("minimal") == recorded.ledger("minimal")


@pytest.mark.parametrize(
    "directory", sorted((Path(__file__).parent / "bundles").iterdir()), ids=lambda d: d.name
)
def test_every_reference_bundle_is_what_its_manifest_says(directory):
    kept = bundle.load(directory)
    assert kept.manifest["schema_minor"] == 1 and kept.unverified == ()
    for which, case in (("original", kept.original), ("minimal", kept.minimal)):
        events = kept.ledger(which)
        assert ledger_hash(events) == kept.manifest[which]["hash"]
        assert bundle.case_digest(case) == kept.manifest[which]["case_digest"]
        assert len(case["speeches"]) == kept.manifest[which]["speeches"]
        assert kept.predicate.holds(check(events))
        assert events[0]["data"]["framework"] == kept.manifest["framework"]
    assert kept.manifest["reduction"]["complete"] and kept.manifest["windows"]
    assert not any(w["instant"] for w in kept.manifest["windows"])


def test_a_failure_measured_again_past_the_end_of_its_window_is_one_window():
    """A window is located to 1 ms: the second case, 0.4 ms past the first window's end, finds
    the same failure. Counted once, it leaves room within ``most`` for the other failure."""
    fails = lambda at: 3_000_500 <= at <= 3_004_500 or 5_000_000 <= at <= 5_002_000
    found = moved_to(fails, 3_001_000, 3_004_400, 5_001_000)
    windows, unmeasured = found._measured(A2, most=2)
    assert [(w.lo_us, w.hi_us) for w in windows] == [(3_001_000, 3_004_400), (5_000_000, 5_002_000)]
    assert unmeasured == 0 and all(w.lo_end == w.hi_end == "passes" for w in windows)


def test_the_shrinker_repeats_its_passes_until_none_removes_anything():
    """The rule breaks while the question is asked, and either the booking is too or no delay
    is declared. Only once the delay is gone can the booking go."""

    def fails(case):
        said = [s["transcript"] for s in case["speeches"]]
        return WINDOW["transcript"] in said and (BOOK["transcript"] in said or not case["delays"])

    case = a_case(BOOK, WINDOW, delays=[{"of": "llm.ttft", "nth": 1, "us": 500_000}])
    reduced = shrink(case, answers(fails), A2)
    assert [s["transcript"] for s in reduced.case["speeches"]] == [WINDOW["transcript"]]
    assert reduced.case["delays"] == []
    assert reduced.steps == ["removed 1 of 1 declared delays", "removed 1 of 2 speeches"]
