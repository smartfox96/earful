"""Every ledger Earful writes validates, and the validator rejects broken ones."""
import copy

import pytest
from jsonschema import Draft202012Validator

from earful.core.ledger import SCHEMA, run_id
from earful.core.schema import event_errors, kinds, load_schema, validate, validity
from earful.livekit.cases import CASES
from earful.livekit.harness import Delay, Fault, run_case


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
        (lambda ev: ev[0].update(schema="earful.ledger/0"), "'earful.ledger/5' was expected"),
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


@pytest.fixture(scope="module")
def timed_out():
    """One booking whose caller gives up after 0.5 s; the write commits at 2 s."""
    case = CASES["book_then_interrupt"]()
    case.speeches = case.speeches[:1]
    case.faults = [Fault("backend.timeout", 1, 0.5)]
    case.delays = [Delay("backend.latency", 1, 2.0)]
    return run_case(case).ledger.events


def _again(kind, as_kind=None, keep=None):
    """Record the first event of ``kind`` a second time, right after it: as another kind,
    with only the fields in ``keep``, if given."""
    def mutate(events):
        at = next(i for i, e in enumerate(events) if e["kind"] == kind)
        again = copy.deepcopy(events[at])
        again["kind"] = as_kind or kind
        if keep is not None:
            again["data"] = {name: again["data"][name] for name in keep}
        events.insert(at + 1, again)
        for seq, e in enumerate(events):
            e["seq"] = seq

    return mutate


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (_again("effect.timed_out"), "its caller gave up twice"),
        (_again("effect.committed", as_kind="effect.failed", keep=("request", "intent", "key")), "no open request"),
        (lambda ev: _data(ev, "effect.timed_out").update(request=9), "was not made"),
        (lambda ev: _data(ev, "effect.timed_out").update(intent="book-table-2"), "intent differs"),
        (lambda ev: _data(ev, "effect.timed_out").pop("after_us"), "'after_us' is a required property"),
        (_edit_header(lambda h: h["scenario"]["faults"][0].update(of="backend.explode")), "is not one of"),
        (_edit_header(lambda h: h["scenario"].pop("faults")), "'faults' is a required property"),
    ],
)
def test_validator_rejects_a_faulted_write_that_cannot_have_happened(timed_out, mutate, expected):
    assert validate(timed_out) == [] and validity(timed_out) == []
    errors = _broken(timed_out, mutate)
    assert any(expected in e for e in errors), errors


@pytest.fixture(scope="module")
def hung_up():
    """One booking; the user leaves at 5 s, while the reply plays."""
    case = CASES["book_then_interrupt"]()
    case.speeches = case.speeches[:1]
    case.hangup = 5.0
    return run_case(case).ledger.events


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (_again("user.hangup"), "user.hangup recorded more than once"),
        (_edit_header(lambda h: h["scenario"].update(hangup_us=None)), "the scenario declares no hangup"),
        (_edit_header(lambda h: h["scenario"].update(hangup_us=4_000_000)), "not at the declared time"),
        (_edit_header(lambda h: h["scenario"].update(hangup_us=1_000_000)), "hangs up before a speech is over"),
        (_edit_header(lambda h: h["scenario"].pop("hangup_us")), "'hangup_us' is a required property"),
        (lambda ev: _first(ev, "user.hangup").update(source="harness"), "'world' was expected"),
        (lambda ev: _data(ev, "user.hangup").update(reason="bored"), "Additional properties are not allowed"),
    ],
)
def test_validator_rejects_a_hangup_that_cannot_have_happened(hung_up, mutate, expected):
    assert validate(hung_up) == [] and validity(hung_up) == []
    errors = _broken(hung_up, mutate)
    assert any(expected in e for e in errors), errors


def _spoiled(event):
    """The event as recorded, then each way of spoiling it that a schema can see."""
    yield "as recorded", event
    for name in event:
        yield f"without {name}", {k: v for k, v in event.items() if k != name}
    for kind in [*kinds(), "fw.made_up", "made.up", 7]:
        yield f"as {kind}", {**event, "kind": kind}
    yield "from another source", {**event, "source": "sink" if event["source"] == "world" else "world"}
    data = event["data"]
    yield "with a field nobody defined", {**event, "data": {**data, "made_up": 1}}
    for name, value in data.items():
        yield f"without data.{name}", {**event, "data": {k: v for k, v in data.items() if k != name}}
        yield f"with data.{name} of another type", {**event, "data": {**data, name: 0 if isinstance(value, str) else "wrong"}}
    for other in (None, [], "event"):
        yield f"as {other!r} in place of an object", other


def test_an_event_checked_by_its_kind_gets_the_whole_schemas_errors(booking, timed_out, hung_up):
    """``event_errors`` leaves out the rules of every other kind. That must change nothing:
    the same errors, in the same order, as the whole schema gives."""
    whole = Draft202012Validator(load_schema())

    def said(errors):
        return [("/".join(map(str, e.absolute_path)), e.message) for e in errors]

    checked_as, compared, rejected = set(), 0, 0
    for events in (booking, timed_out, hung_up):
        for event in {e["kind"]: e for e in events}.values():  # one event of each kind
            for what, spoiled in _spoiled(event):
                expected = said(sorted(whole.iter_errors(spoiled), key=lambda e: list(map(str, e.absolute_path))))
                assert said(event_errors(spoiled)) == expected, (event["kind"], what)
                if what == "as recorded":
                    assert not expected, event["kind"]
                compared += 1
                rejected += bool(expected)
                if isinstance(spoiled, dict) and isinstance(spoiled.get("kind"), str):
                    checked_as.add(spoiled["kind"])
    assert set(kinds()) <= checked_as
    assert rejected > compared // 2  # most of what was compared was an error, as intended


def test_schema_version():
    assert SCHEMA == "earful.ledger/5"
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
