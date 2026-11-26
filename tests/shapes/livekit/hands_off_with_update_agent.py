"""A front desk agent whose tool moves the call to a billing agent with
session.update_agent, passing its conversation on."""

from __future__ import annotations

from typing import Any

import livekit.agents as lk

from earful.livekit import Case, Reply, ToolCall, UserSpeech
from shapes.corpus import judged


class Billing(lk.Agent):
    def __init__(self, chat_ctx: Any = None) -> None:
        super().__init__(instructions="You are the billing agent.", chat_ctx=chat_ctx)

    async def on_enter(self) -> None:
        self.session.generate_reply()


class FrontDesk(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You are the front desk.")

    @lk.function_tool
    async def to_billing(self, context: lk.RunContext) -> str:
        """Move the call to billing."""
        context.session.update_agent(Billing(chat_ctx=self.chat_ctx))
        return "moved"


def policy(items: list[dict[str, Any]]) -> Reply:
    billing = any(
        "billing agent" in (i.get("text") or "") for i in items if i.get("role") == "system"
    )
    if billing or any(i["type"] == "function_call_output" for i in items):
        return Reply(text="Billing here.")
    return Reply(text="One moment.", tool_calls=(ToolCall("to_billing", "{}"),))


def case() -> Case:
    return Case(
        name="hands_off_with_update_agent",
        agent=FrontDesk,
        speeches=[
            UserSpeech(start=0.5, end=1.5, transcript="Billing please."),
            UserSpeech(start=8.0, end=9.0, transcript="Thanks."),
        ],
        policy=policy,
    )


SHOULD = judged()
# The new agent is held to the old one's memory.
DEFECT = judged(M1="rule 2", M2="rule d")
