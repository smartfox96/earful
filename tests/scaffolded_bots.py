"""Pipecat bots for ``earful init`` to write a first test of. Each function fails if run."""

from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.processors.aggregators.llm_context import LLMContext


async def _book(params):
    raise AssertionError("a test reached the reservation service")


def booking_bot(llm):
    """Advertises its function with a schema, and registers its handler."""
    llm.register_function("create_reservation", _book)
    schema = FunctionSchema(
        name="create_reservation",
        description="Book a table.",
        properties={"party_size": {"type": "integer"}, "note": {"type": "string"}},
        required=["party_size"],
    )
    return LLMContext(tools=ToolsSchema(standard_tools=[schema]))


def handed_bot(llm, reservations, *, greeting="Hello"):
    """Registers its function by name alone, and needs the service it books with."""
    llm.register_function("create_reservation", _book)
    return LLMContext()


def greeter(llm):
    return LLMContext()


def needs_a_service(llm, reservations):
    reservations.connect()  # None has no connect: the bot cannot be read
    return LLMContext()


class NotABot:
    pass
