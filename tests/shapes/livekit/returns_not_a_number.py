"""An agent whose quote tool writes the quote and returns its price, which comes out as not
a number."""

from __future__ import annotations

import livekit.agents as lk
from invariants import HISTORY

from earful.livekit import (
    Case,
    Intent,
    ScriptedModel,
    UserSpeech,
    effect,
    tool_returned,
    user_ever_said,
)
from shapes.corpus import judged


class Quotes(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You quote prices.")

    @lk.function_tool
    async def quote(self) -> float:
        """Quote a price."""
        await effect("quote", {"item": "tea"})
        return float("nan")


def case() -> Case:
    return Case(
        name="returns_not_a_number",
        agent=Quotes,
        speeches=[UserSpeech(start=0.5, end=1.5, transcript="Please quote me.")],
        policy=ScriptedModel()
        .when(tool_returned(), say="Here is your quote.")
        .when(user_ever_said("quote"), say="One moment.", call="quote")
        .otherwise(say="Hello there."),
        intents=[Intent("quote", tools=("quote",))],
    )


SHOULD = judged(A2="pass")
# Recording the value raises inside the tool call: the model is told it failed.
DEFECT = judged(L1="milestone 2")
DEFECT_VARIED = {
    HISTORY: judged(M2="rule b", L1="milestone 2"),
}
