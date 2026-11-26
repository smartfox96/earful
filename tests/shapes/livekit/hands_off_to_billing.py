"""A front desk agent that says a few words and hands the call to a billing agent from a
tool, passing its conversation on. The billing agent greets the caller."""

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
    async def to_billing(self, party_size: int) -> lk.Agent:
        """Hand the call to billing.

        Args:
            party_size: Number of guests
        """
        return Billing(chat_ctx=self.chat_ctx)


def policy(items: list[dict[str, Any]]) -> Reply:
    system = next((i.get("text") or "" for i in items if i.get("role") == "system"), "")
    if "billing" in system:
        return Reply(text="Billing here, how can I help?")
    if any(item["type"] == "function_call_output" for item in items):
        return Reply(text="You are being transferred.")
    return Reply(
        text="Sure thing.", tool_calls=(ToolCall("to_billing", '{"party_size": 6}', "h1"),)
    )


def case() -> Case:
    return Case(
        name="hands_off_to_billing",
        agent=FrontDesk,
        speeches=[
            UserSpeech(start=0.5, end=2.5, transcript="Book a table for six tonight."),
            UserSpeech(start=9.0, end=10.0, transcript="Thanks."),
        ],
        policy=policy,
    )


SHOULD = judged(M1="pass", L1="pass")
DEFECT = judged(M1="unobservable", L1="pass")
