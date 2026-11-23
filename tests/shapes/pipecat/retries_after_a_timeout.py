"""A bot whose booking times out for it after one second while the service commits it eight
seconds later, and which tries again with no idempotency key."""

from __future__ import annotations

from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.llm_service import FunctionCallParams, LLMService

from earful.pipecat import (
    Case,
    Delay,
    Fault,
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


def bot(llm: LLMService) -> LLMContext:
    async def book(params: FunctionCallParams) -> None:
        receipt = await effect(BOOKING, {"party_size": 4})
        await params.result_callback(f"Booked {receipt}.")

    llm.register_function("book", book)
    return LLMContext()


def case() -> Case:
    return Case(
        name="retries_after_a_timeout",
        bot=bot,
        speeches=[UserSpeech(start=0.5, end=2.5, transcript="A table for four.")],
        policy=ScriptedModel()
        .when(tool_succeeded(), say="Your table is booked.", claims=[BOOKING])
        .when(tool_failed(), say="One moment, trying again.", call="book")
        .when(user_ever_said("table"), say="Let me book that.", call="book")
        .otherwise(say="How can I help?"),
        intents=[Intent(BOOKING)],
        faults=[Fault("backend.timeout", 1, 1.0)],
        delays=[Delay("backend.latency", 1, 8.0)],
    )


# Both writes commit: the service holds two bookings.
SHOULD = judged(A2="rule")
