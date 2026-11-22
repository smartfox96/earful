"""Reference cases: one per behaviour the recorded Pipecat ledgers rest on."""

from __future__ import annotations

from pipecat.frames.frames import Frame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import FunctionCallParams, LLMService
from pipecat.turns.user_start.transcription_user_turn_start_strategy import (
    TranscriptionUserTurnStartStrategy,
)
from pipecat.turns.user_start.vad_user_turn_start_strategy import VADUserTurnStartStrategy
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from ..core.backend import effect
from ..scripted.case import Intent
from ..scripted.playbook import ScriptedModel, tool_succeeded, user_said
from ..scripted.world import UserSpeech
from .harness import Case
from .pipeline import Ends

MENU = (
    "Tonight we have a roasted squash soup to start. Then grilled sea bass with lemon "
    "butter, or a slow braised short rib. For dessert there is a dark chocolate tart."
)  # 29 words, 124 letters -> 7.44 s of audio at 0.06 s per letter


def _no_functions(llm: LLMService) -> LLMContext:
    return LLMContext()


def greeting() -> Case:
    """One user turn, and a reply of two sentences that plays to its end."""
    return Case(
        name="greeting",
        bot=_no_functions,
        speeches=[UserSpeech(start=0.5, end=1.0, transcript="Hello.")],
        policy=ScriptedModel().otherwise(say="Good morning. How can I help you today?"),
    )


RESERVATION_INTENT = "reserve-table-1"


def _takes_reservations(llm: LLMService) -> LLMContext:
    async def create_reservation(params: FunctionCallParams) -> None:
        receipt = await effect(RESERVATION_INTENT, dict(params.arguments))
        await params.result_callback(f"Reservation {receipt} confirmed.")

    llm.register_function("create_reservation", create_reservation)
    return LLMContext()


def reserve_table() -> Case:
    """A function call that makes one backend write, and the reply that confirms it."""
    return Case(
        name="reserve_table",
        bot=_takes_reservations,
        speeches=[UserSpeech(start=0.5, end=2.5, transcript="A table for six tonight, please.")],
        policy=ScriptedModel()
        .when(tool_succeeded(), say="Your table is booked.", claims=[RESERVATION_INTENT])
        .otherwise(say="One moment.", call=("create_reservation", {"party_size": 6})),
        intents=[Intent(RESERVATION_INTENT, tools=("create_reservation",))],
    )


def barge_in() -> Case:
    """A long reply, and a user who speaks over it inside a word."""
    return Case(
        name="barge_in",
        bot=_no_functions,
        speeches=[
            UserSpeech(start=0.5, end=2.5, transcript="Tell me about the menu."),
            UserSpeech(start=5.0, end=6.0, transcript="Stop."),
        ],
        policy=ScriptedModel().when(user_said("stop"), say="Okay, stopping.").otherwise(say=MENU),
        tts_word_timestamps=True,
    )


class _PassesOn(FrameProcessor):
    """A processor of the application between the model and the voice: it passes every frame."""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


def _own_pipeline(ends: Ends) -> list[FrameProcessor]:
    turns = UserTurnStrategies(
        start=[VADUserTurnStartStrategy(), TranscriptionUserTurnStartStrategy()],
        stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.8)],
    )
    params = LLMUserAggregatorParams(user_turn_strategies=turns, empty_user_turn=None)
    pair = LLMContextAggregatorPair(ends.context, user_params=params)
    processors = [ends.input, ends.stt, pair.user(), ends.llm, _PassesOn(), ends.tts]
    return [*processors, ends.output, pair.assistant()]


def own_pipeline() -> Case:
    """A pipeline of the application's own, with a second turn start strategy and a processor
    between the model and the voice: the header names both."""
    return Case(
        name="own_pipeline",
        bot=_no_functions,
        speeches=[UserSpeech(start=0.5, end=1.0, transcript="Hello.")],
        policy=ScriptedModel().otherwise(say="Good morning. How can I help you today?"),
        pipeline=_own_pipeline,
        user_speech_timeout=0.8,
    )


def hangs_up() -> Case:
    """The user hangs up while the booking is confirmed: the hangup cancels the pipeline."""
    return Case(
        name="hangs_up",
        bot=_takes_reservations,
        speeches=[UserSpeech(start=0.5, end=2.5, transcript="A table for six tonight, please.")],
        policy=ScriptedModel()
        .when(tool_succeeded(), say="Your table for six is booked.", claims=[RESERVATION_INTENT])
        .otherwise(say="One moment.", call=("create_reservation", {"party_size": 6})),
        intents=[Intent(RESERVATION_INTENT, tools=("create_reservation",))],
        hangup=5.0,
    )


CASES = {
    "greeting": greeting,
    "reserve_table": reserve_table,
    "barge_in": barge_in,
    "own_pipeline": own_pipeline,
    "hangs_up": hangs_up,
}
