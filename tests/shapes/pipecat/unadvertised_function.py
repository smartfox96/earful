"""A bot that registers a booking function but offers its model only a weather function, so
no real model could book."""

from __future__ import annotations

from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.llm_service import FunctionCallParams, LLMService

from earful.pipecat import Case, Intent, ScriptedModel, UserSpeech, effect, tool_succeeded
from shapes.corpus import invalid, judged

BOOKING = "book-table"


def bot(llm: LLMService) -> LLMContext:
    async def book_table(params: FunctionCallParams) -> None:
        receipt = await effect(BOOKING, {"party_size": params.arguments["party_size"]})
        await params.result_callback({"confirmation": receipt})

    async def weather(params: FunctionCallParams) -> None:
        await params.result_callback({"sky": "clear"})

    llm.register_function("book_table", book_table)
    llm.register_function("weather", weather)
    offered = FunctionSchema(name="weather", description="The weather.", properties={}, required=[])
    return LLMContext(tools=ToolsSchema(standard_tools=[offered]))


def case() -> Case:
    return Case(
        name="unadvertised_function",
        bot=bot,
        speeches=[UserSpeech(start=0.5, end=2.0, transcript="A table for two, please.")],
        policy=ScriptedModel()
        .when(tool_succeeded("book_table"), say="Your table is booked.", claims=[BOOKING])
        .otherwise(say="One moment.", call=("book_table", {"party_size": 2})),
        intents=[Intent(BOOKING, tools=("book_table",))],
    )


# The scripted model did what no real model could: the run is no evidence.
SHOULD = invalid("book_table")
# The call is carried out, and the run passes.
DEFECT = judged()
