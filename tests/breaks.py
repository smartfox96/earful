"""Scenarios with a deliberate break, and their controls, for the search tests.

Each is a no-argument function returning a ``Case``, so a case file can name it
(``breaks:slow_to_stop``) and a worker process can build it.

The timing breaks are an output that keeps playing for 400 ms after it is told to stop;
they differ in when. The fault breaks are booking models that handle a failed or
timed-out write badly; each passes as written, where every write commits. Two of them
run an agent with nothing of Earful in it, whose booking the scenario declares. The hangup
break is an agent that books again when the session ends unless it had told the user the
table is booked; it passes as written, where the user stays to hear it. The break of a
write in flight is an agent that books again if its reply was cut short while the write
was under way; it passes as written, where the write commits at once.
"""

import asyncio

import livekit.agents as lk

from earful.core.backend import Effect, effect
from earful.livekit.cases import BOOKING_ACK, BOOKING_INTENT, CASES, LONG_REPLY
from earful.livekit.harness import Case, Intent
from earful.livekit.playbook import ScriptedModel, user_said
from earful.livekit.providers import Claim, Reply, ToolCall, UserSpeech, letters
from earful.livekit.sink import ClockedSink

# How much of the long reply has played when its seventh word ends: a boundary well
# inside the reply, with nothing else happening near it.
SEVENTH_WORD_ENDS_US = sum(letters(word) for word in LONG_REPLY.split()[:7]) * 60_000


def _slow_between(after_us: int, before_us: int) -> type[ClockedSink]:
    """An output that is slow to stop only if told to while the audio it has played is at
    least ``after_us`` and less than ``before_us``."""

    class Slow(ClockedSink):
        def _obey(self, action):
            segment = self._segment
            started = segment is not None and segment.started_us is not None
            played = segment.played_us(self._ledger.now_us()) if started else None
            if played is not None and after_us <= played < before_us:
                asyncio.get_running_loop().call_later(0.4, action)
            else:
                action()

    return Slow


def host() -> Case:
    """The control: a long reply, and a user who says "Stop." once it is over. As written
    nothing is interrupted, so every scenario built on it passes until a search moves
    "Stop." into the reply."""
    case = CASES["barge_in_mid_word"]()
    case.speeches = [case.speeches[0], UserSpeech(start=14.0, end=15.0, transcript="Stop.")]
    case.drain_delay = 4.0
    return case


def _with(sink: type[ClockedSink]) -> Case:
    case = host()
    case.sink = sink
    return case


def slow_to_stop() -> Case:
    """Slow to stop whenever it is told to."""
    return _with(_slow_between(0, 10**12))


def slow_to_stop_for_30ms() -> Case:
    """Slow to stop only in the 30 ms after the seventh word ends."""
    return _with(_slow_between(SEVENTH_WORD_ENDS_US, SEVENTH_WORD_ENDS_US + 30_000))


def slow_to_stop_on_an_instant() -> Case:
    """Slow to stop only if told to at the very microsecond the seventh word ends."""
    return _with(_slow_between(SEVENTH_WORD_ENDS_US, SEVENTH_WORD_ENDS_US + 1))


def interrupted_and_slow_to_stop() -> Case:
    """The reference barge-in, which interrupts the reply as written, with an output that
    is slow to stop: a scenario that fails before anything is moved."""
    case = CASES["barge_in_mid_word"]()
    case.sink = _slow_between(0, 10**12)
    return case


# ----------------------------------------------------------------------------- faults


def _asked_to_book(items) -> bool:
    return any(
        i["type"] == "message"
        and i.get("role") == "user"
        and "table" in (i.get("text") or "").lower()
        for i in items
    )


def _book() -> Reply:
    return Reply(
        text=BOOKING_ACK, tool_calls=(ToolCall("create_reservation", '{"party_size": 6}'),)
    )


def _confirms_whatever_came_back(items) -> Reply:
    """Says the table is booked once the tool has answered, error or not."""
    if any(i["type"] == "function_call_output" for i in items):
        return Reply(text="Your table is booked.", claims=(Claim(BOOKING_INTENT),))
    return _book() if _asked_to_book(items) else Reply(text="How can I help?")


def _reads_the_error(items) -> Reply:
    """Confirms a booking only on a result that is not an error."""
    results = [i for i in items if i["type"] == "function_call_output"]
    if any(not r["is_error"] for r in results):
        return Reply(text="Your table is booked.", claims=(Claim(BOOKING_INTENT),))
    if results:
        return Reply(text="Sorry, I could not make that booking.")
    return _book() if _asked_to_book(items) else Reply(text="How can I help?")


def _tries_again(items) -> Reply:
    """After one error it calls the tool a second time; after two it gives up."""
    results = [i for i in items if i["type"] == "function_call_output"]
    if any(not r["is_error"] for r in results):
        return Reply(text="Your table is booked.", claims=(Claim(BOOKING_INTENT),))
    if len(results) == 1:
        return _book()
    if results:
        return Reply(text="Sorry, I could not make that booking.")
    return _book() if _asked_to_book(items) else Reply(text="How can I help?")


class _KeyedBookingAgent(lk.Agent):
    """Books with an idempotency key: the same booking asked for twice is one write."""

    def __init__(self) -> None:
        super().__init__(instructions="You take restaurant reservations.")

    @lk.function_tool
    async def create_reservation(self, party_size: int) -> str:
        """Create a reservation.

        Args:
            party_size: Number of guests
        """
        receipt = await effect(
            BOOKING_INTENT, {"party_size": party_size}, key=f"table-for-{party_size}"
        )
        return f"Reservation {receipt} confirmed for {party_size} guests."


