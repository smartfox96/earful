"""A scenario that declares a burst of noise, with no words in it, as a turn that is owed a
reply. Nothing can answer it, so the case contradicts itself."""

from __future__ import annotations

import livekit.agents as lk

from earful.livekit import Case, Reply, ScriptedModel, UserSpeech
from shapes.corpus import refused


def case() -> Case:
    return Case(
        name="noise_owes_a_reply",
        agent=lambda: lk.Agent(instructions="You are a host."),
        speeches=[
            UserSpeech(start=0.5, end=2.5, transcript="Tell me about the menu."),
            UserSpeech(start=4.0, end=4.6, transcript="", expects_reply=True),
        ],
        policy=ScriptedModel().otherwise(Reply()),
    )


SHOULD = refused("expects_reply")
