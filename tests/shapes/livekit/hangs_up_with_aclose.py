"""An agent whose hang-up tool awaits session.aclose()."""

from __future__ import annotations

import livekit.agents as lk

from earful.livekit import Case, ScriptedModel, UserSpeech, tool_returned
from shapes.corpus import judged, raises


class HangsUp(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You are a host.")

    @lk.function_tool
    async def end_call(self, context: lk.RunContext) -> str:
        """End the call."""
        await context.session.aclose()
        return "closed"


def case() -> Case:
    return Case(
        name="hangs_up_with_aclose",
        agent=HangsUp,
        speeches=[UserSpeech(start=0.5, end=1.5, transcript="Bye now.")],
        policy=ScriptedModel()
        .when(tool_returned(), say="All done now.")
        .otherwise(say="Goodbye.", call="end_call"),
    )


SHOULD = judged()
CLOSE_RECURSES = not lk.__version__.startswith("1.5.")
if CLOSE_RECURSES:
    # The close from inside the tool recurses through the harness.
    DEFECT = raises("RecursionError")
else:
    # The closed session is held to the reply the tool's result would have had.
    DEFECT = judged(M2="rule d", L1="milestone 2, milestone 3")
