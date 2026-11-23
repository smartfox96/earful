"""A scenario with a speech that starts a second before the run does."""

from __future__ import annotations

import livekit.agents as lk
from invariants import SHIFTED

from earful.livekit import Case, ScriptedModel, UserSpeech
from shapes.corpus import invalid, refused


def case() -> Case:
    return Case(
        name="speech_before_the_start",
        agent=lambda: lk.Agent(instructions="You are a host."),
        speeches=[UserSpeech(start=-1.0, end=0.5, transcript="Hello.")],
        policy=ScriptedModel().otherwise(say="Hello there."),
    )


SHOULD = refused("-1.0")
# It runs, and its own header then fails validation.
DEFECT = invalid("less than the minimum of 0")
NOT_VARIED = {SHIFTED: "the shift gives the speech a start the run can have: another case"}
