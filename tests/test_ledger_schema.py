"""The committed ledgers validate, and the validator rejects broken ones."""

import copy
from pathlib import Path

import pytest

from earful.core.ledger import SCHEMA, ledger_hash, normalize, read_jsonl, run_id
from earful.core.schema import kinds, load_schema, validate, validity

GOLDEN = Path(__file__).parent / "golden"


@pytest.fixture(scope="module")
def booking():
    return read_jsonl(GOLDEN / "lk-1.8.12.book_then_interrupt.ledger.normalized.jsonl")


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
        (lambda ev: ev[0].update(schema="earful.ledger/0"), "'earful.ledger/1' was expected"),
        (lambda ev: ev[4].update(run="run-0000000000000000"), "not the digest of run.start"),
        (lambda ev: ev[0]["data"].update(seed=7), "not the digest of run.start"),
        (lambda ev: ev[4].pop("run"), "'run' is a required property"),
        (lambda ev: _first(ev, "tool.called").update(source="framework"), "'world' was expected"),
        (
            lambda ev: _first(ev, "llm.response").update(kind="llm.reply"),
            "kind: 'llm.reply' is not valid",
        ),
        # the fields of each kind
        (
            lambda ev: _data(ev, "playout.finished").pop("word_cut"),
            "'word_cut' is a required property",
        ),
        (lambda ev: _data(ev, "llm.request").update(call="one"), "'one' is not of type"),
        (
            lambda ev: _data(ev, "effect.committed").update(request=True),
            "True is not of type 'integer'",
        ),
        (lambda ev: _data(ev, "user.origin").update(by="llm"), "'llm' is not one of"),
        (
            lambda ev: _data(ev, "tts.synthesized")["stamps"].append("x"),
            "'x' is not of type 'integer'",
        ),
        # nested records
        (
            lambda ev: _data(ev, "llm.request")["items"][0].pop("role"),
            "'role' is a required property",
        ),
        (lambda ev: _data(ev, "llm.request")["items"][0].update(text=3), "3 is not of type"),
        (
            lambda ev: _data(ev, "history.final")["items"][0].update(type=1),
            "1 is not of type",
        ),
        (
            lambda ev: _data(ev, "llm.response")["tool_calls"][0].pop("call_id"),
            "'call_id' is a required property",
        ),
        (
            _edit_header(lambda h: h["scenario"]["session"].pop("min_interruption_duration_us")),
            "is a required property",
        ),
        (
            _edit_header(lambda h: h["scenario"]["providers"]["backend"].update(mode="fast")),
            "'fast' is not one of",
        ),
        (
            _edit_header(lambda h: h["scenario"]["speeches"][0].update(start_us=0.5)),
            "0.5 is not of type 'integer'",
        ),
        (_edit_header(lambda h: h.update(contracts={"Z9": {}})), "does not match"),
        (_edit_header(lambda h: h.update(clock="fast")), "'fast' is not one of"),
        # references between events
        (lambda ev: _data(ev, "user.vad_start").update(speech=9), "not declared"),
        (lambda ev: _data(ev, "tool.returned").update(tool=7), "no open call"),
        (lambda ev: _data(ev, "effect.committed").update(request=7), "no open request"),
        (lambda ev: _data(ev, "tts.synthesized").update(attribution="none"), "call must be set"),
        (
            lambda ev: _data(ev, "playout.finished")["words_completed_us"].pop(),
            "one completion time",
        ),
        (lambda ev: ev.append({**copy.deepcopy(ev[-1]), "seq": len(ev)}), "only run.end"),
        # things a real run cannot produce
        (
            lambda ev: _first(ev, "playout.paused").update(kind="playout.resumed"),
            "pauses while playing and resumes while paused",
        ),
        # The schema's own words differ between jsonschema releases: the place is checked.
        (
            lambda ev: _data(ev, "user.transcript_final").update(text=""),
            "(user.transcript_final): data/text: ''",
        ),
        (
            lambda ev: _first(ev, "llm.response")["data"]["tool_calls"].append(
                dict(_first(ev, "llm.response")["data"]["tool_calls"][0])
            ),
            "is used twice",
        ),
        (
            lambda ev: _data(ev, "tts.synthesized")["word_samples"].pop(),
            "one stamp and one sample count per word",
        ),
        (
            lambda ev: _data(ev, "tool.returned").pop("expects_reply"),
            "'expects_reply' is a required property",
        ),
        (
            lambda ev: _data(ev, "tts.synthesized").update(text="", stamps=[], word_samples=[]),
            "cannot be attributed to a reply",
        ),
        (
            lambda ev: _data(ev, "playout.finished").update(utterances=[99]),
            "utterance 99 was never synthesized",
        ),
        (
            lambda ev: _data(ev, "playout.started").update(utterance=99),
            "utterance 99 was never synthesized",
        ),
        (
            lambda ev: _data(ev, "playout.finished")["words_played"].__setitem__(0, 9999),
            "belongs to none of the segment's utterances",
        ),
        (
            lambda ev: ev.insert(5, {**copy.deepcopy(_first(ev, "user.vad_start")), "seq": 5}),
            "recorded twice for speech 0",
        ),
        (
            _edit_header(lambda h: h["scenario"].pop("witnessed_tools")),
            "'witnessed_tools' is a required property",
        ),
        (_edit_header(lambda h: h["scenario"].pop("delays")), "'delays' is a required property"),
        (
            _edit_header(
                lambda h: h["scenario"]["delays"].append({"of": "llm.speed", "nth": 1, "us": 0})
            ),
            "is not one of",
        ),
        # the two speeches are 2.5 s apart: each is a segment of its own to the VAD
        (
            lambda ev: next(e for e in ev if e["kind"] == "user.vad_end")["data"].update(speech=1),
            "recorded twice for speech 1",
        ),
        (
            _edit_header(lambda h: h["scenario"]["speeches"][1].update(start_us=2_700_000)),
            "speech 1 does not open a segment the VAD would hear",
        ),
        (
            lambda ev: _data(ev, "effect.requested").update(intent="book-tabel-1"),
            "undeclared intent",
        ),
        (
            lambda ev: _first(ev, "llm.response")["data"]["claims"].append(
                {"intent": "nope", "first_word": 0, "last_word": 0}
            ),
            "undeclared intent",
        ),
        (
            _edit_header(lambda h: h["scenario"]["speeches"][0]["retires"].append("nope")),
            "retires undeclared intent",
        ),
    ],
)
def test_validator_rejects(booking, mutate, expected):
    errors = _broken(booking, mutate)
    assert any(expected in e for e in errors), errors


