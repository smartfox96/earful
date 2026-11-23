"""Validation of earful.ledger/1, and the one definition of a valid run."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache, lru_cache
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator, validators

from .ledger import SCHEMA, run_id

Event = dict[str, Any]

_IntegerValidator = validators.extend(  # type: ignore[no-untyped-call]
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine("integer", lambda _, n: type(n) is int),
)

SCHEMA_FILE = "earful.ledger-1.schema.json"


@lru_cache(maxsize=1)
def load_schema() -> dict[str, Any]:
    """The JSON Schema for one ledger event."""
    text = resources.files(__package__).joinpath("schemas").joinpath(SCHEMA_FILE).read_text("utf-8")
    return json.loads(text)


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = load_schema()
    Draft202012Validator.check_schema(schema)
    return _IntegerValidator(schema)


@cache
def _validator_for(kind: str) -> Draft202012Validator:
    # Rules for other kinds cannot apply, and testing them all was most of the cost.
    schema = load_schema()
    rules = [
        rule
        for rule in schema["allOf"]
        if rule["if"]["properties"]["kind"].get("const", kind)
        == kind  # its own rule, and any not tied to one kind
    ]
    return _IntegerValidator({**schema, "allOf": rules})


def event_errors(event: Any) -> list[Any]:
    """The schema's errors for one event, ordered by where in the event they are."""
    kind = event.get("kind") if isinstance(event, dict) else None
    validator = _validator_for(kind) if isinstance(kind, str) and kind in kinds() else _validator()
    return sorted(validator.iter_errors(event), key=lambda e: list(map(str, e.absolute_path)))


@lru_cache(maxsize=1)
def kinds() -> dict[str, str]:
    """Every Earful-owned event kind and the source that records it."""
    out = {}
    for rule in load_schema()["allOf"]:
        kind = rule["if"]["properties"]["kind"].get("const")
        if kind is not None:
            out[kind] = rule["then"]["properties"]["source"]["const"]
    return out


def _where(index: int, event: Any) -> str:
    kind = event.get("kind") if isinstance(event, dict) else None
    return f"event {index} ({kind})" if isinstance(kind, str) else f"event {index}"


def validate(events: list[Event]) -> list[str]:
    """Every way ``events`` departs from earful.ledger/1. An empty list means valid."""
    if not events:
        return ["ledger is empty"]
    named = {
        event["schema"]
        for event in events
        if isinstance(event, dict) and isinstance(event.get("schema"), str)
    }
    if len(named) == 1 and (other := named.pop()) != SCHEMA and isinstance(other, str):
        return [f"the ledger is {other}, and this version of Earful reads {SCHEMA}"]

    errors: list[str] = []
    for i, event in enumerate(events):
        for error in event_errors(event):
            path = "/".join(str(p) for p in error.absolute_path) or "event"
            errors.append(f"{_where(i, event)}: {path}: {error.message}")
    if errors:
        return errors  # order and references are only meaningful on well-formed events
    _check_ledger(events, errors)
    return errors


def _check_ledger(events: list[Event], errors: list[str]) -> None:
    if not _framed(events, errors):
        return
    header = events[0]["data"]
    _check_sequence(events, run_id(header), errors)
    scenario = header["scenario"]
    _check_speeches(scenario["speeches"], errors)
    declared = _check_intents(scenario, errors)
    _check_effects(scenario, errors)
    origins = [e for e in events if e["kind"] == "user.origin"]
    if len(origins) > 1:
        errors.append("user.origin recorded more than once")
    _check_hangup(events, header, origins, errors)
    walk = _Walk(scenario["speeches"], declared, origins[0]["seq"] if origins else None, errors)
    walk.segments(scenario["providers"]["vad"]["min_silence_us"])
    for i, e in enumerate(events):
        check = _BY_KIND.get(e["kind"])
        if check is not None:
            check(walk, e, _where(i, e))
    _check_playouts(walk)


def _framed(events: list[Event], errors: list[str]) -> bool:
    kinds_seen = [e["kind"] for e in events]
    if kinds_seen[0] != "run.start" or kinds_seen.count("run.start") != 1:
        errors.append("the ledger must begin with its only run.start")
    if kinds_seen.count("history.final") != 1:
        errors.append("the ledger must contain exactly one history.final")
    if kinds_seen[-1] != "run.end" or kinds_seen.count("run.end") != 1:
        errors.append("the ledger must end with its only run.end")
    return not errors


