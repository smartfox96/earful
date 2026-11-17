"""A case as data: earful.case/3.

A scenario has two parts. Its code, the agent and the model policy, is named by a
``module:function`` reference and never leaves the module it is written in. Everything
else is data and lives here: the scripted user's speeches and when they hang up, the
provider delays and the faults the scenario declares, the settings, the seed and the
contract policy.

Every time in a case is a **variable**: something that can be moved on its own. Where a
speech sits, when it ends, how long its transcript takes; when the user hangs up; how long
a model call, a first audio or a backend write takes. The boundary scheduler moves variables, the shrinker
moves them back, and both do it through the functions here, so neither needs to know what
a speech or a delay is.

The shape is defined by a JSON Schema document shipped with this package
(``schemas/earful.case-3.schema.json``).

Framework-independent: this module must never import livekit.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator

SCHEMA = "earful.case/3"
SCHEMA_FILE = "earful.case-3.schema.json"

Case = dict[str, Any]


@lru_cache(maxsize=1)
def load_schema() -> dict[str, Any]:
    text = resources.files(__package__).joinpath("schemas", SCHEMA_FILE).read_text("utf-8")
    return json.loads(text)


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = load_schema()
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate(case: Any) -> list[str]:
    """Every way ``case`` departs from earful.case/3. An empty list means valid."""
    errors = [
        f"{'/'.join(str(p) for p in error.absolute_path) or 'case'}: {error.message}"
        for error in sorted(
            _validator().iter_errors(case), key=lambda e: list(map(str, e.absolute_path))
        )
    ]
    if errors:
        return errors
    last_end = None
    for n, speech in enumerate(case["speeches"]):
        if speech["end_us"] <= speech["start_us"]:
            errors.append(f"speeches/{n}: must end after it starts")
        if last_end is not None and speech["start_us"] < last_end:
            errors.append(f"speeches/{n}: starts before the speech before it has ended")
        last_end = speech["end_us"]
    if case["hangup_us"] is not None and last_end is None:
        # The hangup is timed from the origin, which the user's first audio sets.
        errors.append("hangup_us: a user who never speaks cannot hang up")
    elif case["hangup_us"] is not None and case["hangup_us"] < last_end:
        errors.append("hangup_us: the user hangs up before their last speech is over")
    declared = [(d["of"], d["nth"]) for d in case["delays"]]
    if len(set(declared)) != len(declared):
        errors.append("delays: a delay is declared more than once")
    faulted = [f["nth"] for f in case["faults"]]
    if len(set(faulted)) != len(faulted):
        errors.append("faults: a backend write has more than one fault")
    for n, fault in enumerate(case["faults"]):
        if (fault["us"] is None) != (fault["of"] == "backend.error"):
            errors.append(
                f"faults/{n}: a timeout or a drop says how long the caller waits; an error does not"
            )
    return errors


def canonical(case: Case) -> str:
    """One line of JSON that is the same for the same case: what a run is keyed by."""
    return json.dumps(case, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def without_speech(case: Case, index: int) -> Case:
    """``case`` with one speech removed."""
    out = copy.deepcopy(case)
    del out["speeches"][index]
    return out


# --------------------------------------------------------------------------- variables


@dataclass(frozen=True, order=True)
class Variable:
    """One time in a case that can be moved on its own.

    ``kind`` is ``speech.position`` (the whole speech, its length kept), ``speech.end``
    (its end, so its length), ``speech.stt_delay`` (how long its transcript takes), the
    name of a provider delay (``llm.ttft``, ``llm.duration``, ``tts.ttfb``,
    ``backend.latency``), or ``backend.wait``: how long the caller of a write waits before
    it is told the write timed out, for a write with a ``backend.timeout`` or
    ``backend.drop`` fault. ``user.hangup`` is when the user hangs up. ``index`` is the
    speech's place in the case, the occurrence from 1 of the delay or the write, and 0
    for the hangup.
    """

    kind: str
    index: int

    def __str__(self) -> str:
        return f"{self.kind} {self.index}"


SPEECH_KINDS = ("speech.position", "speech.end", "speech.stt_delay")
DELAY_KINDS = ("llm.ttft", "llm.duration", "tts.ttfb", "backend.latency")
WAIT = "backend.wait"
HANGUP = "user.hangup"
FAULT_KINDS = ("backend.error", "backend.timeout", "backend.drop")


def value(case: Case, variable: Variable) -> int | None:
    """What ``variable`` is in ``case``, in microseconds. None for a delay the case does
    not declare: the scenario's code then chooses it. None for the hangup of a user who
    stays."""
    if variable.kind == HANGUP:
        return case["hangup_us"]
    if variable.kind in SPEECH_KINDS:
        speech = case["speeches"][variable.index]
        return {
            "speech.position": speech["start_us"],
            "speech.end": speech["end_us"],
            "speech.stt_delay": speech["stt_delay_us"],
        }[variable.kind]
    if variable.kind == WAIT:
        return next((f["us"] for f in case["faults"] if f["nth"] == variable.index), None)
    return next(
        (d["us"] for d in case["delays"] if (d["of"], d["nth"]) == (variable.kind, variable.index)),
        None,
    )


def with_value(case: Case, variable: Variable, us: int) -> Case | None:
    """``case`` with ``variable`` set to ``us``. None if no such case exists: a negative
    time, a speech that would end before it starts, or one that would overlap or pass
    another, or a hangup before the last speech is over.

    The order of the speeches is part of the scenario. Moving "cancel that" to before the
    request it cancels is not the same conversation at another timing; it is another
    conversation.
    """
    if us < 0:
        return None
    out = copy.deepcopy(case)
    if variable.kind == HANGUP:
        out["hangup_us"] = us
        return None if validate(out) else out
    if variable.kind == WAIT:
        fault = next(
            (f for f in out["faults"] if f["nth"] == variable.index and f["us"] is not None), None
        )
        if fault is None:
            return None  # the write has no fault with a wait to move
        fault["us"] = us
        return out
    if variable.kind in DELAY_KINDS:
        out["delays"] = [
            d for d in out["delays"] if (d["of"], d["nth"]) != (variable.kind, variable.index)
        ]
        out["delays"].append({"of": variable.kind, "nth": variable.index, "us": us})
        out["delays"].sort(key=lambda d: (d["of"], d["nth"]))
        return out
    speech = out["speeches"][variable.index]
    if variable.kind == "speech.position":
        speech["start_us"], speech["end_us"] = us, us + speech["end_us"] - speech["start_us"]
    elif variable.kind == "speech.end":
        speech["end_us"] = us
    else:
        speech["stt_delay_us"] = us
    return None if validate(out) else out


def without_delay(case: Case, variable: Variable) -> Case:
    """``case`` with a declared delay dropped: the scenario's code chooses it again."""
    out = copy.deepcopy(case)
    out["delays"] = [
        d for d in out["delays"] if (d["of"], d["nth"]) != (variable.kind, variable.index)
    ]
    return out


def without_hangup(case: Case) -> Case:
    """``case`` with a user who stays to the end of the scenario."""
    out = copy.deepcopy(case)
    out["hangup_us"] = None
    return out


def with_fault(case: Case, of: str, nth: int, us: int | None = None) -> Case | None:
    """``case`` with a fault declared for backend write ``nth``. None if that write
    already has one."""
    if any(f["nth"] == nth for f in case["faults"]):
        return None
    out = copy.deepcopy(case)
    out["faults"].append({"of": of, "nth": nth, "us": us})
    out["faults"].sort(key=lambda f: (f["of"], f["nth"]))
    return None if validate(out) else out


__all__ = [
    "DELAY_KINDS",
    "FAULT_KINDS",
    "HANGUP",
    "SCHEMA",
    "SPEECH_KINDS",
    "WAIT",
    "Case",
    "Variable",
    "canonical",
    "load_schema",
    "validate",
    "value",
    "with_fault",
    "with_value",
    "without_delay",
    "without_hangup",
    "without_speech",
]
