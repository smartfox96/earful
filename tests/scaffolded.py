"""Agents as applications write them, for ``earful init`` to write a first test of. No
tool here may run in a test: each body fails if it does."""

from typing import Literal

import livekit.agents as lk


class SupportAgent(lk.Agent):
    """Takes no arguments, and has a tool that writes and a tool that reads."""

    def __init__(self) -> None:
        super().__init__(instructions="You handle refunds.")

    @lk.function_tool
    async def issue_refund(
        self, ctx: lk.RunContext, order: str, amount: float, urgent: bool = False
    ) -> str:
        """Refund an order.

        Args:
            order: The order number
            amount: How much to refund
            urgent: Whether to skip the queue
        """
        raise AssertionError("a test reached the payment service")

    @lk.function_tool
    async def look_up(
        self, order_id: int, how: Literal["card", "cash"], tags: list[str], note: str | None
    ) -> str:
        """Look up an order.

        Args:
            order_id: The order's number
            how: How it was paid
            tags: Labels to filter by
            note: A note to attach
        """
        raise AssertionError("a test reached the order service")


class BookingAgent(lk.Agent):
    """Is handed the service it books with, and cannot be built without it."""

    def __init__(self, reservations: object, /, *, greeting: str, tone: str = "warm") -> None:
        super().__init__(instructions=f"You take reservations. {greeting}")
        self._reservations = reservations

    @lk.function_tool
    async def create_reservation(self, party_size: int) -> str:
        """Book a table.

        Args:
            party_size: Number of guests
        """
        raise AssertionError("a test reached the reservation service")


class Greeter(lk.Agent):
    """Has no tools."""

    def __init__(self) -> None:
        super().__init__(instructions="You greet people.")


def not_an_agent() -> None:
    """Something a reference can name that ``earful init`` has to refuse."""
