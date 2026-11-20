"""Agents for ``earful init`` to write a first test of. Each tool fails if a test runs it."""

import datetime
import uuid
from typing import Literal

import livekit.agents as lk
from pydantic import BaseModel, Field


class SupportAgent(lk.Agent):
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


class Stop(BaseModel):
    city: str
    nights: int = 1


class Trip(BaseModel):
    traveller: str
    stops: list[Stop] = Field(min_length=1)
    leaves: datetime.datetime


class TravelAgent(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You book trips.")

    @lk.function_tool
    async def book_trip(self, trip: Trip, on: datetime.date, reference: uuid.UUID) -> str:
        """Book a trip.

        Args:
            trip: The trip
            on: The day it is booked for
            reference: The customer's reference
        """
        raise AssertionError("a test reached the travel service")


@lk.function_tool
async def send_invoice(order: str) -> str:
    """Send an invoice.

    Args:
        order: The order number
    """
    raise AssertionError("a test reached the billing service")


class HandedItsTools(lk.Agent):
    def __init__(self, billing: object) -> None:
        super().__init__(instructions="You send invoices.", tools=[send_invoice])
        self._billing = billing


class NeedsItsService(lk.Agent):
    def __init__(self, service: object) -> None:
        if service is None:
            raise ValueError("a service is needed")
        super().__init__(instructions="You cancel orders.")

    @lk.function_tool
    async def cancel_order(self, order: str) -> str:
        """Cancel an order.

        Args:
            order: The order number
        """
        raise AssertionError("a test reached the order service")


class CannotBeBuilt(lk.Agent):
    def __init__(self) -> None:
        raise RuntimeError("no configuration")


class Voucher(BaseModel):
    code: str = Field(pattern=r"^[A-Z]{3}-[0-9]{4}$")


class TakesACode(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You redeem vouchers.")

    @lk.function_tool
    async def redeem(self, voucher: Voucher) -> str:
        """Redeem a voucher.

        Args:
            voucher: The voucher
        """
        raise AssertionError("a test reached the voucher service")


def _raw(name: str, code: dict[str, str]) -> dict[str, object]:
    parameters = {"type": "object", "properties": {"code": code}, "required": ["code"]}
    return {"name": name, "description": "Redeem a voucher.", "parameters": parameters}


@lk.function_tool(raw_schema=_raw("redeem_plain", {"type": "string"}))
async def redeem_plain(raw_arguments: dict[str, object]) -> str:
    raise AssertionError("a test reached the voucher service")


@lk.function_tool(raw_schema=_raw("redeem_coded", {"type": "string", "pattern": "^[A-Z]{3}$"}))
async def redeem_coded(raw_arguments: dict[str, object]) -> str:
    raise AssertionError("a test reached the voucher service")


class DeclaresASchema(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You redeem vouchers.", tools=[redeem_plain])


class DeclaresAPattern(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You redeem vouchers.", tools=[redeem_coded])


class DeclaresBoth(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You redeem vouchers.", tools=[redeem_coded, redeem_plain])


class Greeter(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You greet people.")


def not_an_agent() -> None:
    """Something a reference can name that ``earful init`` has to refuse."""
