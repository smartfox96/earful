"""Every ledger Earful writes validates, and the validator rejects broken ones."""
import copy

import pytest

from earful.core.ledger import SCHEMA, run_id
from earful.core.schema import kinds, load_schema, validate, validity
from earful.livekit.cases import CASES
from earful.livekit.harness import run_case


@pytest.fixture(scope="module")
def booking():
    return run_case(CASES["book_then_interrupt"]()).ledger.events


@pytest.mark.parametrize("can_pause", [True, False])
@pytest.mark.parametrize("name", sorted(CASES))
def test_every_case_validates(name, can_pause):
    case = CASES[name]()
    case.sink_can_pause = can_pause
    events = run_case(case).ledger.events
    assert validate(events) == []
    assert validity(events) == []


def _broken(events, mutate):
    events = copy.deepcopy(events)
    mutate(events)
    return validate(events)


def _first(events, kind):
    return next(e for e in events if e["kind"] == kind)


def _data(events, kind):
    return _first(events, kind)["data"]


def _reseal(events):
    """Give every event the run id of the (edited) header, so only the edit is at fault."""
    run = run_id(events[0]["data"])
    for e in events:
        e["run"] = run


def _edit_header(edit):
    def mutate(events):
        edit(events[0]["data"])
        _reseal(events)

    return mutate


