"""An agent that asks for consent to record before anything else, with an AgentTask awaited
in on_enter as the livekit-agents documentation shows."""

from __future__ import annotations

from typing import Any

import livekit.agents as lk

from earful.livekit import Case, Reply, ToolCall, UserSpeech
from shapes.corpus import ends_within, invalid, judged


class CollectConsent(lk.AgentTask[bool]):
    def __init__(self, chat_ctx: Any = None) -> None:
        super().__init__(instructions="Ask for recording consent.", chat_ctx=chat_ctx)

    async def on_enter(self) -> None:
        self.session.generate_reply(instructions="Ask for consent")

    @lk.function_tool
    async def consent_given(self) -> None:
        """The user consents."""
        self.complete(True)


class FrontDesk(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You are the front desk.")

    async def on_enter(self) -> None:
        if await CollectConsent(chat_ctx=self.chat_ctx):
            await self.session.generate_reply(instructions="Offer help")


def policy(items: list[dict[str, Any]]) -> Reply:
    users = [i for i in items if i["type"] == "message" and i.get("role") == "user"]
    if any(i.get("role") == "system" and "front desk" in (i.get("text") or "") for i in items):
        return Reply(text="How can I help you today?")
    if users and not any(i["type"] == "function_call_output" for i in items):
        return Reply(text="Thank you.", tool_calls=(ToolCall("consent_given", "{}"),))
    return Reply(text="May I record this call?")


def case() -> Case:
    return Case(
        name="consent_task_on_enter",
        agent=FrontDesk,
        speeches=[UserSpeech(start=3.0, end=4.0, transcript="Yes you may.")],
        policy=policy,
    )


SHOULD = ends_within(20, judged())
# The task's tool runs without the witness.
DEFECT = invalid("without the witness")
