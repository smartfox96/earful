"""A scenario that sets the drain delay to minus five seconds."""

from __future__ import annotations

import livekit.agents as lk

from earful.livekit import Case, ScriptedModel, UserSpeech
from shapes.corpus import judged, refused


def case() -> Case:
    return Case(
        name="negative_drain_delay",
        agent=lambda: lk.Agent(instructions="You are a host."),
        speeches=[UserSpeech(start=0.5, end=1.5, transcript="Hello.")],
        policy=ScriptedModel().otherwise(say="Hello there."),
        drain_delay=-5,
    )


SHOULD = refused("drain_delay")
DEFECT = judged()