def _check_sequence(events: list[Event], run: str, errors: list[str]) -> None:
    last_t = 0
    for i, e in enumerate(events):
        where = _where(i, e)
        if e["seq"] != i:
            errors.append(f"{where}: seq {e['seq']}, expected {i}")
        if e["t_us"] < last_t:
            errors.append(f"{where}: t_us {e['t_us']} is earlier than the previous event")
        last_t = max(last_t, e["t_us"])
        if e["run"] != run:
            errors.append(f"{where}: run {e['run']!r} is not the digest of run.start ({run!r})")
        if e.get("schema_minor", 0) != events[0].get("schema_minor", 0):
            errors.append(f"{where}: schema_minor differs from run.start")


def _check_speeches(speeches: list[dict[str, Any]], errors: list[str]) -> None:
    for n, speech in enumerate(speeches):
        if speech["speech"] != n:
            errors.append(f"run.start speech {n}: speech index must be {n}")
        if speech["end_us"] < speech["start_us"]:
            errors.append(f"run.start speech {n}: ends before it starts")
        if n and speech["start_us"] < speeches[n - 1]["end_us"]:
            errors.append(f"run.start speech {n}: starts before speech {n - 1} has ended")


def _check_intents(scenario: dict[str, Any], errors: list[str]) -> set[str]:
    """The intents the scenario declares."""
    intents = [it["intent"] for it in scenario["intents"]]
    if len(set(intents)) != len(intents):
        errors.append("run.start: an intent is declared more than once")
    declared = set(intents)
    for n, speech in enumerate(scenario["speeches"]):
        for intent in speech["retires"]:
            if intent not in declared:
                errors.append(f"run.start speech {n}: retires undeclared intent {intent!r}")
    return declared


def _check_effects(scenario: dict[str, Any], errors: list[str]) -> None:
    replaced = [effect["tool"] for effect in scenario.get("effects", [])]
    if len(set(replaced)) != len(replaced):
        errors.append("run.start: a tool has more than one effect")
    for effect in scenario.get("effects", []):
        tool, intent = effect["tool"], effect["intent"]
        if tool not in scenario["witnessed_tools"]:
            errors.append(f"run.start: an effect is declared for {tool!r}, which is not witnessed")
        carried_out_by = next(
            (it["tools"] for it in scenario["intents"] if it["intent"] == intent), None
        )
        if carried_out_by is None:
            errors.append(f"run.start: the effect of {tool!r} is for undeclared intent {intent!r}")
        elif tool not in carried_out_by:
            errors.append(f"run.start: {tool!r} is not among the tools of its intent {intent!r}")


def _check_hangup(
    events: list[Event], header: dict[str, Any], origins: list[Event], errors: list[str]
) -> None:
    scenario = header["scenario"]
    speeches = scenario["speeches"]
    hangups = [e for e in events if e["kind"] == "user.hangup"]
    if len(hangups) > 1:
        errors.append("user.hangup recorded more than once")
    for e in hangups[:1]:
        where = _where(e["seq"], e)
        if scenario["hangup_us"] is None:
            errors.append(f"{where}: the scenario declares no hangup")
        elif not origins or origins[0]["seq"] > e["seq"]:
            errors.append(f"{where}: comes before user.origin")
        elif header["clock"] == "virtual" and (
            e["t_us"] != origins[0]["t_us"] + scenario["hangup_us"]
        ):
            # To the microsecond, which only a virtual clock can keep.
            errors.append(
                f"{where}: not at the declared time, {scenario['hangup_us']} us after the origin"
            )
    if scenario["hangup_us"] is not None and any(
        s["end_us"] > scenario["hangup_us"] for s in speeches
    ):
        errors.append("run.start: the user hangs up before a speech is over")


