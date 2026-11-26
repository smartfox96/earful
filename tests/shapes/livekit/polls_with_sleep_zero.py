"""An agent whose tool waits for a flag a timer sets, yielding with asyncio.sleep(0) between
looks, which works on a real event loop."""

from __future__ import annotations

import asyncio

import livekit.agents as lk

from earful.livekit import Case, ScriptedModel, UserSpeech, tool_returned, user_ever_said
from shapes.corpus import ends_within, judged


class Poller(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You look things up.")

    @lk.function_tool
    async def look_up(self) -> str:
        """Look it up."""
        done: list[int] = []
        asyncio.get_running_loop().call_later(0.2, done.append, 1)
        while not done:
            await asyncio.sleep(0)
        return "found"


def case() -> Case:
    return Case(
        name="polls_with_sleep_zero",
        agent=Poller,
        speeches=[UserSpeech(start=0.5, end=1.5, transcript="Please look it up.")],
        policy=ScriptedModel()
        .when(tool_returned(), say="Found it.")
        .when(user_ever_said("look"), say="One moment.", call="look_up")
        .otherwise(say="Hello there."),
    )


SHOULD = ends_within(20, judged())
