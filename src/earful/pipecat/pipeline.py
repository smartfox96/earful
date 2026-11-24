"""A case's own pipeline: what goes between Earful's ends, and what cannot be run."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from pipecat.pipeline.pipeline import Pipeline, PipelineSink, PipelineSource
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMAssistantAggregator,
    LLMContextAggregatorPair,
    LLMUserAggregator,
    LLMUserAggregatorParams,
)
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.ai_service import AIService
from pipecat.services.llm_service import LLMService
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.turns.user_start.transcription_user_turn_start_strategy import (
    TranscriptionUserTurnStartStrategy,
)
from pipecat.turns.user_start.vad_user_turn_start_strategy import VADUserTurnStartStrategy
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_processor import UserTurnProcessor
from pipecat.turns.user_turn_strategies import UserTurnStrategies, UserTurnStrategiesParams

from ..core.ledger import json_value
from ..core.timebase import to_us
from . import seams


@dataclass(frozen=True)
class Ends:
    """Earful's processors, and the context ``bot`` returned, for a case's own pipeline."""

    input: FrameProcessor
    stt: FrameProcessor
    llm: LLMService
    tts: FrameProcessor
    output: FrameProcessor
    context: LLMContext


Build = Callable[[Ends], "Sequence[FrameProcessor] | Pipeline"]


@dataclass(frozen=True)
class Plan:
    """A pipeline Earful can run, and what the run's header says of it."""

    processors: list[FrameProcessor]
    user: LLMUserAggregator
    assistant: LLMAssistantAggregator
    options: dict[str, Any]  # the header's session.extra_options


_ENDS = ("input", "stt", "llm", "tts", "output")
_ORDER = "ends.input, ends.stt, a user context aggregator, ends.llm, ends.tts, ends.output"
# What a pipeline may not hold, and why, besides Earful's own ends.
_UNRUNNABLE: tuple[tuple[type | tuple[type, ...], str], ...] = (
    (
        AIService,
        (
            "is a Pipecat service: Earful supplies the speech-to-text, the model and the "
            "voice, and runs none that reaches the network"
        ),
    ),
    (
        (BaseInputTransport, BaseOutputTransport),
        "is a transport: Earful supplies the input and the output",
    ),
    (VADProcessor, "runs a VAD on audio in a thread: Earful scripts the VAD and sends no audio"),
    (
        (LLMUserAggregator, LLMAssistantAggregator, UserTurnProcessor),
        (
            f"decides turns or writes the context a second time: the pipeline is {_ORDER} "
            "and an assistant context aggregator, each once and at the top level"
        ),
    ),
)
# Settings of the user aggregator the header states through fields of its own.
_STATED = frozenset(
    {
        "user_turn_strategies",
        "user_turn_strategies_params",
        "user_turn_stop_timeout",
        "vad_analyzer",
        "audio_idle_timeout",  # read by a vad_analyzer alone
    }
)
_SUMMARIES = "enable_auto_context_summarization"


def standard(user_speech_timeout: float, user_turn_stop_timeout: float) -> Build:
    """The pipeline of a case that gives none."""

    def build(ends: Ends) -> list[FrameProcessor]:
        pair = LLMContextAggregatorPair(
            ends.context,
            user_params=LLMUserAggregatorParams(
                user_turn_strategies=UserTurnStrategies(
                    start=[VADUserTurnStartStrategy()],
                    stop=[
                        SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=user_speech_timeout)
                    ],
                ),
                user_turn_stop_timeout=user_turn_stop_timeout,
                empty_user_turn=None,
            ),
        )
        return [
            ends.input,
            ends.stt,
            pair.user(),
            ends.llm,
            ends.tts,
            ends.output,
            pair.assistant(),
        ]

    return build


def _inside(processor: FrameProcessor) -> Iterator[FrameProcessor]:
    for held in processor.processors:
        yield held
        yield from _inside(held)


def _shape(processor: FrameProcessor) -> Any:
    held = [p for p in processor.processors if not isinstance(p, (PipelineSource, PipelineSink))]
    name = type(processor).__name__
    return {name: [_shape(p) for p in held]} if processor.processors else name


def _start(strategy: object) -> str:
    name = type(strategy).__name__
    if type(strategy) not in (VADUserTurnStartStrategy, TranscriptionUserTurnStartStrategy):
        raise ValueError(
            f"{name} is not a turn start the run's header can state: use "
            "VADUserTurnStartStrategy, and TranscriptionUserTurnStartStrategy with it if wanted"
        )
    changed = []
    if not seams.interruptions_enabled(strategy):
        changed.append("enable_interruptions=False")
    if isinstance(strategy, TranscriptionUserTurnStartStrategy) and not seams.uses_interim(
        strategy
    ):
        changed.append("use_interim=False")
    return f"{name}({', '.join(changed)})" if changed else name


