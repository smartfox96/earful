"""A user who speaks up as the bot's reply ends, inside its last forty milliseconds, with a
voice that says when each word starts."""

from __future__ import annotations

from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.llm_service import LLMService

from earful.pipecat import Case, ScriptedModel, UserSpeech, user_said
from shapes.corpus import judged


def bot(llm: LLMService) -> LLMContext:
    return LLMContext()


def case() -> Case:
    return Case(
        name="stops_in_the_last_word",
        bot=bot,
        speeches=[
            UserSpeech(start=0.5, end=1.0, transcript="Hello"),
            UserSpeech(start=3.04, end=3.84, transcript="Wait a second"),  # heard at 3.24 s
        ],
        policy=ScriptedModel()
        .when(user_said("Wait"), say="Sure.")
        .otherwise(say="Good morning to you."),  # plays from 2.3 to 3.26 s
        tts_word_timestamps=True,
    )


SHOULD = judged(M1="pass")
# A clear inside the last chunk marks the fully played reply interrupted.
DEFECT = judged(M1="rule 5")
