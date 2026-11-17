"""Between a ``Case`` and its data: the LiveKit side of earful.case/3.

The core moves, reduces and saves cases as plain data (``earful.core.case``). This module
turns a harness ``Case`` into that data and back, and runs the data: the one function the
boundary scheduler and the shrinker need from an adapter.

    data = default("earful.livekit.cases:book_then_interrupt")
    events = run(data)          # a real AgentSession, on the virtual clock

A scenario is named by ``module:function``. The function takes no arguments and returns
a ``Case``: the agent, the model policy and the defaults. The data then overrides the
speeches, the hangup, the declared delays and faults, the settings, the seed and the
contract policy.

Running a case imports and calls the function it names. Run only cases you trust, as you
would only run test code you trust.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import inspect
import multiprocessing
import platform
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from importlib import metadata
from typing import Any

import livekit.agents as lk
from livekit import rtc

from ..core import case as case_format
from ..core.timebase import to_seconds, to_us
from .harness import Case, Delay, Fault, run_case
from .providers import UserSpeech

# Setting name -> the Case field that holds it, in seconds.
_TIMES = {
    "seconds_per_letter_us": "seconds_per_letter",
    "tts_ttfb_us": "tts_ttfb",
    "vad_min_speech_us": "vad_min_speech",
    "vad_min_silence_us": "vad_min_silence",
    "min_interruption_duration_us": "min_interruption_duration",
    "false_interruption_timeout_us": "false_interruption_timeout",
    "min_endpointing_delay_us": "min_endpointing_delay",
    "max_endpointing_delay_us": "max_endpointing_delay",
    "drain_delay_us": "drain_delay",
    "drain_timeout_us": "drain_timeout",
}
# Setting name -> the Case field that holds it as it is, and the kind of value it takes.
_PLAIN: dict[str, tuple[str, type]] = {
    "backend_mode": ("backend_mode", str),
    "sink_can_pause": ("sink_can_pause", bool),
}

SETTINGS = (*_TIMES, *_PLAIN)


def resolve(scenario: str) -> Callable[[], Case]:
    """The function a ``module:function`` reference names. Raises ``ValueError`` if there
    is no such module or function."""
    module, _, name = scenario.partition(":")
    try:
        target: Any = importlib.import_module(module)
        for part in name.split("."):
            target = getattr(target, part)
    except (ImportError, AttributeError) as exc:
        raise ValueError(f"no scenario {scenario!r}: {exc}") from exc
    if not callable(target):
        raise ValueError(f"scenario {scenario!r} is not a function")
    return target


def fingerprint(scenario: str) -> str:
    """A digest of the source of the module a scenario lives in. A saved case names its
    code by reference; this is how a replay can tell that the code has since changed."""
    module = inspect.getmodule(resolve(scenario))
    try:
        source = inspect.getsource(module) if module is not None else ""
    except (OSError, TypeError):
        source = ""
    return hashlib.sha256(source.encode("utf-8")).hexdigest() if source else ""


def describe(case: Case, scenario: str, seed: int = 0) -> dict[str, Any]:
    """The data of ``case``, as earful.case/3. ``scenario`` is the reference that will
    produce its code again."""
    return {
        "schema": case_format.SCHEMA,
        "scenario": scenario,
        "seed": seed,
        "speeches": [
            {
                "start_us": speech.start_us,
                "end_us": speech.end_us,
                "transcript": speech.transcript,
                "stt_delay_us": speech.stt_delay_us,
                "expects_reply": speech.expects_reply,
                "backchannel": speech.backchannel,
                "retires": list(speech.retires),
            }
            for speech in sorted(case.speeches, key=lambda s: s.start)
        ],
        "hangup_us": None if case.hangup is None else to_us(case.hangup),
        "delays": [
            {"of": delay.of, "nth": delay.nth, "us": to_us(delay.seconds)}
            for delay in sorted(case.delays, key=lambda d: (d.of, d.nth))
        ],
        "faults": [
            {
                "of": fault.of,
                "nth": fault.nth,
                "us": None if fault.seconds is None else to_us(fault.seconds),
            }
            for fault in sorted(case.faults, key=lambda f: (f.of, f.nth))
        ],
        "settings": {
            **{name: to_us(getattr(case, attr)) for name, attr in _TIMES.items()},
            **{name: getattr(case, attr) for name, (attr, _kind) in _PLAIN.items()},
        },
        "contracts": copy.deepcopy(case.contracts),
    }


def default(scenario: str, seed: int = 0) -> dict[str, Any]:
    """The scenario as its author wrote it."""
    return describe(resolve(scenario)(), scenario, seed)


def build(data: dict[str, Any]) -> Case:
    """The ``Case`` that ``data`` describes: the scenario's code, with the data's
    speeches, hangup, delays, faults, settings and contract policy. Raises ``ValueError`` on data that is
    not earful.case/3, names a setting this adapter does not have or gives one the wrong
    kind of value, or names a scenario that does not exist."""
    errors = case_format.validate(data)
    if errors:
        raise ValueError("not an earful.case/3 document: " + "; ".join(errors))
    for name, value in data["settings"].items():
        if name not in SETTINGS:
            raise ValueError(f"unknown setting {name!r}; this adapter has {sorted(SETTINGS)}")
        wanted = int if name in _TIMES else _PLAIN[name][1]
        if not isinstance(value, wanted) or (
            wanted is int and (isinstance(value, bool) or value < 0)
        ):
            raise ValueError(
                f"setting {name!r} takes a {'time in microseconds' if wanted is int else wanted.__name__}, not {value!r}"
            )
    case = resolve(data["scenario"])()
    case.speeches = [
        UserSpeech(
            start=to_seconds(speech["start_us"]),
            end=to_seconds(speech["end_us"]),
            transcript=speech["transcript"],
            stt_delay=to_seconds(speech["stt_delay_us"]),
            expects_reply=speech["expects_reply"],
            backchannel=speech["backchannel"],
            retires=tuple(speech["retires"]),
        )
        for speech in data["speeches"]
    ]
    case.hangup = None if data["hangup_us"] is None else to_seconds(data["hangup_us"])
    case.delays = [Delay(d["of"], d["nth"], to_seconds(d["us"])) for d in data["delays"]]
    case.faults = [
        Fault(f["of"], f["nth"], None if f["us"] is None else to_seconds(f["us"]))
        for f in data["faults"]
    ]
    for name, value in data["settings"].items():
        if name in _TIMES:
            setattr(case, _TIMES[name], to_seconds(value))
        else:
            setattr(case, _PLAIN[name][0], value)
    case.contracts = copy.deepcopy(data["contracts"])
    return case


def run(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Run the case ``data`` describes and return its ledger. A run that is not valid
    evidence is returned all the same; the checker says why and gives no verdicts.
    Raises ``ValueError`` if the case is refused before it runs."""
    return run_case(build(data), seed=data["seed"], require_valid=False).ledger.events


def pool(workers: int | None = None) -> ProcessPoolExecutor:
    """Worker processes for running cases side by side: ``pool().map`` is what the
    scheduler's ``each`` takes. Each worker starts as a fresh interpreter, on every
    platform, so nothing of this process's event loops or threads is carried into it."""
    return ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"))


def environment() -> dict[str, str]:
    """What a saved case was run with: the framework, Earful, the interpreter, the
    platform, and a digest of every installed distribution and its version."""
    installed = sorted(
        f"{d.metadata['Name']}=={d.version}" for d in metadata.distributions() if d.metadata["Name"]
    )
    return {
        "livekit-agents": lk.__version__,
        "livekit": rtc.__version__,
        "earful": metadata.version("earful"),
        "python": platform.python_version(),
        "platform": f"{platform.system()}-{platform.machine()}",
        "packages": hashlib.sha256("\n".join(installed).encode("utf-8")).hexdigest(),
    }


__all__ = [
    "SETTINGS",
    "build",
    "default",
    "describe",
    "environment",
    "fingerprint",
    "pool",
    "resolve",
    "run",
]
