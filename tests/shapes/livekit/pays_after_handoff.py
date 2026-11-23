"""A front desk agent that hands the call to a billing agent, whose payment tool is
declared in the case as a write to the payment service."""

from __future__ import annotations

from typing import Any

import livekit.agents as lk

from earful.livekit import Case, Effect, Reply, ToolCall, UserSpeech
from shapes.corpus import judged, refused


class Billing(lk.Agent):
    def __init__(self, chat_ctx: Any = None) -> None:
        super().__init__(instructions="You are the billing agent.", chat_ctx=chat_ctx)

    async def on_enter(self) -> None:
        self.session.generate_reply()

    @lk.function_tool
    async def pay(self, amount: int) -> str:
        """Pay the bill."""
        return f"paid {amount}"


class FrontDesk(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You are the front desk.")

    @lk.function_tool
    async def to_billing(self) -> lk.Agent:
        """Hand the call to billing."""
        return Billing(chat_ctx=self.chat_ctx)


def policy(items: list[dict[str, Any]]) -> Reply:
    returned = [i.get("name") for i in items if i["type"] == "function_call_output"]
    users = [i.get("text") or "" for i in items if i.get("role") == "user"]
    if "pay" in returned:
        return Reply(text="Payment is done.")
    if any("pay" in u for u in users):
        return Reply(text="Paying now.", tool_calls=(ToolCall("pay", '{"amount": 5}'),))
    if any("billing agent" in (i.get("text") or "") for i in items if i.get("role") == "system"):
        return Reply(text="Billing here.")
    return Reply(text="One moment.", tool_calls=(ToolCall("to_billing", "{}"),))


def case() -> Case:
    return Case(
        name="pays_after_handoff",
        agent=FrontDesk,
        speeches=[
            UserSpeech(start=0.5, end=1.5, transcript="Billing please."),
            UserSpeech(start=8.0, end=9.0, transcript="I want to pay."),
        ],
        policy=policy,
        effects={"pay": Effect("pay-bill")},
    )


SHOULD = judged(A2="pass")
# Only the first agent's tools may be declared.
DEFECT = refused("not function tools of the agent the run starts with")