@pytest.mark.parametrize(
    "mutate, expected",
    [
        # the envelope
        (lambda ev: ev.pop(5), "seq"),
        (lambda ev: ev[3].update(t_us=-1), "t_us: -1 is less than the minimum"),
        (lambda ev: ev[-1].update(t_us=0), "earlier than the previous event"),
        (lambda ev: ev[0].update(schema="earful.ledger/0"), "'earful.ledger/3' was expected"),
        (lambda ev: ev[4].update(run="run-0000000000000000"), "not the digest of run.start"),
        (lambda ev: ev[0]["data"].update(seed=7), "not the digest of run.start"),
        (lambda ev: ev[4].pop("run"), "'run' is a required property"),
        (lambda ev: _first(ev, "tool.called").update(source="framework"), "'world' was expected"),
        (lambda ev: _first(ev, "llm.response").update(kind="llm.reply"), "kind: 'llm.reply' is not valid"),
        # the fields of each kind
        (lambda ev: _data(ev, "playout.finished").pop("word_cut"), "'word_cut' is a required property"),
        (lambda ev: _data(ev, "llm.request").update(extra=1), "'extra' was unexpected"),
        (lambda ev: _data(ev, "effect.committed").update(request=True), "True is not of type 'integer'"),
        (lambda ev: _data(ev, "user.origin").update(by="llm"), "'llm' is not one of"),
        (lambda ev: _data(ev, "tts.synthesized")["stamps"].append("x"), "'x' is not of type 'integer'"),
        # nested records: the context a model was shown, tool calls, the scenario
        (lambda ev: _data(ev, "llm.request")["items"][0].pop("role"), "'role' is a required property"),
        (lambda ev: _data(ev, "llm.request")["items"][0].update(text=3), "3 is not of type"),
        (lambda ev: _data(ev, "history.final")["items"][0].update(extra=1), "'extra' was unexpected"),
        (lambda ev: _data(ev, "llm.response")["tool_calls"][0].pop("call_id"), "'call_id' is a required property"),
        (_edit_header(lambda h: h["scenario"]["session"].pop("min_interruption_duration_us")), "is a required property"),
        (_edit_header(lambda h: h["scenario"]["providers"]["backend"].update(mode="fast")), "'fast' is not one of"),
        (_edit_header(lambda h: h["scenario"]["speeches"][0].update(start_us=0.5)), "0.5 is not of type 'integer'"),
        (_edit_header(lambda h: h.update(contracts={"Z9": {}})), "does not match"),
        (_edit_header(lambda h: h.update(clock="fast")), "'fast' is not one of"),
        # references between events
        (lambda ev: _data(ev, "user.vad_start").update(speech=9), "not declared"),
        (lambda ev: _data(ev, "tool.returned").update(tool=7), "no open call"),
        (lambda ev: _data(ev, "effect.committed").update(request=7), "no open request"),
        (lambda ev: _data(ev, "tts.synthesized").update(attribution="none"), "call must be set"),
        (lambda ev: _data(ev, "playout.finished")["words_completed_us"].pop(), "one completion time"),
        (lambda ev: ev.append({**copy.deepcopy(ev[-1]), "seq": len(ev)}), "only run.end"),
        # things a real run cannot produce, which the checker need not survive
        (lambda ev: _first(ev, "playout.paused").update(kind="playout.resumed"), "pauses while playing and resumes while paused"),
        (lambda ev: _data(ev, "user.transcript_final").update(text=""), "should be non-empty"),
        (lambda ev: _first(ev, "llm.response")["data"]["tool_calls"].append(
            dict(_first(ev, "llm.response")["data"]["tool_calls"][0])), "is used twice"),
        (lambda ev: _data(ev, "tts.synthesized")["word_samples"].pop(), "one stamp and one sample count per word"),
        (lambda ev: _data(ev, "tool.returned").pop("expects_reply"), "'expects_reply' is a required property"),
        (lambda ev: _data(ev, "tts.synthesized").update(text="", stamps=[], word_samples=[]), "cannot be attributed to a reply"),
        (lambda ev: _data(ev, "playout.finished").update(utterances=[99]), "utterance 99 was never synthesized"),
        (lambda ev: _data(ev, "playout.started").update(utterance=99), "utterance 99 was never synthesized"),
        (lambda ev: _data(ev, "playout.finished")["words_played"].__setitem__(0, 9999), "belongs to none of the segment's utterances"),
        (lambda ev: ev.insert(5, {**copy.deepcopy(_first(ev, "user.vad_start")), "seq": 5}), "recorded twice for speech 0"),
        (_edit_header(lambda h: h["scenario"].pop("witnessed_tools")), "'witnessed_tools' is a required property"),
        (_edit_header(lambda h: h["scenario"].pop("delays")), "'delays' is a required property"),
        (_edit_header(lambda h: h["scenario"]["delays"].append({"of": "llm.speed", "nth": 1, "us": 0})), "is not one of"),
        # the two speeches are 2.5 s apart: each is a segment of its own to the VAD
        (lambda ev: [e for e in ev if e["kind"] == "user.vad_end"][0]["data"].update(speech=1), "recorded twice for speech 1"),
        (_edit_header(lambda h: h["scenario"]["speeches"][1].update(start_us=2_700_000)), "speech 1 does not open a segment the VAD would hear"),
        # intents must be declared
        (lambda ev: _data(ev, "effect.requested").update(intent="book-tabel-1"), "undeclared intent"),
        (lambda ev: _first(ev, "llm.response")["data"]["claims"].append(
            {"intent": "nope", "first_word": 0, "last_word": 0}), "undeclared intent"),
        (_edit_header(lambda h: h["scenario"]["speeches"][0]["retires"].append("nope")), "retires undeclared intent"),
    ],
)
def test_validator_rejects(booking, mutate, expected):
    errors = _broken(booking, mutate)
    assert any(expected in e for e in errors), errors


def test_schema_version():
    assert SCHEMA == "earful.ledger/3"
    assert load_schema()["properties"]["schema"]["const"] == SCHEMA


def test_every_kind_has_one_source():
    assert kinds()["run.start"] == "harness"
    assert set(kinds().values()) <= {"world", "llm", "sink", "backend", "harness"}


@pytest.mark.parametrize(
    "edit, expected",
    [
        (lambda ev: ev[-1]["data"].update(stamp_errors=2), "reached the sink altered"),
        (lambda ev: ev[-1]["data"].update(executor_calls=1), "left the virtual clock"),
        (lambda ev: ev[-1]["data"].update(underruns=1), "ran ahead of its audio"),
        (lambda ev: ev[-1]["data"].update(harness_errors=["tap broke"]), "harness error: tap broke"),
        (_edit_header(lambda h: h.update(clock="real")), "real clock"),
        (lambda ev: ev.pop(), "schema:"),
    ],
)
def test_validity_names_each_reason(booking, edit, expected):
    events = copy.deepcopy(booking)
    assert validity(events) == []
    edit(events)
    assert any(expected in reason for reason in validity(events)), validity(events)