@pytest.mark.parametrize("early_us", [0, 1])
def test_a_speech_may_start_as_the_one_before_it_ends_and_no_earlier(booking, early_us):
    events = copy.deepcopy(booking)
    first, second = events[0]["data"]["scenario"]["speeches"]
    second["start_us"] = first["end_us"] - early_us
    _reseal(events)
    refused = [e for e in validate(events) if "starts before speech 0 has ended" in e]
    assert len(refused) == early_us


def test_schema_version():
    assert SCHEMA == "earful.ledger/1"
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
        (
            lambda ev: ev[-1]["data"].update(harness_errors=["tap broke"]),
            "harness error: tap broke",
        ),
        (_edit_header(lambda h: h.update(clock="real")), "real clock"),
        (lambda ev: ev.pop(), "schema:"),
    ],
)
def test_validity_names_each_reason(booking, edit, expected):
    events = copy.deepcopy(booking)
    assert validity(events) == []
    edit(events)
    assert any(expected in reason for reason in validity(events)), validity(events)


def test_streaming_metadata_cannot_start_after_it_is_finalized(booking):
    events = copy.deepcopy(booking)
    synthesized = _first(events, "tts.synthesized")
    synthesized["data"]["started_us"] = synthesized["t_us"] + 1
    assert any("starts after" in error for error in validate(events))


def test_playout_cannot_precede_the_declared_first_audio(booking):
    events = copy.deepcopy(booking)
    synthesized = _first(events, "tts.synthesized")
    started = _first(events, "playout.started")
    synthesized["data"]["ttfb_us"] = started["t_us"] - synthesized["data"]["started_us"] + 1
    assert any("precedes" in error for error in validate(events))


