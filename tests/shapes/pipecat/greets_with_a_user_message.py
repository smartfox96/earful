"""A bot that speaks first: at start-up a processor appends a user message asking it to
introduce itself, and runs the model."""

from __future__ import annotations

from typing import Any

from pipecat.frames.frames import Frame, LLMMessagesAppendFrame, StartFrame
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

from earful.pipecat import Case, Ends, ScriptedModel, UserSpeech, user_said
from shapes.corpus import judged

INTRODUCE = "Please introduce yourself to the user."


class SpeaksFirst(FrameProcessor):
    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if isinstance(frame, StartFrame):
            asked: list[Any] = [{"role": "user", "content": INTRODUCE}]
            await self.push_frame(LLMMessagesAppendFrame(messages=asked, run_llm=True))


def bot(llm: LLMService) -> LLMContext:
    return LLMContext(messages=[{"role": "system", "content": "You are a host."}])


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
        SpeaksFirst(),
        pair.user(),
        ends.llm,
        ends.tts,
        ends.output,
        pair.assistant(),
    ]


def case() -> Case:
    return Case(
        name="greets_with_a_user_message",
        bot=bot,
        speeches=[UserSpeech(start=4.0, end=5.5, transcript="A table for two, please.")],
        policy=ScriptedModel()
        .when(user_said("table"), say="Of course, for two.")
        .otherwise(say="Welcome to the restaurant. How can I help?"),
        pipeline=pipeline,
    )


SHOULD = judged()
# The added message is read as a turn of the user's.
DEFECT = judged(M2="rule a, rule b", L1="milestone 1")
