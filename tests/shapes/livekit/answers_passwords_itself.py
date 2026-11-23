"""An agent whose llm_node answers questions about passwords itself, without calling the
model."""

from __future__ import annotations

from collections.abc import AsyncIterable
from typing import Any

import livekit.agents as lk
from invariants import HISTORY

from earful.livekit import Case, ScriptedModel, UserSpeech
from shapes.corpus import judged


class Guarded(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You are a help desk.")

    async def llm_node(self, chat_ctx: Any, tools: Any, model_settings: Any) -> AsyncIterable[Any]:
        users = [i for i in chat_ctx.items if i.type == "message" and i.role == "user"]
        if "password" in (users[-1].text_content or "").lower():
            yield "I cannot help with passwords."
            return
        async for chunk in lk.Agent.default.llm_node(self, chat_ctx, tools, model_settings):
            yield chunk


def case() -> Case:
    return Case(
        name="answers_passwords_itself",
        agent=Guarded,
        speeches=[UserSpeech(start=0.5, end=1.5, transcript="What is my password?")],
        policy=ScriptedModel().otherwise(say="Sure."),
    )


SHOULD = judged()
# A turn answered without the model is held to a model call.
DEFECT = judged(L1="milestone 1")
DEFECT_VARIED = {
    HISTORY: judged(M2="rule b", L1="milestone 1"),
}
