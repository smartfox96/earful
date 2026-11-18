"""Between a ``Case`` and its data: the LiveKit side of earful.case/1.
Running a case imports and calls the ``module:function`` it names: run only cases you trust."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import importlib
import inspect
import multiprocessing
import platform
import re
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
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

# The scenario a case names when it was built inline and cannot be built again from its data.
INLINE = "__inline__:case"
_REFERENCE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_.]*$")

Source = str | Case | Callable[[], Case]


def resolve(scenario: str) -> Callable[[], Case]:
    """The function a ``module:function`` reference names, or ``ValueError`` if there is none."""
    if scenario == INLINE:
        raise ValueError(
            "this case was built inline, in the process that ran it, so its data does not "
            "name code that can build it again. Define the scenario as a function at the "
            "top level of a module and pass that function, or its module:function name"
        )
    module, _, name = scenario.partition(":")
    try:
        target: Any = importlib.import_module(module)
        for part in name.split("."):
            target = getattr(target, part)
    except (ImportError, AttributeError) as exc:
        raise ValueError(f"no scenario {scenario!r}: {exc}") from exc
    if not callable(target):
        # ValueError like every other bad reference: callers handle one kind of refusal.
        raise ValueError(f"scenario {scenario!r} is not a function")  # noqa: TRY004
    return target


def _copied(case: Case) -> Case:
    return dataclasses.replace(
        case,
        speeches=list(case.speeches),
        intents=list(case.intents),
        effects=dict(case.effects),
        contracts=copy.deepcopy(case.contracts),
        delays=list(case.delays),
        faults=list(case.faults),
        session_kwargs=dict(case.session_kwargs),
    )


def _importable(make: Callable[[], Case]) -> str | None:
    module, name = getattr(make, "__module__", None), getattr(make, "__qualname__", None)
    if not module or not name or module == "__main__":
        return None  # a worker process has another __main__
    reference = f"{module}:{name}"
    if not _REFERENCE.match(reference):
        return None  # a lambda, or a function defined inside another
    try:
        return reference if resolve(reference) is make else None
    except ValueError:
        return None


def importable(reference: str | None, to: str) -> str:
    """``reference``, or ``ValueError`` for a scenario built inline. ``to`` says what needs it."""
    if reference is None or reference == INLINE:
        raise ValueError(
            f"to {to}, the scenario has to be importable: define it as a function at the "
            "top level of a module and pass that function, or its module:function name. A "
            "Case object, a lambda and a function defined inside another can only run in "
            "the process that built them"
        )
    return reference


@dataclass(frozen=True)
class Scenario:
    """A scenario's code: what builds its ``Case``, and its name if it has one."""

    make: Callable[[], Case]
    reference: str | None  # module:function; None for a scenario built inline

    @classmethod
    def of(cls, source: Source) -> Scenario:
        """The scenario ``source`` gives: a ``module:function`` reference, a function that
        returns a ``Case``, or a ``Case``, which is copied for each run. Raises ``TypeError``
        for anything else."""
        if isinstance(source, str):
            return cls(resolve(source), source)
        if isinstance(source, Case):
            return cls(lambda: _copied(source), None)
        if callable(source):
            return cls(source, _importable(source))
        raise TypeError(
            "a scenario is a Case, a function that returns one, or a module:function "
            f"reference; got {type(source).__name__}"
        )

    @property
    def name(self) -> str:
        """What the data of this scenario's cases names as their scenario."""
        return self.reference or INLINE

    def default(self, seed: int = 0) -> dict[str, Any]:
        """The scenario as its author wrote it, as earful.case/1."""
        return describe(self.make(), self.name, seed)

    def build(self, data: dict[str, Any]) -> Case:
        """The ``Case`` that ``data`` describes, with this scenario's code."""
        return build(data, self.make)

    def run(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Run the case ``data`` describes, in this process, and return its ledger."""
        return run_case(self.build(data), seed=data["seed"], require_valid=False).ledger.events


def fingerprint(scenario: str) -> str:
    """A digest of the source of the scenario's module, by which a replay sees it changed."""
    module = inspect.getmodule(resolve(scenario))
    try:
        source = inspect.getsource(module) if module is not None else ""
    except (OSError, TypeError):
        source = ""
    return hashlib.sha256(source.encode("utf-8")).hexdigest() if source else ""


def describe(case: Case, scenario: str, seed: int = 0) -> dict[str, Any]:
    """The data of ``case``, as earful.case/1, naming ``scenario`` as its code."""
    return {
        "schema": case_format.SCHEMA,
        "schema_minor": case_format.SCHEMA_MINOR,
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


def build(data: dict[str, Any], make: Callable[[], Case] | None = None) -> Case:
    """The ``Case`` that ``data`` describes: the code ``make`` or the named scenario returns,
    with the data's values. Raises ``ValueError`` for data that is not earful.case/1, a bad
    setting or a scenario that does not exist."""
    errors = case_format.validate(data)
    if errors:
        raise ValueError("not an earful.case/1 document: " + "; ".join(errors))
    for name, value in data["settings"].items():
        if name not in SETTINGS:
            raise ValueError(f"unknown setting {name!r}; this adapter has {sorted(SETTINGS)}")
        wanted = int if name in _TIMES else _PLAIN[name][1]
        if wanted is int:
            ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else:
            ok = isinstance(value, wanted)
        if not ok:
            kind = "time in microseconds" if wanted is int else wanted.__name__
            raise ValueError(f"setting {name!r} takes a {kind}, not {value!r}")
    case = (make or resolve(data["scenario"]))()
    if not isinstance(case, Case):
        raise ValueError(  # noqa: TRY004 - one kind of refusal, as for every bad scenario
            f"scenario {data['scenario']!r} returned {type(case).__name__}, not a Case"
        )
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
    """Run the case ``data`` describes and return its ledger, valid evidence or not.
    Raises ``ValueError`` if the case is refused before it runs."""
    return run_case(build(data), seed=data["seed"], require_valid=False).ledger.events


def pool(workers: int | None = None) -> ProcessPoolExecutor:
    """Worker processes for running cases side by side, each spawned as a fresh interpreter."""
    return ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"))


def environment() -> dict[str, str]:
    """What a saved case was run with, and a digest of every installed distribution."""
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
    "INLINE",
    "SETTINGS",
    "Scenario",
    "Source",
    "build",
    "default",
    "describe",
    "environment",
    "fingerprint",
    "importable",
    "pool",
    "resolve",
    "run",
]