@dataclass
class _Walk:
    """What the checks of single events have seen of the run so far."""

    speeches: list[dict[str, Any]]
    declared: set[str]
    origin_seq: int | None
    errors: list[str]
    opens: set[int] = field(default_factory=set)  # speeches that open a segment the VAD hears
    closes: set[int] = field(default_factory=set)
    speech_events: set[tuple[str, int]] = field(default_factory=set)
    requests: set[int] = field(default_factory=set)
    responses: set[int] = field(default_factory=set)
    attributed: set[int] = field(default_factory=set)
    call_ids: set[str] = field(default_factory=set)
    utterances: set[int] = field(default_factory=set)
    synthesis: dict[int, Event] = field(default_factory=dict)
    stamp_owner: dict[int, int] = field(default_factory=dict)  # stamp -> its utterance
    tools: set[int] = field(default_factory=set)
    tool_outcomes: set[int] = field(default_factory=set)
    effect_requests: dict[int, str] = field(default_factory=dict)
    effect_outcomes: set[int] = field(default_factory=set)
    gave_up: set[int] = field(default_factory=set)  # writes whose caller stopped waiting
    receipts: set[str] = field(default_factory=set)
    started: set[int] = field(default_factory=set)
    paused: set[int] = field(default_factory=set)
    finished: set[int] = field(default_factory=set)
    playouts: list[Event] = field(default_factory=list)

    def segments(self, min_silence: int) -> None:
        # What a VAD hears as one segment: speech that resumes within its silence window.
        speeches = self.speeches
        for n, speech in enumerate(speeches):
            if n == 0 or speech["start_us"] - speeches[n - 1]["end_us"] >= min_silence:
                self.opens.add(n)
            last = n == len(speeches) - 1
            if last or speeches[n + 1]["start_us"] - speech["end_us"] >= min_silence:
                self.closes.add(n)


def _user_event(walk: _Walk, e: Event, where: str) -> None:
    k, d, errors = e["kind"], e["data"], walk.errors
    if d["speech"] >= len(walk.speeches):
        errors.append(f"{where}: speech {d['speech']} is not declared")
    elif (k == "user.vad_start" and d["speech"] not in walk.opens) or (
        k == "user.vad_end" and d["speech"] not in walk.closes
    ):
        does = "open" if k == "user.vad_start" else "close"
        errors.append(f"{where}: speech {d['speech']} does not {does} a segment the VAD would hear")
    if walk.origin_seq is None or walk.origin_seq > e["seq"]:
        errors.append(f"{where}: comes before user.origin")
    if (k, d["speech"]) in walk.speech_events:
        errors.append(f"{where}: recorded twice for speech {d['speech']}")
    walk.speech_events.add((k, d["speech"]))


def _llm_request(walk: _Walk, e: Event, where: str) -> None:
    d = e["data"]
    if d["call"] in walk.requests:
        walk.errors.append(f"{where}: call {d['call']} requested twice")
    walk.requests.add(d["call"])


def _llm_response(walk: _Walk, e: Event, where: str) -> None:
    d, errors = e["data"], walk.errors
    if d["call"] not in walk.requests or d["call"] in walk.responses:
        errors.append(f"{where}: call {d['call']} has no open request")
    walk.responses.add(d["call"])
    for tool_call in d["tool_calls"]:
        if tool_call["call_id"] in walk.call_ids:
            errors.append(f"{where}: tool call id {tool_call['call_id']!r} is used twice")
        walk.call_ids.add(tool_call["call_id"])
    words = len(d["text"].split())
    for claim in d["claims"]:
        if not claim["first_word"] <= claim["last_word"] < words:
            errors.append(f"{where}: claim {claim} does not fit the reply")
        if claim["intent"] not in walk.declared:
            errors.append(
                f"{where}: claim names undeclared intent {claim['intent']!r}; "
                "the scenario has to list it among its intents"
            )


