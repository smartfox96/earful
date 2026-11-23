"""An agent that shows its model only the last two items of the conversation, to keep the
context window small, and answers every question on time."""

from __future__ import annotations

from collections.abc import AsyncIterable
from typing import Any

import livekit.agents as lk
from invariants import TWO_PARTS

from earful.livekit import Case, ScriptedModel, UserSpeech, user_said
from shapes.corpus import judged


class Windowed(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You are a host.")

    async def llm_node(self, chat_ctx: Any, tools: Any, model_settings: Any) -> AsyncIterable[Any]:
        recent = chat_ctx.copy()
        recent.truncate(max_items=2)
        async for chunk in lk.Agent.default.llm_node(self, recent, tools, model_settings):
            yield chunk


def case() -> Case:
    return Case(
        name="windowed_context",
        agent=Windowed,
        speeches=[
            UserSpeech(start=0.5, end=1.5, transcript="One."),
            UserSpeech(start=5.0, end=6.0, transcript="Two."),
            UserSpeech(start=9.5, end=10.5, transcript="Three."),
        ],
        policy=ScriptedModel()
        .when(user_said("Three"), say="Third answer.")
        .when(user_said("Two"), say="Second answer.")
        .otherwise(say="First answer."),
    )


# The window is the application's choice: it is no memory the framework lost.
SHOULD = judged(M1="pass", M2="pass", L1="pass")
DEFECT = judged(M1="rule 2", M2="rule a, rule b", L1="milestone 1")
DEFECT_VARIED = {
    TWO_PARTS: judged(M1="rule 2, rule 5", M2="rule a, rule b", L1="milestone 1"),
}
