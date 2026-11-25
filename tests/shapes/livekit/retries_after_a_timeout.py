"""An agent whose booking times out for it after one second while the service commits it
eight seconds later, and which tries again with no idempotency key."""

from __future__ import annotations

import livekit.agents as lk
from invariants import TWO_PARTS

from earful.livekit import (
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


class Host(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You take reservations.")

    @lk.function_tool
    async def book(self) -> str:
        """Book a table."""
        return "booked " + await effect(BOOKING, {"party_size": 4})


def case() -> Case:
    return Case(
        name="retries_after_a_timeout",
        agent=Host,
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
# The first write is cancelled at the end of the run, and one booking is seen.
DEFECT = judged()
DEFECT_VARIED = {
    TWO_PARTS: judged(M1="rule 2, rule 5"),
}
