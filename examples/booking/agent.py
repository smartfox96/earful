"""A restaurant booking agent, as an application would write it. Nothing here knows of
Earful: the agent is handed the service it books with."""

from typing import Protocol

import livekit.agents as lk


class Reservations(Protocol):
    """The reservation service, as the agent uses it."""

    async def create(self, party_size: int, *, idempotency_key: str | None = None) -> str:
        """Book a table and return the confirmation number."""
        ...


class BookingAgent(lk.Agent):
    def __init__(self, reservations: Reservations) -> None:
        super().__init__(instructions="You take reservations for a restaurant.")
        self._reservations = reservations

    @lk.function_tool
    async def create_reservation(self, party_size: int) -> str:
        """Book a table for tonight.

        Args:
            party_size: Number of guests
        """
        confirmation = await self._reservations.create(party_size)
        return f"Booked. Confirmation {confirmation}."
