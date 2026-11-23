"""A scenario whose scripted reply has its first token two seconds in and its end a tenth of
a second in."""

from __future__ import annotations

import livekit.agents as lk

from earful.livekit import Case, Reply, ScriptedModel, UserSpeech
from shapes.corpus import invalid


def case() -> Case:
    return Case(
        name="reply_ends_before_it_starts",
        agent=lambda: lk.Agent(instructions="You are a host."),
        speeches=[UserSpeech(start=0.5, end=1.5, transcript="Hello.")],
        policy=ScriptedModel().otherwise(Reply(text="Hello there.", ttft=2.0, duration=0.1)),
    )


SHOULD = invalid("ttft")
