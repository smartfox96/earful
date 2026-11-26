"""A host that books a table with a tool that writes to the reservation service, and
confirms the booking once the tool has returned."""

from __future__ import annotations

import livekit.agents as lk
from invariants import TWO_PARTS

from earful.livekit import Case, Intent, ScriptedModel, UserSpeech, effect, tool_succeeded
from shapes.corpus import judged

BOOKING = "book-table"


class Host(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You take restaurant reservations.")

    @lk.function_tool
    async def book_table(self, party_size: int) -> str:
        """Book a table.

        Args:
            party_size: Number of guests
        """
        receipt = await effect(BOOKING, {"party_size": party_size})
        return f"Booked, reference {receipt}."


def case() -> Case:
    return Case(
        name="booking",
        agent=Host,
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
DEFECT_VARIED = {
    TWO_PARTS: judged(M1="rule 2, rule 3, rule 5"),  # the second part is read as other speech
}
