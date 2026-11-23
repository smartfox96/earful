"""An agent that looks up the account as the user's turn ends and adds what it found to the
turn's context as an assistant message, as the livekit-agents docs show retrieval."""

from __future__ import annotations

import livekit.agents as lk
from invariants import HISTORY
from livekit.agents import llm

from earful.livekit import Case, ScriptedModel, UserSpeech
from shapes.corpus import judged


class AccountAgent(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You answer questions about the account.")

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        found = "Additional information: the balance is twenty dollars."
        turn_ctx.add_message(role="assistant", content=found)


def case() -> Case:
    return Case(
        name="retrieval",
        agent=AccountAgent,
        speeches=[UserSpeech(start=0.5, end=1.5, transcript="What is my balance?")],
        policy=ScriptedModel().otherwise(say="Your balance is twenty dollars."),
    )


SHOULD = judged(M1="pass", M2="pass", L1="pass")
DEFECT = judged(M2="rule c")  # the added message is read as a reply nobody heard
DEFECT_VARIED = {HISTORY: judged(M1="rule 2", M2="rule b, rule c")}