def _booking(policy, **changes) -> Case:
    case = CASES["book_then_interrupt"]()
    case.speeches = case.speeches[:1]
    case.policy = policy
    case.intents = [Intent(BOOKING_INTENT, tools=("create_reservation",))]
    for name, to in changes.items():
        setattr(case, name, to)
    return case


def confirms_a_failed_booking() -> Case:
    """One request to book. The model confirms whatever the tool returned."""
    return _booking(_confirms_whatever_came_back)


def reads_the_error() -> Case:
    """The control: the model confirms only a write that succeeded."""
    return _booking(_reads_the_error)


def retries_without_a_key() -> Case:
    """The model calls the tool again after an error, and the tool sends no key."""
    return _booking(_tries_again)


def retries_with_a_key() -> Case:
    """The control: the same model, a tool that sends a key and a backend that honours it."""
    return _booking(_tries_again, agent=_KeyedBookingAgent, backend_mode="keyed")


# ----------------------------------------------------------------------------- an agent as written


class TalksToItsServiceItself(lk.Agent):
    """An agent with nothing of Earful in it. Its tool reaches the reservation service
    directly, which no test may do: only a declared effect makes it runnable."""

    def __init__(self) -> None:
        super().__init__(instructions="You take restaurant reservations.")

    @lk.function_tool
    async def create_reservation(self, party_size: int) -> str:
        """Create a reservation.

        Args:
            party_size: Number of guests
        """
        raise AssertionError("a test reached the reservation service")


def _confirmation(receipt: str, arguments: dict) -> str:
    return f"Reservation {receipt} confirmed for {arguments['party_size']} guests."


BOOKING = Effect(BOOKING_INTENT, result=_confirmation)


def _as_written(policy) -> Case:
    """The agent above, with its booking declared by the scenario and no intent listed:
    the effect names it."""
    case = _booking(policy, agent=TalksToItsServiceItself, effects={"create_reservation": BOOKING})
    case.intents = []
    return case


def as_written_confirms_a_failed_booking() -> Case:
    """One request to book, to the agent as written. The model confirms whatever the tool
    returned."""
    return _as_written(_confirms_whatever_came_back)


def as_written_reads_the_error() -> Case:
    """The control: the model confirms only a write that succeeded."""
    return _as_written(_reads_the_error)


def host_with_no_rule_for_stop() -> Case:
    """A scenario that is wrong in itself: its model has a rule for the menu and none for
    "Stop.", so its run is not valid evidence."""
    case = CASES["barge_in_mid_word"]()
    case.policy = ScriptedModel().when(user_said("menu"), say="We have soup.")
    return case


# ----------------------------------------------------------------------------- hangup


class _BooksAgainOnExit(lk.Agent):
    """Books in its tool, and once more when the session ends unless it had told the user
    the table is booked: a cleanup written to make sure the booking went through."""

    def __init__(self, key=None) -> None:
        super().__init__(instructions="You take restaurant reservations.")
        self._key, self._asked = key, False

    @lk.function_tool
    async def create_reservation(self, party_size: int) -> str:
        """Create a reservation.

        Args:
            party_size: Number of guests
        """
        self._asked = True
        receipt = await effect(BOOKING_INTENT, {"party_size": party_size}, key=self._key)
        return f"Reservation {receipt} confirmed for {party_size} guests."

    async def on_exit(self) -> None:
        told = any(
            item.type == "message"
            and item.role == "assistant"
            and not item.interrupted
            and "booked" in (item.text_content or "")
            for item in self.session.history.items
        )
        if self._asked and not told:
            await effect(BOOKING_INTENT, {"party_size": 6}, key=self._key)


def books_again_if_the_user_left() -> Case:
    """One request to book. The agent books again on exit if the user never heard that
    the table is booked. As written the user stays, and it books once."""
    return _booking(_reads_the_error, agent=_BooksAgainOnExit)


def books_again_with_a_key() -> Case:
    """The control: the same agent sends a key with both writes, to a backend that honours it."""
    return _booking(
        _reads_the_error,
        agent=lambda: _BooksAgainOnExit(key="table-for-6"),
        backend_mode="keyed",
    )


# ----------------------------------------------------------------------------- a write in flight


class _BooksAgainIfCutShort(lk.Agent):
    """Books in its tool, and once more if the reply that called the tool was cut short
    while the write was under way: it takes a booking the user did not stay to hear
    about for one that did not go through. The first write commits all the same."""

    def __init__(self, key=None) -> None:
        super().__init__(instructions="You take restaurant reservations.")
        self._key = key

    @lk.function_tool
    async def create_reservation(self, context: lk.RunContext, party_size: int) -> str:
        """Create a reservation.

        Args:
            party_size: Number of guests
        """
        booking = {"party_size": party_size}
        was_cut_short = context.speech_handle.interrupted
        receipt = await effect(BOOKING_INTENT, booking, key=self._key)
        if context.speech_handle.interrupted and not was_cut_short:
            receipt = await effect(BOOKING_INTENT, booking, key=self._key)
        return f"Reservation {receipt} confirmed for {party_size} guests."


def books_again_if_cut_short() -> Case:
    """One request to book. The agent books again if its reply was cut short while the
    write was under way. As written the write commits at once, and it books once."""
    return _booking(_reads_the_error, agent=_BooksAgainIfCutShort)


def books_again_if_cut_short_with_a_key() -> Case:
    """The control: the same agent sends a key with both writes, to a backend that honours it."""
    return _booking(
        _reads_the_error,
        agent=lambda: _BooksAgainIfCutShort(key="table-for-6"),
        backend_mode="keyed",
    )
