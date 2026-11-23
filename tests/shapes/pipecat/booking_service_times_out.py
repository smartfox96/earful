"""A bot whose booking service hangs, so Pipecat cancels the function at its five-second
timeout. The model follows the rules of the Pipecat page: retry once, then apologise."""

from __future__ import annotations

import asyncio

from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.llm_service import FunctionCallParams, LLMService

from earful.pipecat import (
    Case,
    Intent,
    ScriptedModel,
    UserSpeech,
    effect,
    tool_failed,
    tool_succeeded,
    user_ever_said,
)
from shapes.corpus import judged

BOOKING = "book-table"
BOOK = ("create_reservation", {"party_size": 4})


def bot(llm: LLMService) -> LLMContext:
    async def create_reservation(params: FunctionCallParams) -> None:
        await asyncio.sleep(20)  # the service never answers in time
        await params.result_callback({"ok": await effect(BOOKING, {})})

    llm.register_function("create_reservation", create_reservation, timeout_secs=5)
    return LLMContext()


def case() -> Case:
    return Case(
        name="booking_service_times_out",
        bot=bot,
        speeches=[UserSpeech(start=0.5, end=2.5, transcript="A table for four tonight, please.")],
        policy=ScriptedModel()
        .when(tool_succeeded(), say="Your table for four is booked.", claims=[BOOKING])
        .when(
            tool_failed() & (lambda context: len(context.tool_results) >= 2),
            say="Sorry, I could not book that.",
        )
        .when(tool_failed(), say="One moment, let me try that again.", call=BOOK)
        .when(user_ever_said("table"), say="Let me book that for you.", call=BOOK)
        .otherwise(say="How can I help?"),
        intents=[Intent(BOOKING)],
    )


SHOULD = judged()
# The cancelled call is shown to the model as a success, and it announces the booking.
DEFECT = judged(A3="rule", L1="milestone 2")
