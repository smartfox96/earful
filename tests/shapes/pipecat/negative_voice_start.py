"""A scenario that sets the voice detector's start time to a negative number of seconds."""

from __future__ import annotations

from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.llm_service import LLMService

from earful.pipecat import Case, ScriptedModel, UserSpeech
from shapes.corpus import invalid, refused


def bot(llm: LLMService) -> LLMContext:
    return LLMContext()


def case() -> Case:
    return Case(
        name="negative_voice_start",
        bot=bot,
        speeches=[UserSpeech(start=0.5, end=1.0, transcript="Hello")],
        policy=ScriptedModel().otherwise(say="Good morning."),
        vad_start_secs=-1,
    )


SHOULD = refused("vad_start_secs")
# The run goes ahead, and its own header fails validation.
DEFECT = invalid("less than the minimum of 0")
