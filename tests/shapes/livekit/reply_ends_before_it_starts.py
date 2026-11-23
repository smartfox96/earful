"""A scenario whose scripted reply has its first token two seconds in and its end a tenth of
a second in."""

from __future__ import annotations

import livekit.agents as lk
from invariants import HISTORY, TWO_PARTS

from earful.livekit import Case, Reply, ScriptedModel, UserSpeech
from shapes.corpus import invalid, judged


def case() -> Case:
    return Case(
        name="reply_ends_before_it_starts",
        agent=lambda: lk.Agent(instructions="You are a host."),
        speeches=[UserSpeech(start=0.5, end=1.5, transcript="Hello.")],
        policy=ScriptedModel().otherwise(Reply(text="Hello there.", ttft=2.0, duration=0.1)),
    )


SHOULD = invalid("ttft")
# The end is moved to the first token, and the run is judged.
DEFECT = judged()
DEFECT_VARIED = {
    HISTORY: judged(M1="rule 2", M2="rule b"),
    TWO_PARTS: judged(M1="rule 2, rule 5"),
}
