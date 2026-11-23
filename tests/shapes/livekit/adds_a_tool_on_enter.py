"""An agent that gives itself an opening-hours tool when it enters, with update_tools."""

from __future__ import annotations

import livekit.agents as lk

from earful.livekit import Case, ScriptedModel, UserSpeech, tool_returned
from shapes.corpus import invalid, judged


@lk.function_tool
async def opening_hours(day: str) -> str:
    """Opening hours for a day."""
    return "nine to five"


class Desk(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You are the front desk.")

    async def on_enter(self) -> None:
        await self.update_tools([*self.tools, opening_hours])


def case() -> Case:
    return Case(
        name="adds_a_tool_on_enter",
        agent=Desk,
        speeches=[UserSpeech(start=0.5, end=1.5, transcript="When are you open?")],
        policy=ScriptedModel()
        .when(tool_returned(), say="We open nine to five.")
        .otherwise(say="One moment.", call=("opening_hours", {"day": "monday"})),
    )


SHOULD = judged()
# The added tool is unknown to the witness, and the run is thrown out.
DEFECT = invalid("without the witness")