def _tts_synthesized(walk: _Walk, e: Event, where: str) -> None:
    d, errors = e["data"], walk.errors
    responses, attributed = walk.responses, walk.attributed
    if d["started_us"] > e["t_us"]:
        errors.append(f"{where}: synthesis starts after it was recorded")
    if (d["call"] is None) != (d["attribution"] in ("ambiguous", "none")):
        errors.append(f"{where}: call must be set exactly when attribution is unique or prefix")
    if d["call"] is not None:
        if d["call"] not in responses:
            errors.append(f"{where}: call {d['call']} has no response")
        if d["call"] in attributed:
            errors.append(f"{where}: call {d['call']} is attributed twice")
        attributed.add(d["call"])
    if not len(d["stamps"]) == len(d["word_samples"]) == len(d["text"].split()):
        errors.append(f"{where}: one stamp and one sample count per word expected")
    if d["call"] is not None and not d["stamps"]:
        errors.append(f"{where}: a text with no words cannot be attributed to a reply")
    expected = {"unique": 1, "prefix": 1, "none": 0}.get(d["attribution"])
    if (
        (d["call"] is not None and d["candidates"] != [d["call"]])
        or (expected is not None and len(d["candidates"]) != expected)
        or (expected is None and len(d["candidates"]) < 2)
    ):
        errors.append(f"{where}: candidates do not fit the attribution")
    if any(c not in responses or c in attributed - {d["call"]} for c in d["candidates"]):
        errors.append(f"{where}: a candidate has not answered, or is already attributed")
    if d["utterance"] in walk.utterances:
        errors.append(f"{where}: utterance {d['utterance']} synthesized twice")
    walk.utterances.add(d["utterance"])
    walk.synthesis[d["utterance"]] = e
    for stamp in d["stamps"]:
        if stamp in walk.stamp_owner:
            errors.append(f"{where}: stamp {stamp} is used twice")
        walk.stamp_owner[stamp] = d["utterance"]


def _tool_called(walk: _Walk, e: Event, where: str) -> None:
    d = e["data"]
    if d.get("call_id") is not None and d["call_id"] not in walk.call_ids:
        walk.errors.append(f"{where}: no model response requested call {d['call_id']!r}")
    if d["tool"] in walk.tools:
        walk.errors.append(f"{where}: tool {d['tool']} called twice")
    walk.tools.add(d["tool"])


def _tool_outcome(walk: _Walk, e: Event, where: str) -> None:
    d = e["data"]
    if d["tool"] not in walk.tools or d["tool"] in walk.tool_outcomes:
        walk.errors.append(f"{where}: tool {d['tool']} has no open call")
    walk.tool_outcomes.add(d["tool"])


def _effect_requested(walk: _Walk, e: Event, where: str) -> None:
    d, errors = e["data"], walk.errors
    if d["request"] in walk.effect_requests:
        errors.append(f"{where}: request {d['request']} made twice")
    if d["tool"] is not None and d["tool"] not in walk.tools:
        errors.append(f"{where}: tool {d['tool']} was never called")
    if d["intent"] not in walk.declared:
        errors.append(
            f"{where}: write for undeclared intent {d['intent']!r}; "
            "the scenario has to list it among its intents"
        )
    walk.effect_requests[d["request"]] = d["intent"]


def _effect_outcome(walk: _Walk, e: Event, where: str) -> None:
    k, d, errors = e["kind"], e["data"], walk.errors
    effect_requests, effect_outcomes = walk.effect_requests, walk.effect_outcomes
    if d["request"] not in effect_requests or d["request"] in effect_outcomes:
        errors.append(f"{where}: request {d['request']} has no open request")
    elif effect_requests[d["request"]] != d["intent"]:
        errors.append(f"{where}: intent differs from the request's")
    effect_outcomes.add(d["request"])
    if k == "effect.committed":
        if d["receipt"] in walk.receipts:
            errors.append(f"{where}: receipt {d['receipt']!r} was already committed")
        walk.receipts.add(d["receipt"])
    elif k == "effect.deduplicated" and d["receipt"] not in walk.receipts:
        errors.append(f"{where}: receipt {d['receipt']!r} was never committed")


def _effect_timed_out(walk: _Walk, e: Event, where: str) -> None:
    # The caller gave up. The write may commit before or after, or never.
    d, errors = e["data"], walk.errors
    effect_requests, gave_up = walk.effect_requests, walk.gave_up
    if d["request"] not in effect_requests or d["request"] in gave_up:
        errors.append(f"{where}: request {d['request']} was not made, or its caller gave up twice")
    elif effect_requests[d["request"]] != d["intent"]:
        errors.append(f"{where}: intent differs from the request's")
    gave_up.add(d["request"])


def _playout_started(walk: _Walk, e: Event, where: str) -> None:
    d = e["data"]
    if d["segment"] in walk.started or d["segment"] in walk.finished:
        walk.errors.append(f"{where}: segment {d['segment']} cannot start here")
    walk.started.add(d["segment"])
    walk.playouts.append(e)


