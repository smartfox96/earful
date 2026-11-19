"""Between a ``Case`` and its data: the Pipecat side of earful.case/1.
Running a case imports and calls the ``module:function`` it names: run only cases you trust."""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pipecat

from ..scripted import scenario as shared
from ..scripted.scenario import INLINE, fingerprint, importable, pool, resolve
from .harness import Case, run_case

# Setting name -> the Case field that holds it, in seconds.
_TIMES = {
    "seconds_per_letter_us": "seconds_per_letter",
    "tts_ttfb_us": "tts_ttfb",
    "vad_start_us": "vad_start_secs",
    "vad_stop_us": "vad_stop_secs",
    "user_speech_timeout_us": "user_speech_timeout",
    "user_turn_stop_timeout_us": "user_turn_stop_timeout",
    "drain_delay_us": "drain_delay",
    "drain_timeout_us": "drain_timeout",
}
# Setting name -> the Case field that holds it as it is, and the kind of value it takes.
_PLAIN: dict[str, tuple[str, type]] = {
    "backend_mode": ("backend_mode", str),
    "tts_word_timestamps": ("tts_word_timestamps", bool),
}

SETTINGS = (*_TIMES, *_PLAIN)

Source = str | Case | Callable[[], Case]


def _copied(case: Case) -> Case:
    return dataclasses.replace(
        case,
        speeches=list(case.speeches),
        intents=list(case.intents),
        effects=dict(case.effects),
        contracts=copy.deepcopy(case.contracts),
        delays=list(case.delays),
        faults=list(case.faults),
    )


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
        if isinstance(source, cls):
            return source
        if isinstance(source, str):
            return cls(resolve(source), source)
        if isinstance(source, Case):
            return cls(lambda: _copied(source), None)
        if callable(source):
            return cls(source, shared.reference_of(source))
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


def describe(case: Case, scenario: str, seed: int = 0) -> dict[str, Any]:
    return shared.describe(case, scenario, seed, _TIMES, _PLAIN)


def default(scenario: str, seed: int = 0) -> dict[str, Any]:
    """The scenario as its author wrote it."""
    return describe(resolve(scenario)(), scenario, seed)


def build(data: dict[str, Any], make: Callable[[], Case] | None = None) -> Case:
    return shared.build(data, make, Case, _TIMES, _PLAIN)


def run(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Run the case ``data`` describes and return its ledger, valid evidence or not.
    Raises ``ValueError`` if the case is refused before it runs."""
    return run_case(build(data), seed=data["seed"], require_valid=False).ledger.events


def environment() -> dict[str, str]:
    return shared.environment({"pipecat-ai": pipecat.__version__})


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
