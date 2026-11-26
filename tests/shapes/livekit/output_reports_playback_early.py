"""An agent whose audio output says playback started two and a half seconds before it
does, so a user who speaks up hears a fragment of a short reply that the agent then
remembers whole."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import livekit.agents as lk

from earful.livekit import Case, ClockedSink, ScriptedModel, UserSpeech, user_said
from shapes.corpus import judged


class LateOutput(ClockedSink):
    def _begin(self, start: Callable[[], None]) -> None:
        asyncio.get_running_loop().call_later(2.5, start)


def case() -> Case:
    return Case(
        name="output_reports_playback_early",
        agent=lambda: lk.Agent(instructions="You take reservations."),
        speeches=[
            UserSpeech(start=0.5, end=2.5, transcript="Book a table for six tonight."),
            UserSpeech(start=5.25, end=6.25, transcript="Is it by the window?"),
        ],
        policy=ScriptedModel()
        .when(user_said("window"), say="Yes it is.")
        .otherwise(say="Your table is booked."),
        sink=LateOutput,
    )


# The late output makes the replies late and puts one over the user: those fails are right.
SHOULD = judged(M1="fail", L1="milestone 3", L2="rule b")
# A reply none of whose words played is remembered whole, and M1 passes it.
DEFECT = judged(L1="milestone 3", L2="rule b")
