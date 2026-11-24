"""Wire the scripted services and observe the pipeline worker."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, ClassVar

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
    Frame,
    InterruptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from ..core.backend import EffectBackend
from ..core.ledger import Ledger
from ..scripted.kernel.run import RunContext
from ..scripted.world import WorldOrigin
from . import pipeline as pipelines
from . import seams
from .case import Case
from .clock import VirtualClock
from .config import _header
from .pipeline import Ends
from .providers import (
    SAMPLE_RATE,
    ScriptedLLM,
    ScriptedSTT,
    ScriptedUser,
    StampedTTS,
    summarize,
)
from .sink import ClockedOutput
from .tools import ToolWitness


class Tap(FrameProcessor):
    """The framework's own account of the turn, at the end of the pipeline. Explanatory:
    recorded as ``fw.*`` events, which decide no verdict."""

    _NAMES: ClassVar[dict[type[Frame], str]] = {
        UserStartedSpeakingFrame: "user_turn_started",
        UserStoppedSpeakingFrame: "user_turn_stopped",
        BotStartedSpeakingFrame: "bot_started_speaking",
        BotStoppedSpeakingFrame: "bot_stopped_speaking",
        InterruptionFrame: "interruption",
    }

    def __init__(self, ledger: Ledger) -> None:
        super().__init__()
        self._ledger = ledger
        self.user_speaking = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        name = self._NAMES.get(type(frame))
        if name is not None and direction is FrameDirection.DOWNSTREAM:
            if isinstance(frame, (UserStartedSpeakingFrame, UserStoppedSpeakingFrame)):
                self.user_speaking = isinstance(frame, UserStartedSpeakingFrame)
            self._ledger.append("framework", f"fw.{name}")
        await self.push_frame(frame, direction)


# On the real clock a sleep ends late. An output with this much audio buffered plays on.
REAL_CLOCK_LATE_US = 20_000
# Pipecat names an audio context by a random id and a processor by a count kept per process.
_UNSTABLE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|(?<=#)\d+")


@dataclass
class _Scripted:
    """Earful's side of a run: the backend, the scripted user and recognizer, the model, the
    voice and the output."""

    backend: EffectBackend
    origin: WorldOrigin
    witness: ToolWitness
    left: asyncio.Event  # set when the user hangs up
    user: ScriptedUser
    stt: ScriptedSTT
    model: ScriptedLLM
    voice: StampedTTS
    output: ClockedOutput


def _scripted(case: Case, context: RunContext) -> _Scripted:
    ledger, errors, virtual = context.ledger, context.errors, context.virtual
    backend, declared_us = context.backend, context.declared_us
    origin = context.origin
    witness = ToolWitness(ledger, case.effects)
    left = context.disconnected
    user = ScriptedUser(
        ledger,
        case.speeches,
        origin,
        left,
        start_secs=case.vad_start_secs,
        stop_secs=case.vad_stop_secs,
    )
    stt = ScriptedSTT(ledger, case.speeches, origin, left)
    model = ScriptedLLM(
        ledger,
        case.policy,
        errors,
        witness,
        ttft_us=declared_us("llm.ttft"),
        duration_us=declared_us("llm.duration"),
    )
    voice = StampedTTS(
        ledger,
        model.register,
        seconds_per_letter=case.seconds_per_letter,
        ttfb=case.tts_ttfb,
        ttfb_us=declared_us("tts.ttfb"),
        word_timestamps=case.tts_word_timestamps,
    )
    output = ClockedOutput(ledger, voice.table, late_us=0 if virtual else REAL_CLOCK_LATE_US)
    return _Scripted(backend, origin, witness, left, user, stt, model, voice, output)


def _built(case: Case, s: _Scripted) -> tuple[LLMContext, pipelines.Plan, dict[str, Any]]:
    """The bot's context, the pipeline as inspected, and the header of the run."""
    model, witness = s.model, s.witness
    context = case.bot(model)
    if not isinstance(context, LLMContext):
        raise ValueError(  # noqa: TRY004 - invalid cases are refused with ValueError
            f"bot must return an LLMContext, got {type(context).__name__}"
        )
    ends = Ends(s.user, s.stt, model, s.voice, s.output, context)
    timeouts = (case.user_speech_timeout, case.user_turn_stop_timeout)
    build = case.pipeline or pipelines.standard(*timeouts)
    plan = pipelines.inspect(build(ends), ends, *timeouts, own=case.pipeline is not None)
    seams.check_service(model)
    model.witness_tools(context.tools)
    header = _header(case, witness.names, plan.options)
    history = summarize(context.get_messages(), {}, {})
    if history:
        header["history"] = history
    model.listed = frozenset(witness.names)

    def committed(_aggregator: Any, message: Any) -> None:
        if message.interrupted and message.content:
            for item in reversed(context.get_messages()):
                if (
                    isinstance(item, dict)
                    and item.get("role") == "assistant"
                    and item.get("content") == message.content
                ):
                    model.interrupted[id(item)] = item
                    break

    plan.assistant.add_event_handler("on_assistant_turn_stopped", committed)
    return context, plan, header


def _worker(
    plan: pipelines.Plan,
    s: _Scripted,
    ledger: Ledger,
    errors: list[str],
    reported: list[str],
    virtual: bool,
) -> tuple[PipelineWorker, Tap]:
    """The worker that runs the pipeline. An error a processor raises is the run's, and one
    Pipecat reports of itself is recorded and counted in ``reported``."""
    tap = Tap(ledger)
    pipeline = Pipeline([*plan.processors, tap])
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(audio_out_sample_rate=SAMPLE_RATE),
        clock=VirtualClock() if virtual else None,
        enable_rtvi=False,
        enable_turn_tracking=False,
        idle_timeout_secs=None,
    )

    def pipeline_error(_worker: PipelineWorker, frame: ErrorFrame) -> None:
        if any(exc is frame.exception for exc in s.witness.exceptions):
            return  # a tool raised: the witness has recorded it
        if frame.exception is not None or frame.fatal:
            message = _UNSTABLE.sub("#", frame.error)
            if frame.processor is not None:
                message = f"{type(frame.processor).__name__}: {message}"
            if message not in errors:
                errors.append(message)
            return
        # Something Pipecat says of itself, such as a voice that sent no audio in time.
        reported.append(frame.error)
        ledger.append(
            "framework",
            "fw.error",
            error=_UNSTABLE.sub("#", frame.error),
        )

    worker.add_event_handler("on_pipeline_error", pipeline_error)
    return worker, tap
