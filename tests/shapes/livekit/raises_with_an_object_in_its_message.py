"""An agent whose lookup tool raises an error whose message holds an object's default
representation, with its memory address."""

from __future__ import annotations

import livekit.agents as lk
from invariants import AGAIN, TWO_PARTS

from earful.livekit import Case, ScriptedModel, UserSpeech, tool_returned, user_ever_said
from shapes.corpus import judged


class Guest:
    pass


class Host(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You seat guests.")

    @lk.function_tool
    async def find_table(self) -> str:
        """Find a table."""
        raise LookupError(f"no table for {Guest()}")


def case() -> Case:
    return Case(
        name="raises_with_an_object_in_its_message",
        agent=Host,
        speeches=[UserSpeech(start=0.5, end=1.5, transcript="Please find a table.")],
        policy=ScriptedModel()
        .when(tool_returned(), say="Sorry, there is none.")
        .when(user_ever_said("table"), say="One moment.", call="find_table")
        .otherwise(say="Hello there."),
    )


SHOULD = judged()
# The message is recorded as written, memory address and all, so no two runs agree.
DEFECT_VARIED = {
    AGAIN: judged(),
    TWO_PARTS: judged(M1="rule 2, rule 5"),
}
