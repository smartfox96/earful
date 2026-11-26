"""The behaviours every adapter must have (tests/conformance.py), on Pipecat."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("pipecat")

import conformance
import pipecat
from conformance import Scenario
from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import EndTaskFrame, Frame, LLMTextFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import FunctionCallParams, LLMService
from pipecat.turns.user_start.vad_user_turn_start_strategy import VADUserTurnStartStrategy
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from shapes.corpus import judged
from shapes.pipecat import booking

from earful.pipecat import (
    Case,
    Delay,
    Ends,
    Intent,
    ScriptedModel,
    UserSpeech,
    effect,
    run_case,
    tool_returned,
    user_said,
)

ADAPTER = "earful.pipecat:run_case"
ASK = UserSpeech(start=0.5, end=2.5, transcript="A table for four tonight, please.")


def _sends_in_background(llm: LLMService) -> LLMContext:
    sending: set[asyncio.Task[str]] = set()

    async def book_table(params: FunctionCallParams) -> None:
        sending.add(asyncio.create_task(effect(booking.BOOKING, {"party_size": 4})))
        await params.result_callback("The booking is on its way.")

    llm.register_function("book_table", book_table)
    return LLMContext()


def write_in_flight() -> Case:
    return Case(
        name="write_in_flight",
        bot=_sends_in_background,
        speeches=[ASK],
        policy=ScriptedModel()
        .when(tool_returned(), say="It is on its way.")
        .otherwise(say="One moment.", call="book_table"),
        intents=[Intent(booking.BOOKING, tools=("book_table",))],
        delays=[Delay("backend.latency", 1, 10.0)],  # past the end of the run
    )


def _no_functions(llm: LLMService) -> LLMContext:
    return LLMContext()


def _turns(ends: Ends) -> LLMContextAggregatorPair:
    strategies = UserTurnStrategies(
        start=[VADUserTurnStartStrategy()],
        stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.6)],
    )
    params = LLMUserAggregatorParams(user_turn_strategies=strategies, empty_user_turn=None)
    return LLMContextAggregatorPair(ends.context, user_params=params)


def _saves_each_turn(ends: Ends) -> list[FrameProcessor]:
    pair = _turns(ends)
    user = pair.user()

    @user.event_handler("on_user_turn_stopped")
    async def save(aggregator: Any, strategy: Any, message: Any) -> None:
        raise RuntimeError(conformance.RAISED)

    return [ends.input, ends.stt, user, ends.llm, ends.tts, ends.output, pair.assistant()]


def application_raises() -> Case:
    return Case(
        name="application_raises",
        bot=_no_functions,
        speeches=[UserSpeech(start=0.5, end=1.5, transcript="Hello.")],
        policy=ScriptedModel().otherwise(say="Hello, how can I help?"),
        pipeline=_saves_each_turn,
    )


def _hangs_up(llm: LLMService) -> LLMContext:
    async def end_call(params: FunctionCallParams) -> None:
        await params.result_callback("Ending the call.")
        await params.llm.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)

    llm.register_function("end_call", end_call)
    return LLMContext()


def closes_early() -> Case:
    return Case(
        name="closes_early",
        bot=_hangs_up,
        speeches=[
            UserSpeech(start=0.5, end=1.5, transcript="Okay, bye."),
            UserSpeech(start=12.0, end=13.0, transcript="Hello? Are you there?"),
        ],
        policy=ScriptedModel()
        .when(user_said("bye"), say="Goodbye then.", call="end_call")
        .otherwise(say="How can I help?"),
    )


class _WaitsForClient(FrameProcessor):
    """Holds the bot's speech until the client says it plays audio, which no run does."""

    def __init__(self) -> None:
        super().__init__()
        self.client_ready = asyncio.Event()  # set from on_client_connected in production

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMTextFrame):
            await self.client_ready.wait()
        await self.push_frame(frame, direction)


def _waits_for_client(ends: Ends) -> list[FrameProcessor]:
    pair = _turns(ends)
    processors = [ends.input, ends.stt, pair.user(), ends.llm, _WaitsForClient(), ends.tts]
    return [*processors, ends.output, pair.assistant()]


def stuck() -> Case:
    return Case(
        name="stuck",
        bot=_no_functions,
        speeches=[UserSpeech(start=0.5, end=1.5, transcript="Hello.")],
        policy=ScriptedModel().otherwise(say="Good morning."),
        pipeline=_waits_for_client,
    )


def _offers_no_tools(llm: LLMService) -> LLMContext:
    async def book_table(params: FunctionCallParams) -> None:
        await params.result_callback(f"Booked, reference {await effect(booking.BOOKING, {})}.")

    llm.register_function("book_table", book_table)
    schema = FunctionSchema(
        name="book_table", description="Book a table.", properties={}, required=[]
    )
    return LLMContext(tools=ToolsSchema(standard_tools=[schema]), tool_choice="none")


def tool_choice_none() -> Case:
    return Case(
        name="tool_choice_none",
        bot=_offers_no_tools,
        speeches=[UserSpeech(start=0.5, end=1.5, transcript="Hello.")],
        policy=ScriptedModel()
        .when(tool_returned(), say="Your table is booked.")
        .otherwise(say="Hello.", call="book_table"),
        intents=[Intent(booking.BOOKING, tools=("book_table",))],
    )


SCENARIOS = {
    conformance.WRITE_SETTLES: Scenario(write_in_flight),
    # Pipecat logs the exception, and nothing reaches the run.
    conformance.RAISE_RECORDED: Scenario(application_raises, defect=judged()),
    conformance.CLOSED_ENDS: Scenario(closes_early),
    conformance.STUCK_CEILING: Scenario(stuck),
    # The scripted model's call is carried out, and the run passes.
    conformance.TOOL_CHOICE: Scenario(tool_choice_none, defect=judged()),
    conformance.ONE_LEDGER: Scenario(booking.case),
}


@pytest.mark.parametrize("behaviour", conformance.params(SCENARIOS, pipecat.__version__))
def test_behaviour(behaviour: conformance.Behaviour) -> None:
    scenario = SCENARIOS[behaviour]
    seen = conformance.observe(behaviour, scenario, ADAPTER, run_case)
    conformance.judge(behaviour, scenario, seen)


def test_every_behaviour_has_a_scenario():
    assert list(SCENARIOS) == list(conformance.BEHAVIOURS)
