"""Pipecat application and pipeline options."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.llm_service import LLMService

from ..scripted.case import (
    Application,
    DrainOptions,
)
from ..scripted.case import Case as ScriptedCase
from . import pipeline as pipelines


def bot_context(
    bot: Callable[..., Any], model: LLMService, *args: Any, **kwargs: Any
) -> LLMContext:
    result = bot(model, *args, **kwargs)
    if inspect.iscoroutine(result):
        result.close()
        raise ValueError(
            "bot must be synchronous and return an LLMContext; it returned a coroutine"
        )
    if not isinstance(result, LLMContext):
        raise ValueError(f"bot must return an LLMContext, got {type(result).__name__}")  # noqa: TRY004
    return result


@dataclass
class BotFactory(Application):
    bot: Callable[[LLMService], LLMContext]


@dataclass
class PipelineOptions:
    vad_start_secs: float = 0.2  # Pipecat's VADParams.start_secs
    vad_stop_secs: float = 0.2  # Pipecat's VADParams.stop_secs
    tts_word_timestamps: bool = False  # whether the voice says when each word starts
    user_speech_timeout: float = 0.6  # SpeechTimeoutUserTurnStopStrategy
    user_turn_stop_timeout: float = 5.0  # LLMUserAggregatorParams


@dataclass
class Case(DrainOptions, PipelineOptions, ScriptedCase, BotFactory):
    """A scenario for a Pipecat bot. ``bot`` is the application: handed the model
    service, it registers its functions and returns the context the bot starts with.
    ``pipeline`` is the application's pipeline around Earful's ends."""

    # The application's processors in order, around Earful's ends. None: the standard seven.
    pipeline: pipelines.Build | None = None