def turns(
    user: LLMUserAggregator, user_speech_timeout: float, user_turn_stop_timeout: float
) -> dict[str, str]:
    """The user aggregator's turn strategies as the header names them, or ``ValueError``."""
    params = seams.aggregator_params(user)
    strategies = seams.turn_controller(user).user_turn_strategies
    if params.vad_analyzer is not None:
        raise ValueError(
            "a vad_analyzer hears audio in a thread: Earful scripts the VAD, which "
            "Case.vad_start_secs and Case.vad_stop_secs set"
        )
    if params.user_turn_strategies_params != UserTurnStrategiesParams():
        raise ValueError(
            "user_turn_strategies_params other than the defaults are not supported: the "
            "run's header cannot state how they combine the strategies"
        )
    start = [_start(strategy) for strategy in strategies.start or []]
    if [type(s) for s in strategies.start or []].count(VADUserTurnStartStrategy) != 1:
        raise ValueError(
            "a turn has to start on the scripted VAD: the start strategies need one "
            "VADUserTurnStartStrategy"
        )
    stop = list(strategies.stop or [])
    if any(isinstance(s, TurnAnalyzerUserTurnStopStrategy) for s in stop):
        raise ValueError(
            "TurnAnalyzerUserTurnStopStrategy, Pipecat's default, runs a turn model on the "
            "user's audio, and Earful sends none: give UserTurnStrategies "
            "stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=...)]"
        )
    if [type(s) for s in stop] != [SpeechTimeoutUserTurnStopStrategy]:
        raise ValueError(
            f"{', '.join(type(s).__name__ for s in stop) or 'no stop strategy'} is not a turn "
            "stop the run's header can state: use one SpeechTimeoutUserTurnStopStrategy"
        )
    if not stop[0].wait_for_transcript:
        raise ValueError(
            "SpeechTimeoutUserTurnStopStrategy(wait_for_transcript=False) is for a realtime "
            "model, which Earful does not script"
        )
    for name, held, declared in (
        ("user_speech_timeout", seams.speech_timeout(stop[0]), user_speech_timeout),
        ("user_turn_stop_timeout", params.user_turn_stop_timeout, user_turn_stop_timeout),
    ):
        # The case's value is in its data and in the header, so the two have to agree.
        if to_us(held) != to_us(declared):
            raise ValueError(
                f"the pipeline is built with {name}={held:g} and the case declares "
                f"{declared:g}: set Case.{name} to what the pipeline uses"
            )
    return {"user_turn_start": ", ".join(start), "user_turn_stop": type(stop[0]).__name__}


def _further(params: object, standard: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Each setting of ``params`` that is not its default, by name."""
    out = {}
    for field in dataclasses.fields(params):  # type: ignore[arg-type]
        if field.name in _STATED:
            continue
        if field.name in standard:
            usual = standard[field.name]
        elif field.default_factory is not dataclasses.MISSING:
            usual = field.default_factory()
        else:
            usual = field.default
        value = getattr(params, field.name)
        if value != usual:
            strategies = field.name == "user_mute_strategies"
            out[prefix + field.name] = (
                [type(s).__name__ for s in value] if strategies else json_value(value)
            )
    return out


def inspect(
    built: Sequence[FrameProcessor] | Pipeline,
    ends: Ends,
    user_speech_timeout: float,
    user_turn_stop_timeout: float,
    *,
    own: bool,
) -> Plan:
    """The pipeline built around ``ends`` as Earful runs it, or ``ValueError`` in one line."""
    top = list(built.processors[1:-1]) if isinstance(built, Pipeline) else list(built)
    if not all(isinstance(processor, FrameProcessor) for processor in top):
        raise ValueError("pipeline returns its processors in order, or a Pipeline of them")
    nested = [held for processor in top for held in _inside(processor)]
    ours = {name: getattr(ends, name) for name in _ENDS}
    places = []
    for name, end in ours.items():
        if sum(p is end for p in top) != 1 or any(p is end for p in nested):
            raise ValueError(
                f"the pipeline has to hold ends.{name} once, at its top level: Earful "
                f"scripts and witnesses the run there. In order: {_ORDER}"
            )
        places.append(next(i for i, p in enumerate(top) if p is end))
    users = [p for p in top if isinstance(p, LLMUserAggregator)]
    assistants = [p for p in top if isinstance(p, LLMAssistantAggregator)]
    if (
        places != sorted(places)
        or len(users) != 1
        or len(assistants) != 1
        or not places[1] < top.index(users[0]) < places[2]
        or top.index(assistants[0]) < places[4]
    ):
        raise ValueError(
            f"the pipeline has to be, in order and at its top level, {_ORDER} and an "
            "assistant context aggregator, with the application's processors between them"
        )
    user, assistant = users[0], assistants[0]
    run_as_they_are = (user, assistant, *ours.values())
    for processor in (*top, *nested):
        for kinds, why in _UNRUNNABLE:
            if isinstance(processor, kinds) and not any(processor is p for p in run_as_they_are):
                raise ValueError(f"{type(processor).__name__} {why}")
    if user.context is not ends.context or assistant.context is not ends.context:
        raise ValueError(
            "the context aggregators have to be built on ends.context, the context bot returned"
        )
    options: dict[str, Any] = turns(user, user_speech_timeout, user_turn_stop_timeout)
    kept = seams.aggregator_params(assistant)
    if seams.summaries_enabled(kept):
        raise ValueError(
            f"{_SUMMARIES} summarizes the context with a model call of its own, which "
            "Earful does not script"
        )
    if own:
        options["pipeline"] = [_shape(processor) for processor in top]
        options.update(_further(seams.aggregator_params(user), {"empty_user_turn": None}))
        options.update(_further(kept, {}, "assistant_"))
    return Plan([built] if isinstance(built, Pipeline) else top, user, assistant, options)