def _with_ids(order, first="item_aaaaaaaaaaaa", second="item_bbbbbbbbbbbb"):
    """Events in which the framework's ids and an application's id look alike."""
    item = {"type": "function_call_output", "id": second, "output": order, "call_id": order}
    third = first[:-1] + "9"
    return [
        {"source": "llm", "kind": "llm.request", "data": {"call": 1, "items": [{"id": first}]}},
        {"source": "world", "kind": "tool.returned", "data": {"output": {"id": order}}},
        {"source": "backend", "kind": "effect.requested", "data": {"payload": {"order": order}}},
        {"source": "framework", "kind": "fw.conversation_item_added", "data": {"item": item}},
        {"source": "framework", "kind": "fw.speech_created", "data": {"speech_id": third}},
        {"source": "harness", "kind": "history.final", "data": {"items": [item, {"id": first}]}},
    ]


def test_only_the_frameworks_own_identifiers_are_replaced_by_ordinals():
    order = "order_0123456789ab"
    lines = normalize(_with_ids(order))
    assert '"items":[{"id":"item_#1"}]' in lines[0]
    assert order in lines[1] and order in lines[2]  # what the application wrote stays
    assert f'"call_id":"{order}","id":"item_#2","output":"{order}"' in lines[3]
    assert '"speech_id":"item_#3"' in lines[4]
    assert lines[5].count("item_#2") == lines[5].count("item_#1") == 1
    assert "aaaa" not in "".join(lines) and "bbbb" not in "".join(lines)
    # The ordinals are the same whatever the framework drew; an application's id is data.
    assert ledger_hash(_with_ids(order, "item_cccccccccccc", "item_dddddddddddd")) == ledger_hash(
        _with_ids(order)
    )
    assert ledger_hash(_with_ids("order_ba9876543210")) != ledger_hash(_with_ids(order))


def test_every_committed_ledger_is_a_valid_run():
    paths = sorted(GOLDEN.glob("*.ledger.normalized.jsonl"))
    assert paths
    for path in paths:
        assert validity(read_jsonl(path)) == [], path.name


def test_a_normalized_ledger_file_is_its_own_normal_form():
    for path in sorted(GOLDEN.glob("*.ledger.normalized.jsonl")):
        assert normalize(read_jsonl(path)) == path.read_text("ascii").splitlines(), path.name


def test_a_ledger_of_another_format_is_said_to_be_so_once(booking):
    events = [{**e, "schema": "earful.ledger/2"} for e in booking]
    assert validate(events) == [
        "the ledger is earful.ledger/2, and this version of Earful reads earful.ledger/1"
    ]
    mixed = [events[0], *booking[1:]]  # one event of another format is said of it alone
    assert len(validate(mixed)) == 1 and validate(mixed)[0].startswith("event 0 (run.start)")


def _one(events, kind, n=0):
    return [e for e in events if e["kind"] == kind][n]


def _numbered(events):
    return [{**e, "seq": n} for n, e in enumerate(events)]


def _committed_twice(events):
    request, commit = _one(events, "effect.requested"), _one(events, "effect.committed")
    again = [copy.deepcopy(request), copy.deepcopy(commit)]
    for e in again:
        e["data"]["request"] = 2  # a second write, given the first one's receipt
    at = events.index(commit) + 1
    return _numbered(events[:at] + again + events[at:])


def _called_by_nobody(events):
    _one(events, "tool.called")["data"]["call_id"] = "nope"
    return events


def _a_stamp_played_four_times(events):
    _one(events, "playout.finished", 1)["data"]["words_played"] = [19, 19, 19, 19]
    return events


DAMAGED = {
    "two commits with one receipt": _committed_twice,
    "a tool call no model asked for": _called_by_nobody,
    "words played that repeat one stamp": _a_stamp_played_four_times,
    "no final history": lambda ev: _numbered([e for e in ev if e["kind"] != "history.final"]),
    "seq and t_us as floats": lambda ev: [
        {**e, "seq": float(e["seq"]), "t_us": float(e["t_us"])} for e in ev
    ],
}


@pytest.mark.parametrize("damage", DAMAGED)
def test_the_validator_refuses_a_ledger_no_run_could_have_written(damage):
    events = read_jsonl(GOLDEN / "lk-1.8.12.book_then_interrupt.ledger.normalized.jsonl")
    assert validity(events) == []
    assert validity(DAMAGED[damage](events)), damage
