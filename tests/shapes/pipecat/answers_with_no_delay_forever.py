"""A bot whose scripted model answers every call at once with another call of the booking
function, so the conversation never pauses."""

from __future__ import annotations

from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.llm_service import FunctionCallParams, LLMService

from earful.pipecat import (
    Case,
    Intent,
    Reply,
    ScriptedModel,
    ToolCall,
    UserSpeech,
    effect,
    user_ever_said,
)
from shapes.corpus import ends_within, hangs

BOOKING = "book-table"


def bot(llm: LLMService) -> LLMContext:
    async def book(params: FunctionCallParams) -> None:
        await params.result_callback(f"Booked {await effect(BOOKING, {'n': 4})}.")

    llm.register_function("book", book)
    return LLMContext()


def case() -> Case:
    again = Reply(text="", tool_calls=(ToolCall("book", "{}"),), ttft=0, duration=0)
    return Case(
        name="answers_with_no_delay_forever",
        bot=bot,
        speeches=[UserSpeech(start=0.5, end=2.5, transcript="A table for four.")],
        policy=ScriptedModel().when(user_ever_said("table"), again),
        intents=[Intent(BOOKING, authorized=10**9)],
    )


SHOULD = ends_within(20)
# The clock never moves, and the run never ends.
DEFECT = hangs()
