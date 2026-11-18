"""Reference cases: one per behaviour the recorded ledgers and historical checks rest on."""

from __future__ import annotations

from typing import Any

import livekit.agents as lk

from ..core.backend import effect
from .harness import Case, Intent
from .providers import Claim, Reply, ToolCall, UserSpeech

LONG_REPLY = (
    "Tonight we have a roasted squash soup to start then grilled sea bass with lemon "
    "butter or a slow braised short rib and for dessert a dark chocolate tart"
)  # 29 words, 124 letters -> 7.44 s of audio at 0.06 s per letter


def _last_user_text(items: list[dict[str, Any]]) -> str:
    for item in reversed(items):
        if item["type"] == "message" and item.get("role") == "user":
            return item.get("text") or ""
    return ""


def _barge_in_policy(items: list[dict[str, Any]]) -> Reply:
    text = _last_user_text(items).lower()
    if "stop" in text:
        return Reply(text="Okay, stopping.")
    if "menu" in text:
        return Reply(text=LONG_REPLY, ttft=0.1, duration=0.4)
    return Reply(text="Sorry, could you repeat that?")


def barge_in_mid_word() -> Case:
    """One user turn, a long reply, and an interruption that lands inside a word."""
    return Case(
        name="barge_in_mid_word",
        agent=lambda: lk.Agent(instructions="You are a restaurant host."),
        speeches=[
            UserSpeech(start=0.5, end=2.5, transcript="Tell me about the menu."),
            UserSpeech(start=5.0, end=6.0, transcript="Stop."),
        ],
        policy=_barge_in_policy,
    )


BOOKING_INTENT = "book-table-1"
BOOKING_ACK = (
    "One moment while I book that table for you and get everything confirmed with the "
    "restaurant for tonight"
)  # 18 words, 86 letters -> 5.16 s of audio


class Reservations:
    """Stands in for the reservation service: a booking is one backend write."""

    async def create(self, party_size: int) -> str:
        return await effect(BOOKING_INTENT, {"party_size": party_size})


class BookingAgent(lk.Agent):
    """An agent as an application writes one: it knows nothing of Earful."""

    def __init__(self, reservations: Reservations) -> None:
        super().__init__(instructions="You take restaurant reservations.")
        self._reservations = reservations

    @lk.function_tool
    async def create_reservation(self, party_size: int) -> str:
        """Create a reservation.

        Args:
            party_size: Number of guests
        """
        receipt = await self._reservations.create(party_size)
        return f"Reservation {receipt} confirmed for {party_size} guests."


# Books again when it sees no tool result: the model behaviour in livekit/agents#3702.
def _booking_policy(items: list[dict[str, Any]]) -> Reply:
    booked = any(i["type"] == "function_call_output" for i in items)
    if booked:
        return Reply(text="Your table is booked.", claims=(Claim(BOOKING_INTENT),))
    wants_booking = any(
        i["type"] == "message"
        and i.get("role") == "user"
        and "table" in (i.get("text") or "").lower()
        for i in items
    )
    if wants_booking:
        return Reply(
            text=BOOKING_ACK,
            tool_calls=(ToolCall("create_reservation", '{"party_size": 6}'),),
        )
    return Reply(text="How can I help?")


def book_then_interrupt() -> Case:
    """livekit/agents#3702: the user interrupts while the agent acknowledges a booking."""
    return Case(
        name="book_then_interrupt",
        agent=lambda: BookingAgent(Reservations()),
        speeches=[
            UserSpeech(start=0.5, end=2.5, transcript="Book a table for six tonight."),
            UserSpeech(start=5.0, end=6.0, transcript="Is the table by the window?"),
        ],
        policy=_booking_policy,
        intents=[Intent(BOOKING_INTENT, tools=("create_reservation",))],
        drain_delay=12.0,
    )


def _slow_menu_policy(items: list[dict[str, Any]]) -> Reply:
    text = _last_user_text(items).lower()
    if "dessert" in text:
        return Reply(text="For dessert we have a dark chocolate tart.")
    if "menu" in text:
        return Reply(text=LONG_REPLY, ttft=0.3, duration=0.6)
    return Reply(text="Sorry, could you repeat that?")


def speak_over_new_turn() -> Case:
    """livekit/agents#5509: a reply still being generated starts playing over a new user turn."""
    return Case(
        name="speak_over_new_turn",
        agent=lambda: lk.Agent(instructions="You are a restaurant host."),
        speeches=[
            UserSpeech(start=0.5, end=2.5, transcript="Tell me about the menu."),
            UserSpeech(start=3.2, end=4.6, transcript="Actually what about dessert?"),
        ],
        policy=_slow_menu_policy,
        # Puts the first audio after the new turn's VAD start, which is the upstream race.
        tts_ttfb=0.5,
    )


CASES = {
    "barge_in_mid_word": barge_in_mid_word,
    "book_then_interrupt": book_then_interrupt,
    "speak_over_new_turn": speak_over_new_turn,
}
