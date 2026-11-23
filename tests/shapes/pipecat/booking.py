"""A bot that books a table with a function that writes to the reservation service, and
confirms the booking in two sentences once the function has returned."""

from __future__ import annotations

from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.llm_service import FunctionCallParams, LLMService

from earful.pipecat import Case, Intent, ScriptedModel, UserSpeech, effect, tool_succeeded
from shapes.corpus import judged

BOOKING = "book-table"


def bot(llm: LLMService) -> LLMContext:
    async def book_table(params: FunctionCallParams) -> None:
        receipt = await effect(BOOKING, dict(params.arguments))
        await params.result_callback(f"Booked, reference {receipt}.")

    llm.register_function("book_table", book_table)
    return LLMContext()


def case() -> Case:
    return Case(
        name="booking",
        bot=bot,
        speeches=[UserSpeech(start=0.5, end=2.5, transcript="A table for four tonight, please.")],
        policy=ScriptedModel()
        .when(
            tool_succeeded(),
            say="Your table for four is booked. See you tonight.",
            claims=[BOOKING],
        )
        .otherwise(say="One moment while I book that.", call=("book_table", {"party_size": 4})),
        intents=[Intent(BOOKING, tools=("book_table",))],
    )


SHOULD = judged(A2="pass", A3="pass", M1="pass", M2="pass", L1="pass")
