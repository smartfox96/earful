"""An agent whose model takes four seconds over each call and then says nothing, so the
user's question is never answered."""

from __future__ import annotations

import livekit.agents as lk
from invariants import HISTORY

from earful.livekit import Case, Delay, Reply, ScriptedModel, UserSpeech
from shapes.corpus import judged


def case() -> Case:
    return Case(
        name="says_nothing_slowly",
        agent=lambda: lk.Agent(instructions="You are a host."),
        speeches=[UserSpeech(start=0.5, end=2.5, transcript="Tell me about the menu.")],
        policy=ScriptedModel().otherwise(Reply()),
        delays=[Delay("llm.duration", 1, 4.0)],
    )


SHOULD = judged(L1="fail")
DEFECT = judged(L1="not_exercised")  # the deadline falls after the run ends
DEFECT_VARIED = {
    HISTORY: judged(M2="rule b"),
}
