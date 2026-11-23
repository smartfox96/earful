"""An agent whose booking tool asks the session's model a side question, with a context of
its own, to label the party as large or small."""

from __future__ import annotations

from typing import Any

import livekit.agents as lk
from invariants import HISTORY
from livekit.agents import llm

from earful.livekit import Case, Claim, Intent, Reply, ToolCall, UserSpeech, effect
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
        side = llm.ChatContext()
        side.add_message(role="system", content="Classify the request as LARGE or SMALL.")
        side.add_message(role="user", content=f"party of {party_size}")
        model = self.session.llm
        assert isinstance(model, llm.LLM)
        async with model.chat(chat_ctx=side) as stream:
            label = "".join([chunk.delta.content or "" async for chunk in stream if chunk.delta])
        receipt = await effect(BOOKING, {"party_size": party_size, "size": label})
        return f"Reservation {receipt} confirmed."


def policy(items: list[dict[str, Any]]) -> Reply:
    if any("Classify" in (item.get("text") or "") for item in items):
        return Reply(text="LARGE")
    if any(item["type"] == "function_call_output" for item in items):
        return Reply(text="Your table is booked.", claims=(Claim(BOOKING),))
    book = ToolCall("create_reservation", '{"party_size": 6}', "c1")
    return Reply(text="One moment please.", tool_calls=(book,))


def case() -> Case:
    return Case(
        name="side_question_to_the_model",
        agent=Host,
        speeches=[UserSpeech(start=0.5, end=2.5, transcript="Book a table for six tonight.")],
        policy=policy,
        intents=[Intent(BOOKING, tools=("create_reservation",))],
    )


SHOULD = judged(M2="pass")
DEFECT = judged(M2="rule a")  # the side question is read as a turn of the conversation
DEFECT_VARIED = {
    HISTORY: judged(M2="rule a, rule b"),
}
