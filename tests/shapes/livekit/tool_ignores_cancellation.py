"""An agent whose tool waits in a loop that swallows cancellation, so nothing can stop it."""

from __future__ import annotations

import asyncio

import livekit.agents as lk

from earful.livekit import Case, ScriptedModel, UserSpeech, tool_returned
from shapes.corpus import ends_within, hangs, invalid


class Stubborn(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You are a host.")

    @lk.function_tool
    async def wait_for_stock(self) -> str:
        """Wait until the item is back in stock."""
        while True:
            try:
                await asyncio.sleep(1000)
            except asyncio.CancelledError:
                pass


def case() -> Case:
    return Case(
        name="tool_ignores_cancellation",
        agent=Stubborn,
        speeches=[UserSpeech(start=0.5, end=1.5, transcript="Tell me when it is back.")],
        policy=ScriptedModel()
        .when(tool_returned(), say="It is back.")
        .otherwise(say="One moment.", call="wait_for_stock"),
    )


# A run that cannot finish is a harness error that names what was still running.
SHOULD = ends_within(20, invalid("wait_for_stock"))
DEFECT = hangs()
