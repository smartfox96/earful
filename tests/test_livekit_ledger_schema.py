"""Every ledger a LiveKit run writes validates, and the validator rejects what cannot happen."""

import copy

import pytest

pytest.importorskip("livekit.agents")

from jsonschema import Draft202012Validator
from test_ledger_schema import _broken, _data, _edit_header, _first

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


@pytest.fixture(scope="module")
def timed_out():
    case = CASES["book_then_interrupt"]()
    case.speeches = case.speeches[:1]
    case.faults = [Fault("backend.timeout", 1, 0.5)]
    case.delays = [Delay("backend.latency", 1, 2.0)]
    return run_case(case).ledger.events


def _again(kind, as_kind=None, keep=None):
    """Repeat the first event of ``kind`` right after it, as ``as_kind`` and with only ``keep``."""

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
        (
            _again("effect.committed", as_kind="effect.failed", keep=("request", "intent", "key")),
            "no open request",
        ),
        (lambda ev: _data(ev, "effect.timed_out").update(request=9), "was not made"),
        (lambda ev: _data(ev, "effect.timed_out").update(intent="book-table-2"), "intent differs"),
        (
            lambda ev: _data(ev, "effect.timed_out").pop("after_us"),
            "'after_us' is a required property",
        ),
        (
            _edit_header(lambda h: h["scenario"]["faults"][0].update(of="backend.explode")),
            "is not one of",
        ),
        (_edit_header(lambda h: h["scenario"].pop("faults")), "'faults' is a required property"),
    ],
)
def test_validator_rejects_a_faulted_write_that_cannot_have_happened(timed_out, mutate, expected):
    assert validate(timed_out) == [] and validity(timed_out) == []
    errors = _broken(timed_out, mutate)
    assert any(expected in e for e in errors), errors


@pytest.fixture(scope="module")
def hung_up():
    """The user leaves while the reply plays."""
    case = CASES["book_then_interrupt"]()
    case.speeches = case.speeches[:1]
    case.hangup = 5.0
    return run_case(case).ledger.events


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (_again("user.hangup"), "user.hangup recorded more than once"),
        (
            _edit_header(lambda h: h["scenario"].update(hangup_us=None)),
            "the scenario declares no hangup",
        ),
        (
            _edit_header(lambda h: h["scenario"].update(hangup_us=4_000_000)),
            "not at the declared time",
        ),
        (
            _edit_header(lambda h: h["scenario"].update(hangup_us=1_000_000)),
            "hangs up before a speech is over",
        ),
        (
            _edit_header(lambda h: h["scenario"].pop("hangup_us")),
            "'hangup_us' is a required property",
        ),
        (lambda ev: _first(ev, "user.hangup").update(source="harness"), "'world' was expected"),
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
    yield (
        "from another source",
        {**event, "source": "sink" if event["source"] == "world" else "world"},
    )
    data = event["data"]
    yield "with a field nobody defined", {**event, "data": {**data, "made_up": 1}}
    for name, value in data.items():
        yield (
            f"without data.{name}",
            {**event, "data": {k: v for k, v in data.items() if k != name}},
        )
        yield (
            f"with data.{name} of another type",
            {**event, "data": {**data, name: 0 if isinstance(value, str) else "wrong"}},
        )
    for other in (None, [], "event"):
        yield f"as {other!r} in place of an object", other


@pytest.mark.slow
def test_an_event_checked_by_its_kind_gets_the_whole_schemas_errors(booking, timed_out, hung_up):
    """``event_errors`` skips the rules of every other kind, which must change nothing."""
    whole = Draft202012Validator(load_schema())

    def said(errors):
        return [("/".join(map(str, e.absolute_path)), e.message) for e in errors]

    checked_as, compared, rejected = set(), 0, 0
    for events in (booking, timed_out, hung_up):
        for event in {e["kind"]: e for e in events}.values():  # one event of each kind
            for what, spoiled in _spoiled(event):
                expected = said(
                    sorted(
                        whole.iter_errors(spoiled), key=lambda e: list(map(str, e.absolute_path))
                    )
                )
                assert said(event_errors(spoiled)) == expected, (event["kind"], what)
                if what == "as recorded":
                    assert not expected, event["kind"]
                compared += 1
                rejected += bool(expected)
                if isinstance(spoiled, dict) and isinstance(spoiled.get("kind"), str):
                    checked_as.add(spoiled["kind"])
    assert set(kinds()) <= checked_as
    assert rejected > compared // 2  # most of what was compared was an error, as intended
