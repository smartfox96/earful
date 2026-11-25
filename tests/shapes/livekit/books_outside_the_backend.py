"""A booking agent whose tool books through a service Earful does not see, so the writes
the booking contracts judge cannot be observed."""

from __future__ import annotations

import livekit.agents as lk
from invariants import TWO_PARTS

from earful.livekit import Case, Intent, ScriptedModel, UserSpeech, tool_succeeded, user_ever_said
from shapes.corpus import judged

BOOKING = "book-table"


class Host(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You take reservations.")

    @lk.function_tool
    async def create_reservation(self, party_size: int) -> str:
        """Book a table.

        Args:
            party_size: Number of guests
        """
        return "Booked. Confirmation R1."  # written where Earful cannot see


def case() -> Case:
    return Case(
        name="books_outside_the_backend",
        agent=Host,
        speeches=[UserSpeech(start=0.5, end=2.5, transcript="A table for four tonight, please.")],
        policy=ScriptedModel()
        .when(tool_succeeded(), say="Your table is booked.", claims=[BOOKING])
        .when(
            user_ever_said("table"),
            say="Let me book that.",
            call=("create_reservation", {"party_size": 4}),
        )
        .otherwise(say="How can I help?"),
        intents=[Intent(BOOKING, tools=("create_reservation",))],
    )


SHOULD = judged(A2="unobservable", A3="unobservable")
DEFECT_VARIED = {
    TWO_PARTS: judged(A2="unobservable", A3="unobservable", M1="rule 2, rule 5"),
}
