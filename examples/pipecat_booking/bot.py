"""A restaurant booking bot for Pipecat, as an application would write it. Nothing here
knows of Earful: the bot is handed the service it books with."""

from typing import Protocol

from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.llm_service import FunctionCallParams, LLMService


class Reservations(Protocol):
    """The reservation service, as the bot uses it."""

    async def create(self, party_size: int, *, idempotency_key: str | None = None) -> str:
        """Book a table and return the confirmation number."""
        ...


def booking_bot(llm: LLMService, reservations: Reservations) -> LLMContext:
    """Registers the booking function on the model service and returns the bot's context."""

    async def create_reservation(params: FunctionCallParams) -> None:
        party_size = params.arguments["party_size"]
        confirmation = await reservations.create(party_size)
        await params.result_callback(f"Booked. Confirmation {confirmation}.")

    llm.register_function("create_reservation", create_reservation)
    return LLMContext()
