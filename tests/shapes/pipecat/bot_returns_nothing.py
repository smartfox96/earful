"""A bot function that registers its functions and forgets to return its context."""

from __future__ import annotations

from typing import Any

from invariants import HISTORY
from pipecat.services.llm_service import LLMService

from earful.pipecat import Case, ScriptedModel, UserSpeech
from shapes.corpus import refused


def bot(llm: LLMService) -> Any:
    llm.register_function("weather", lambda params: None)


def case() -> Case:
    return Case(
        name="bot_returns_nothing",
        bot=bot,
        speeches=[UserSpeech(start=0.5, end=1.0, transcript="Hello")],
        policy=ScriptedModel().otherwise(say="Good morning."),
    )


SHOULD = refused("LLMContext")
NOT_VARIED = {HISTORY: "the bot provides no context to put a starting history in"}
