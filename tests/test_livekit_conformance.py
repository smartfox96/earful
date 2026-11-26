"""The behaviours every adapter must have (tests/conformance.py), on LiveKit."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("livekit.agents")

import conformance
import livekit.agents as lk
from conformance import Scenario
from shapes.corpus import judged
from shapes.livekit import booking

from earful.livekit import (
    Case,
    Delay,
    Intent,
    ScriptedModel,
    UserSpeech,
    effect,
    run_case,
    tool_returned,
    user_said,
)

ADAPTER = "earful.livekit:run_case"
ASK = UserSpeech(start=0.5, end=2.5, transcript="A table for four tonight, please.")


class SendsInBackground(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You take restaurant reservations.")
        self.sending: set[asyncio.Task[str]] = set()

    @lk.function_tool
    async def book_table(self) -> str:
        """Book a table."""
        task = asyncio.create_task(effect(booking.BOOKING, {"party_size": 4}))
        self.sending.add(task)
        return "The booking is on its way."


def write_in_flight() -> Case:
    return Case(
        name="write_in_flight",
        agent=SendsInBackground,
        speeches=[ASK],
        policy=ScriptedModel()
        .when(tool_returned(), say="It is on its way.")
        .otherwise(say="One moment.", call="book_table"),
        intents=[Intent(booking.BOOKING, tools=("book_table",))],
        delays=[Delay("backend.latency", 1, 10.0)],  # past the end of the run
    )


class FailsToGreet(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You are a host.")

    async def on_enter(self) -> None:
        raise RuntimeError(conformance.RAISED)


def application_raises() -> Case:
    return Case(
        name="application_raises",
        agent=FailsToGreet,
        speeches=[UserSpeech(start=0.5, end=1.5, transcript="Hello.")],
        policy=ScriptedModel().otherwise(say="Hello, how can I help?"),
    )


class HangsUp(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You are a host.")

    @lk.function_tool
    async def end_call(self, context: lk.RunContext) -> None:
        """End the call."""
        await context.wait_for_playout()
        context.session.shutdown()


def closes_early() -> Case:
    return Case(
        name="closes_early",
        agent=HangsUp,
        speeches=[
            UserSpeech(start=0.5, end=1.5, transcript="Okay, bye."),
            UserSpeech(start=12.0, end=13.0, transcript="Hello? Are you there?"),
        ],
        policy=ScriptedModel()
        .when(user_said("bye"), say="Goodbye then.", call="end_call")
        .otherwise(say="How can I help?"),
    )


class WaitsForever(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You take restaurant reservations.")

    @lk.function_tool
    async def book_table(self) -> str:
        """Book a table."""
        await asyncio.Event().wait()  # an answer that never comes
        return "Booked."


def stuck() -> Case:
    return Case(
        name="stuck",
        agent=WaitsForever,
        speeches=[ASK],
        policy=ScriptedModel()
        .when(tool_returned(), say="It is booked.")
        .otherwise(say="One moment.", call="book_table"),
    )


class GreetsWithoutTools(booking.Host):
    async def on_enter(self) -> None:
        self.session.generate_reply(tool_choice="none")


def tool_choice_none() -> Case:
    return Case(
        name="tool_choice_none",
        agent=GreetsWithoutTools,
        speeches=[UserSpeech(start=4.0, end=5.0, transcript="Hello.")],
        policy=ScriptedModel().otherwise(say="Hello.", call=("book_table", {"party_size": 4})),
        intents=[Intent(booking.BOOKING, tools=("book_table",))],
    )


SCENARIOS = {
    conformance.WRITE_SETTLES: Scenario(write_in_flight),
    # livekit-agents logs the exception, and the run counts it as a framework warning only.
    conformance.RAISE_RECORDED: Scenario(application_raises, defect=judged()),
    conformance.CLOSED_ENDS: Scenario(closes_early),
    conformance.STUCK_CEILING: Scenario(stuck),
    conformance.TOOL_CHOICE: Scenario(tool_choice_none),
    conformance.ONE_LEDGER: Scenario(booking.case),
}


@pytest.mark.parametrize("behaviour", conformance.params(SCENARIOS, lk.__version__))
def test_behaviour(behaviour: conformance.Behaviour) -> None:
    scenario = SCENARIOS[behaviour]
    seen = conformance.observe(behaviour, scenario, ADAPTER, run_case)
    conformance.judge(behaviour, scenario, seen)


def test_every_behaviour_has_a_scenario():
    assert list(SCENARIOS) == list(conformance.BEHAVIOURS)
