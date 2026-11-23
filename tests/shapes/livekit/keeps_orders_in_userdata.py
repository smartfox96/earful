"""A shop agent that keeps the order in the session's userdata, given as an object in the
case."""

from __future__ import annotations

from dataclasses import dataclass, field

import livekit.agents as lk
from invariants import HISTORY

from earful.livekit import Case, ScriptedModel, UserSpeech, tool_returned
from shapes.corpus import judged


@dataclass
class Order:
    items: list[str] = field(default_factory=list)


class Shop(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You take orders.")

    @lk.function_tool
    async def add(self, context: lk.RunContext[Order], item: str) -> str:
        """Add an item to the order."""
        context.userdata.items.append(item)
        return f"{len(context.userdata.items)} item(s) in the order"


def case() -> Case:
    return Case(
        name="keeps_orders_in_userdata",
        agent=Shop,
        speeches=[UserSpeech(start=0.5, end=1.5, transcript="Add a tea.")],
        policy=ScriptedModel()
        .when(tool_returned(), say="Added.")
        .otherwise(say="One moment.", call=("add", {"item": "tea"})),
        session_kwargs={"userdata": Order()},
    )


SHOULD = judged()
DEFECT_VARIED = {
    HISTORY: judged(M2="rule b"),
}
