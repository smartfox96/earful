"""A bot whose function speaks its own answer and reports no result, which Pipecat takes
to mean that no further model call is wanted."""

from __future__ import annotations

from pipecat.frames.frames import TTSSpeakFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.llm_service import FunctionCallParams, LLMService

from earful.pipecat import Case, ScriptedModel, UserSpeech
from shapes.corpus import judged


def bot(llm: LLMService) -> LLMContext:
    async def set_volume(params: FunctionCallParams) -> None:
        await params.llm.push_frame(TTSSpeakFrame("The volume is at five."))
        await params.result_callback(None)

    llm.register_function("set_volume", set_volume)
    return LLMContext()


def case() -> Case:
    return Case(
        name="speaks_from_tool",
        bot=bot,
        speeches=[UserSpeech(start=0.5, end=1.5, transcript="Turn it up.")],
        policy=ScriptedModel().otherwise(call=("set_volume", {"level": 5})),
    )


SHOULD = judged(L1="pass")
DEFECT = judged(L1="milestone 2")  # a further model call is owed and never made
