"""An agent whose sign-up tool awaits an AgentTask that collects the caller's email, and
returns once the task completes."""

from __future__ import annotations

from typing import Any

import livekit.agents as lk

from earful.livekit import Case, Reply, ToolCall, UserSpeech
from shapes.corpus import invalid, judged


class GetEmail(lk.AgentTask[str]):
    def __init__(self, chat_ctx: Any = None) -> None:
        super().__init__(instructions="Collect the email.", chat_ctx=chat_ctx)

    async def on_enter(self) -> None:
        self.session.generate_reply(instructions="Ask for the email")

    @lk.function_tool
    async def record_email(self, email: str) -> None:
        """Record the email."""
        self.complete(email)


class FrontDesk(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You are the front desk.")

    @lk.function_tool
    async def sign_up(self) -> str:
        """Sign the user up."""
        email = await GetEmail(chat_ctx=self.chat_ctx)
        return f"signed up {email}"


def policy(items: list[dict[str, Any]]) -> Reply:
    users = [i.get("text") or "" for i in items if i.get("role") == "user"]
    returned = [i.get("name") for i in items if i["type"] == "function_call_output"]
    if any(
        "Collect the email." in (i.get("text") or "") for i in items if i.get("role") == "system"
    ):
        if any("example" in u for u in users) and "record_email" not in returned:
            email = ToolCall("record_email", '{"email": "a@example.com"}')
            return Reply(text="Got it.", tool_calls=(email,))
        return Reply(text="What is your email?")
    if "sign_up" in returned:
        return Reply(text="You are signed up.")
    return Reply(text="Sure.", tool_calls=(ToolCall("sign_up", "{}"),))


def case() -> Case:
    return Case(
        name="sign_up_task_in_tool",
        agent=FrontDesk,
        speeches=[
            UserSpeech(start=0.5, end=1.5, transcript="Sign me up."),
            UserSpeech(start=6.0, end=8.0, transcript="It is a at example dot com."),
        ],
        policy=policy,
    )


SHOULD = judged()
# The task's own tool is unknown to the witness, and the run is thrown out.
DEFECT = invalid("without the witness")
