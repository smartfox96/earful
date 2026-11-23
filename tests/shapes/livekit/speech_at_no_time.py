"""A scenario with a speech whose start is not a number, as a computed time can become."""

from __future__ import annotations

import livekit.agents as lk

from earful.livekit import Case, ScriptedModel, UserSpeech
from shapes.corpus import refused


def case() -> Case:
    return Case(
        name="speech_at_no_time",
        agent=lambda: lk.Agent(instructions="You are a host."),
        speeches=[UserSpeech(start=float("nan"), end=1.5, transcript="Hello.")],
        policy=ScriptedModel().otherwise(say="Hello there."),
    )


SHOULD = refused("start")
# A raw error of the conversion to microseconds.
DEFECT = refused("cannot convert float NaN to integer")
