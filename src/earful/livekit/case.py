"""LiveKit application options and run diagnostics."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import livekit.agents as lk

from ..scripted.case import Application, DrainOptions
from ..scripted.case import Case as ScriptedCase
from ..scripted.clock import ClockPatchReport
from ..scripted.result import RunEvidence
from . import options
from .sink import ClockedSink


@dataclass
class AgentFactory(Application):
    agent: Callable[[], lk.Agent]


@dataclass
class SessionOptions:
    vad_min_speech: float = 0.05
    vad_min_silence: float = 0.5
    sink_can_pause: bool = True
    min_interruption_duration: float = 0.5
    false_interruption_timeout: float = 2.0
    allow_interruptions: bool = True
    preemptive_generation: bool = True
    min_endpointing_delay: float = 0.5
    max_endpointing_delay: float = 6.0


@dataclass
class Case(DrainOptions, SessionOptions, ScriptedCase, AgentFactory):
    """Everything that defines a run: the same case and seed give the same ledger.
    ``agent`` takes no arguments and is called once per run. Times are in seconds."""

    sink: type[ClockedSink] = ClockedSink  # a faulty output is a subclass
    # Further AgentSession options, of those ``options.PASSED`` names.
    session_kwargs: dict[str, Any] = field(default_factory=dict)

    def check(self, seed: int = 0) -> None:
        """Raise ``ValueError`` for a scenario that contradicts itself."""
        options.refuse_session_kwargs(self.session_kwargs)
        if not callable(self.agent):
            raise ValueError(  # noqa: TRY004 - a case that is refused, as the others here
                "agent is called once for each run and returns the agent, so it is a class or "
                f"a function; got an object of {type(self.agent).__name__}. Write "
                f"agent=lambda: {type(self.agent).__name__}(...)"
            )
        from .scenario import INLINE, describe

        describe(self, INLINE, seed)


@dataclass
class RunResult(RunEvidence):
    """A run with the LiveKit clock and output diagnostics."""

    versions: dict[str, str]
    clock: ClockPatchReport
    stamp_errors: int
    executor_calls: int | None
    underruns: int
    harness_errors: list[str]
    framework_warnings: list[str]
    invalid: list[str]