def _playout_paused_or_resumed(walk: _Walk, e: Event, where: str) -> None:
    k, d = e["kind"], e["data"]
    if d["segment"] not in walk.started or d["segment"] in walk.finished:
        walk.errors.append(f"{where}: segment {d['segment']} is not playing")
    elif (k == "playout.paused") == (d["segment"] in walk.paused):
        walk.errors.append(f"{where}: a segment pauses while playing and resumes while paused")
    walk.paused ^= {d["segment"]}


def _playout_finished(walk: _Walk, e: Event, where: str) -> None:
    d, errors = e["data"], walk.errors
    if d["segment"] in walk.finished:
        errors.append(f"{where}: segment {d['segment']} finished twice")
    if d["audible"] != (d["segment"] in walk.started):
        errors.append(f"{where}: audible must match whether the segment started")
    walk.finished.add(d["segment"])
    walk.playouts.append(e)
    done = d["words_completed_us"]
    if len(done) != len(d["words_played"]):
        errors.append(f"{where}: one completion time per played word expected")
    if any(t > e["t_us"] for t in done) or done != sorted(done):
        errors.append(f"{where}: completion times must be in order and no later than the event")


_BY_KIND: dict[str, Callable[[_Walk, Event, str], None]] = {
    "user.vad_start": _user_event,
    "user.vad_end": _user_event,
    "user.transcript_final": _user_event,
    "llm.request": _llm_request,
    "llm.response": _llm_response,
    "tts.synthesized": _tts_synthesized,
    "tool.called": _tool_called,
    "tool.returned": _tool_outcome,
    "tool.raised": _tool_outcome,
    "tool.cancelled": _tool_outcome,
    "effect.requested": _effect_requested,
    "effect.committed": _effect_outcome,
    "effect.deduplicated": _effect_outcome,
    "effect.failed": _effect_outcome,
    "effect.timed_out": _effect_timed_out,
    "playout.started": _playout_started,
    "playout.paused": _playout_paused_or_resumed,
    "playout.resumed": _playout_paused_or_resumed,
    "playout.finished": _playout_finished,
}


def _check_playouts(walk: _Walk) -> None:
    # A streaming utterance is recorded after its first audio, so playouts are resolved last.
    errors, synthesis = walk.errors, walk.synthesis
    for e in walk.playouts:
        d, where = e["data"], _where(e["seq"], e)
        if e["kind"] == "playout.started":
            numbers = [] if d["utterance"] is None else [d["utterance"]]
        else:
            numbers = d["utterances"]
            stamps = [*d["words_played"], *([d["word_cut"]] if d["word_cut"] is not None else [])]
            if len(stamps) != len(set(stamps)):
                errors.append(f"{where}: one word stamp occurs more than once")
            if any(walk.stamp_owner.get(stamp) not in numbers for stamp in stamps):
                errors.append(
                    f"{where}: a word played or cut belongs to none of the segment's utterances"
                )
        for number in numbers:
            if number not in synthesis:
                errors.append(f"{where}: utterance {number} was never synthesized")
            elif e["kind"] == "playout.started":
                said = synthesis[number]["data"]
                if e["t_us"] < said["started_us"] + said["ttfb_us"]:
                    errors.append(f"{where}: playout precedes the utterance's first audio")


def validity(events: list[Event]) -> list[str]:
    """Every reason this run may not produce verdicts. An empty list means the run is valid."""
    errors = validate(events)
    reasons = [f"schema: {e}" for e in errors]
    header = events[0].get("data") if events and isinstance(events[0], dict) else None
    clock = header.get("clock") if isinstance(header, dict) else None
    if isinstance(clock, str) and clock != "virtual":
        reasons.append(f"the run was on the {clock} clock")
    if errors:
        return reasons  # the counters are only meaningful in a well-formed ledger
    end = events[-1]["data"]
    if end["stamp_errors"]:
        reasons.append(f"{end['stamp_errors']} audio samples reached the sink altered")
    if end["executor_calls"]:
        reasons.append(
            f"{end['executor_calls']} pieces of work left the virtual clock for a thread"
        )
    if end["underruns"]:
        reasons.append(f"playout ran ahead of its audio {end['underruns']} times")
    reasons += [f"harness error: {e}" for e in end["harness_errors"]]
    return reasons


__all__ = ["kinds", "load_schema", "validate", "validity"]
