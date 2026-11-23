"""An agent whose order lookup starts a filler line with session.say and returns without
waiting for it."""

from __future__ import annotations

import livekit.agents as lk
from invariants import HISTORY

from earful.livekit import Case, ScriptedModel, UserSpeech, tool_returned, user_ever_said
from shapes.corpus import judged


class Orders(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You track orders.")

    @lk.function_tool
    async def look_up(self, context: lk.RunContext, order: int) -> str:
        """Look up an order."""
        context.session.say("Hold on a second.")
        return "shipped"


def case() -> Case:
    return Case(
        name="filler_from_tool",
        agent=Orders,
        speeches=[UserSpeech(start=0.5, end=1.5, transcript="Where is my order?")],
        policy=ScriptedModel()
        .when(tool_returned(), say="Your order has shipped.")
        .when(user_ever_said("order"), say="One moment.", call=("look_up", {"order": 7}))
        .otherwise(say="Welcome."),
    )


SHOULD = judged()
# The filler is read as an unscripted reply.
DEFECT = judged(M1="unobservable", M2="rule c")
DEFECT_VARIED = {
    HISTORY: judged(M1="unobservable", M2="rule b, rule c"),
}
