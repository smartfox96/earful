"""A bot with a processor of its own between the model and the voice that raises on every
frame of text it is given."""

from __future__ import annotations

from pipecat.frames.frames import Frame, LLMTextFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import LLMService
from pipecat.turns.user_start.vad_user_turn_start_strategy import VADUserTurnStartStrategy
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from earful.pipecat import Case, Ends, ScriptedModel, UserSpeech
from shapes.corpus import invalid


class Censor(FrameProcessor):
    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMTextFrame):
            self.check(frame.text)
        await self.push_frame(frame, direction)

    def check(self, text: str) -> None:
        raise RuntimeError("the word list is not loaded")


def bot(llm: LLMService) -> LLMContext:
    return LLMContext()


def pipeline(ends: Ends) -> list[FrameProcessor]:
    turns = UserTurnStrategies(
        start=[VADUserTurnStartStrategy()],
        stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.6)],
    )
    params = LLMUserAggregatorParams(user_turn_strategies=turns, empty_user_turn=None)
    pair = LLMContextAggregatorPair(ends.context, user_params=params)
    return [
        ends.input,
        ends.stt,
        pair.user(),
        ends.llm,
        Censor(),
        ends.tts,
        ends.output,
        pair.assistant(),
    ]


def case() -> Case:
    return Case(
        name="processor_raises",
        bot=bot,
        speeches=[UserSpeech(start=0.5, end=1.5, transcript="Hello.")],
        policy=ScriptedModel().otherwise(say="Good morning, how can I help you today?"),
        pipeline=pipeline,
    )


# One reason, naming the processor.
SHOULD = invalid("Censor")
