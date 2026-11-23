"""An agent whose first reply the user talks over before any audio plays, and whose booking
tool later speaks a fixed filler with session.say. The filler shares a word with the reply
that was never voiced."""

from __future__ import annotations

import livekit.agents as lk
from invariants import TWO_PARTS

from earful.livekit import (
    Case,
    Intent,
    ScriptedModel,
    UserSpeech,
    effect,
    tool_returned,
    user_said,
)
from shapes.corpus import judged

BOOKING = "book-table"


class Host(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You take reservations.")

    @lk.function_tool
    async def create_reservation(self, party_size: int) -> str:
        """Create a reservation.

        Args:
            party_size: Number of guests
        """
        self.session.say("Let me book that for you.")
        receipt = await effect(BOOKING, {"party_size": party_size}, latency=1.0)
        return f"Reservation {receipt} confirmed."


def case() -> Case:
    return Case(
        name="filler_after_discarded_reply",
        agent=Host,
        speeches=[
            UserSpeech(start=0.5, end=2.5, transcript="Tell me about the menu."),
            UserSpeech(start=2.95, end=4.0, transcript="Actually never mind."),
            UserSpeech(start=12.0, end=14.0, transcript="Book a table for six tonight."),
        ],
        policy=ScriptedModel()
        .when(tool_returned(), say="Your table is booked.", claims=[BOOKING])
        .when(user_said("book"), call=("create_reservation", {"party_size": 6}))
        .otherwise(say="Tonight we have soup to start and a tart for dessert."),
        intents=[Intent(BOOKING, tools=("create_reservation",))],
    )


SHOULD = judged(M1="pass", L1="pass")
# The word the filler shares with the reply never voiced leaves the filler unattributed.
DEFECT = judged(M1="unobservable", L1="unobservable")
DEFECT_VARIED = {
    TWO_PARTS: judged(M1="rule 2", L1="unobservable"),
}
