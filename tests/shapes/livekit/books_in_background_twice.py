"""An agent whose booking tool hands the booking to a background task, which books once and
again eight seconds later on a retry timer that fires although the first write landed."""

from __future__ import annotations

import asyncio

import livekit.agents as lk
from invariants import HISTORY

from earful.livekit import Case, Intent, ScriptedModel, UserSpeech, effect, tool_returned
from shapes.corpus import judged

BOOKING = "book-table"


class Host(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You take restaurant reservations.")
        self.booking: asyncio.Task[None] | None = None

    @lk.function_tool
    async def book_table(self) -> str:
        """Book a table."""

        async def book_and_retry() -> None:
            await effect(BOOKING, {"party_size": 4})
            await asyncio.sleep(8.0)
            await effect(BOOKING, {"party_size": 4})

        self.booking = asyncio.create_task(book_and_retry())
        return "Booking in progress."


def case() -> Case:
    return Case(
        name="books_in_background_twice",
        agent=Host,
        speeches=[UserSpeech(start=0.5, end=1.5, transcript="A table for four, please.")],
        policy=ScriptedModel()
        .when(tool_returned(), say="I am booking it now.")
        .otherwise(say="One moment.", call="book_table"),
        intents=[Intent(BOOKING, tools=("book_table",))],
    )


# The second write is still to come when the run ends: A2 cannot be passed.
SHOULD = judged(A2="unobservable")
# The run ends between the two writes, and A2 passes.
DEFECT = judged()
DEFECT_VARIED = {
    HISTORY: judged(M2="rule b"),
}
