"""An agent whose transfer tool asks for a spoken update with generate_reply, then
returns."""

from __future__ import annotations

from typing import Any

import livekit.agents as lk
from invariants import TWO_PARTS

from earful.livekit import Case, Reply, ToolCall, UserSpeech
from shapes.corpus import judged


class Transfers(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You transfer calls.")

    @lk.function_tool
    async def start_transfer(self, context: lk.RunContext) -> str:
        """Start a transfer."""
        context.session.generate_reply(instructions="Tell the user the transfer has started.")
        return "transfer started"


def policy(items: list[dict[str, Any]]) -> Reply:
    if any(
        "transfer has started" in (i.get("text") or "") for i in items if i.get("role") == "system"
    ):
        return Reply(text="I have started your transfer.")
    if any(i["type"] == "function_call_output" for i in items):
        return Reply(text="Anything else?")
    return Reply(text="One moment.", tool_calls=(ToolCall("start_transfer", "{}"),))


def case() -> Case:
    return Case(
        name="transfer_update_from_tool",
        agent=Transfers,
        speeches=[UserSpeech(start=0.5, end=1.5, transcript="Transfer me please.")],
        policy=policy,
    )


SHOULD = judged()
# The update is read as a reply that does not show the tool's result.
DEFECT = judged(M2="rule d")
DEFECT_VARIED = {
    TWO_PARTS: judged(M1="rule 2, rule 5", M2="rule d"),
}
