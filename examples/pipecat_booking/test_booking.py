"""One conversation with the booking bot, run as written and then searched."""

from bot import booking_bot

from earful import effect
from earful.pipecat import (
    Case,
    Intent,
    ScriptedModel,
    UserSpeech,
    tool_failed,
    tool_succeeded,
    user_ever_said,
)


class FakeReservations:
    """Stands in for the reservation service. A booking is one write to Earful's backend,
    which a search makes slow, failing or lossy."""

    async def create(self, party_size: int, *, idempotency_key: str | None = None) -> str:
        return await effect("book-table", {"party_size": party_size}, key=idempotency_key)


# The scripted model. It books when asked, tries once more if the function fails, and
# tells the user the table is booked only after a call that succeeded.
BOOK = ("create_reservation", {"party_size": 4})
model = (
    ScriptedModel()
    .when(tool_succeeded(), say="Your table for four is booked.", claims=["book-table"])
    .when(
        tool_failed() & (lambda context: len(context.tool_results) >= 2),
        say="Sorry, I could not book that. Please call the restaurant.",
    )
    .when(tool_failed(), say="One moment, let me try that again.", call=BOOK)
    .when(user_ever_said("table"), say="Let me book that for you.", call=BOOK)
    .otherwise(say="How can I help?")
)


def booking() -> Case:
    return Case(
        name="booking",
        bot=lambda llm: booking_bot(llm, FakeReservations()),
        speeches=[UserSpeech(start=0.5, end=2.5, transcript="A table for four tonight, please.")],
        policy=model,
        intents=[Intent("book-table")],  # what the user wants done, once
        backend_mode="keyed",  # the service honours an idempotency key, if it is sent one
    )


def test_the_conversation_as_written(earful):
    earful.run(booking).assert_contracts()


def test_every_timing_and_fault_near_it(earful):
    earful.explore(booking).assert_contracts()
