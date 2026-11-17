"""Validation of earful.ledger/5, and the one definition of a valid run.

The shape of every event is defined by a JSON Schema document shipped with this package
(``schemas/earful.ledger-5.schema.json``), so any language can validate a ledger line
with a standard validator. This module applies that schema and adds the rules a
per-line schema cannot express: order, and references between events.

Framework-independent: this module must never import livekit.
"""

from __future__ import annotations

import json
from functools import cache, lru_cache
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator

from .ledger import run_id

Event = dict[str, Any]

SCHEMA_FILE = "earful.ledger-5.schema.json"


@lru_cache(maxsize=1)
def load_schema() -> dict[str, Any]:
    """The JSON Schema for one ledger event."""
    text = resources.files(__package__).joinpath("schemas").joinpath(SCHEMA_FILE).read_text("utf-8")
    return json.loads(text)


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = load_schema()
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@cache
def _validator_for(kind: str) -> Draft202012Validator:
    """The schema with only the rules that can apply to an event of ``kind``.

    The schema has one ``if kind is K then ...`` rule per kind. For an event of one kind
    every other such rule is skipped, so leaving them out gives the same errors, and
    testing 27 conditions per event was most of the cost of checking a ledger.
    """
    schema = load_schema()
    rules = [
        rule
        for rule in schema["allOf"]
        if rule["if"]["properties"]["kind"].get("const", kind)
        == kind  # its own rule, and any not tied to one kind
    ]
    return Draft202012Validator({**schema, "allOf": rules})


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
    """Every way ``events`` departs from earful.ledger/5. An empty list means valid."""
    if not events:
        return ["ledger is empty"]

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
    kinds_seen = [e["kind"] for e in events]
    if kinds_seen[0] != "run.start" or kinds_seen.count("run.start") != 1:
        errors.append("the ledger must begin with its only run.start")
    if kinds_seen[-1] != "run.end" or kinds_seen.count("run.end") != 1:
        errors.append("the ledger must end with its only run.end")
    if errors:
        return

    header = events[0]["data"]
    run = run_id(header)
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

    scenario = header["scenario"]
    speeches = scenario["speeches"]
    for n, speech in enumerate(speeches):
        if speech["speech"] != n:
            errors.append(f"run.start speech {n}: speech index must be {n}")
        if speech["end_us"] < speech["start_us"]:
            errors.append(f"run.start speech {n}: ends before it starts")
    intents = [it["intent"] for it in scenario["intents"]]
    if len(set(intents)) != len(intents):
        errors.append("run.start: an intent is declared more than once")
    declared = set(intents)
    for n, speech in enumerate(speeches):
        for intent in speech["retires"]:
            if intent not in declared:
                errors.append(f"run.start speech {n}: retires undeclared intent {intent!r}")

    origins = [e for e in events if e["kind"] == "user.origin"]
    if len(origins) > 1:
        errors.append("user.origin recorded more than once")
    hangups = [e for e in events if e["kind"] == "user.hangup"]
    if len(hangups) > 1:
        errors.append("user.hangup recorded more than once")
    for e in hangups[:1]:
        where = _where(e["seq"], e)
        if scenario["hangup_us"] is None:
            errors.append(f"{where}: the scenario declares no hangup")
        elif not origins or origins[0]["seq"] > e["seq"]:
            errors.append(f"{where}: comes before user.origin")
        elif e["t_us"] != origins[0]["t_us"] + scenario["hangup_us"]:
            errors.append(
                f"{where}: not at the declared time, {scenario['hangup_us']} us after the origin"
            )
    if scenario["hangup_us"] is not None and any(
        s["end_us"] > scenario["hangup_us"] for s in speeches
    ):
        errors.append("run.start: the user hangs up before a speech is over")
    requests: set[int] = set()
    responses: set[int] = set()
    attributed: set[int] = set()
    tools: set[int] = set()
    tool_outcomes: set[int] = set()
    call_ids: set[str] = set()
    paused: set[int] = set()
    effect_requests: dict[int, str] = {}
    effect_outcomes: set[int] = set()
    gave_up: set[int] = set()  # writes whose caller stopped waiting
    receipts: set[str] = set()
    started: set[int] = set()
    finished: set[int] = set()
    speech_events: set[tuple[str, int]] = set()
    stamp_owner: dict[int, int] = {}  # stamp -> the utterance it was synthesized for
    utterances: set[int] = set()

    # What a VAD hears as one segment: speech that resumes within its silence window.
    min_silence = scenario["providers"]["vad"]["min_silence_us"]
    opens, closes = set(), set()
    for n, speech in enumerate(speeches):
        if n == 0 or speech["start_us"] - speeches[n - 1]["end_us"] >= min_silence:
            opens.add(n)
        if n == len(speeches) - 1 or speeches[n + 1]["start_us"] - speech["end_us"] >= min_silence:
            closes.add(n)

    for i, e in enumerate(events):
        k, d, where = e["kind"], e["data"], _where(i, e)
        if k in ("user.vad_start", "user.vad_end", "user.transcript_final"):
            if d["speech"] >= len(speeches):
                errors.append(f"{where}: speech {d['speech']} is not declared")
            elif (k == "user.vad_start" and d["speech"] not in opens) or (
                k == "user.vad_end" and d["speech"] not in closes
            ):
                does = "open" if k == "user.vad_start" else "close"
                errors.append(
                    f"{where}: speech {d['speech']} does not {does} a segment the VAD would hear"
                )
            if not origins or origins[0]["seq"] > e["seq"]:
                errors.append(f"{where}: comes before user.origin")
            if (k, d["speech"]) in speech_events:
                errors.append(f"{where}: recorded twice for speech {d['speech']}")
            speech_events.add((k, d["speech"]))
        elif k == "llm.request":
            if d["call"] in requests:
                errors.append(f"{where}: call {d['call']} requested twice")
            requests.add(d["call"])
        elif k == "llm.response":
            if d["call"] not in requests or d["call"] in responses:
                errors.append(f"{where}: call {d['call']} has no open request")
            responses.add(d["call"])
            for tool_call in d["tool_calls"]:
                if tool_call["call_id"] in call_ids:
                    errors.append(f"{where}: tool call id {tool_call['call_id']!r} is used twice")
                call_ids.add(tool_call["call_id"])
            words = len(d["text"].split())
            for claim in d["claims"]:
                if not claim["first_word"] <= claim["last_word"] < words:
                    errors.append(f"{where}: claim {claim} does not fit the reply")
                if claim["intent"] not in declared:
                    errors.append(f"{where}: claim names undeclared intent {claim['intent']!r}")
        elif k == "tts.synthesized":
            if (d["call"] is None) != (d["attribution"] in ("ambiguous", "none")):
                errors.append(
                    f"{where}: call must be set exactly when attribution is unique or prefix"
                )
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
            if d["utterance"] in utterances:
                errors.append(f"{where}: utterance {d['utterance']} synthesized twice")
            utterances.add(d["utterance"])
            for stamp in d["stamps"]:
                if stamp in stamp_owner:
                    errors.append(f"{where}: stamp {stamp} is used twice")
                stamp_owner[stamp] = d["utterance"]
        elif k == "tool.called":
            if d["tool"] in tools:
                errors.append(f"{where}: tool {d['tool']} called twice")
            tools.add(d["tool"])
        elif k in ("tool.returned", "tool.raised", "tool.cancelled"):
            if d["tool"] not in tools or d["tool"] in tool_outcomes:
                errors.append(f"{where}: tool {d['tool']} has no open call")
            tool_outcomes.add(d["tool"])
        elif k == "effect.requested":
            if d["request"] in effect_requests:
                errors.append(f"{where}: request {d['request']} made twice")
            if d["tool"] is not None and d["tool"] not in tools:
                errors.append(f"{where}: tool {d['tool']} was never called")
            if d["intent"] not in declared:
                errors.append(f"{where}: write for undeclared intent {d['intent']!r}")
            effect_requests[d["request"]] = d["intent"]
        elif k in ("effect.committed", "effect.deduplicated", "effect.failed"):
            if d["request"] not in effect_requests or d["request"] in effect_outcomes:
                errors.append(f"{where}: request {d['request']} has no open request")
            elif effect_requests[d["request"]] != d["intent"]:
                errors.append(f"{where}: intent differs from the request's")
            effect_outcomes.add(d["request"])
            if k == "effect.committed":
                receipts.add(d["receipt"])
            elif k == "effect.deduplicated" and d["receipt"] not in receipts:
                errors.append(f"{where}: receipt {d['receipt']!r} was never committed")
        elif k == "effect.timed_out":
            # The caller gave up. The write may commit before or after, or never.
            if d["request"] not in effect_requests or d["request"] in gave_up:
                errors.append(
                    f"{where}: request {d['request']} was not made, or its caller gave up twice"
                )
            elif effect_requests[d["request"]] != d["intent"]:
                errors.append(f"{where}: intent differs from the request's")
            gave_up.add(d["request"])
        elif k == "playout.started":
            if d["segment"] in started or d["segment"] in finished:
                errors.append(f"{where}: segment {d['segment']} cannot start here")
            started.add(d["segment"])
            if d["utterance"] is not None and d["utterance"] not in utterances:
                errors.append(f"{where}: utterance {d['utterance']} was never synthesized")
        elif k in ("playout.paused", "playout.resumed"):
            if d["segment"] not in started or d["segment"] in finished:
                errors.append(f"{where}: segment {d['segment']} is not playing")
            elif (k == "playout.paused") == (d["segment"] in paused):
                errors.append(f"{where}: a segment pauses while playing and resumes while paused")
            paused ^= {d["segment"]}
        elif k == "playout.finished":
            if d["segment"] in finished:
                errors.append(f"{where}: segment {d['segment']} finished twice")
            if d["audible"] != (d["segment"] in started):
                errors.append(f"{where}: audible must match whether the segment started")
            finished.add(d["segment"])
            for utterance in d["utterances"]:
                if utterance not in utterances:
                    errors.append(f"{where}: utterance {utterance} was never synthesized")
            stamps = [*d["words_played"], *([d["word_cut"]] if d["word_cut"] is not None else [])]
            if any(stamp_owner.get(stamp) not in d["utterances"] for stamp in stamps):
                errors.append(
                    f"{where}: a word played or cut belongs to none of the segment's utterances"
                )
            done = d["words_completed_us"]
            if len(done) != len(d["words_played"]):
                errors.append(f"{where}: one completion time per played word expected")
            if any(t > e["t_us"] for t in done) or done != sorted(done):
                errors.append(
                    f"{where}: completion times must be in order and no later than the event"
                )


def validity(events: list[Event]) -> list[str]:
    """Every reason this run may not produce verdicts. An empty list means the run is valid.

    This is the single definition of a valid run (``docs/contracts.md``, section 2.4). It
    reads only the ledger.
    """
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
