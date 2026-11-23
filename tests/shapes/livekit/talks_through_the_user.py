"""An agent configured so the user cannot interrupt it, which talks through the user's
whole second turn."""

from __future__ import annotations

import livekit.agents as lk
from invariants import HISTORY, TWO_PARTS

from earful.livekit import Case, ScriptedModel, UserSpeech, user_said
from earful.livekit.cases import LONG_REPLY
from shapes.corpus import judged


def case() -> Case:
    return Case(
        name="talks_through_the_user",
        agent=lambda: lk.Agent(instructions="You are a host."),
        speeches=[
            UserSpeech(start=0.5, end=2.5, transcript="Tell me about the menu."),
            UserSpeech(start=5.0, end=6.0, transcript="Stop."),
        ],
        policy=ScriptedModel().when(user_said("menu"), say=LONG_REPLY).otherwise(say="Okay."),
        allow_interruptions=False,
        contracts={"L2": {"interruptions_allowed": False}},
    )


# The user's turn is dropped while the agent cannot be interrupted, and M2 and L1 say so.
SHOULD = judged(M2="rule b", L1="milestone 1", L2="not_exercised")
DEFECT = judged(M2="rule b", L1="milestone 1", L2="pass")  # said to be silent while it spoke
DEFECT_VARIED = {
    HISTORY: judged(M1="rule 2", M2="rule b", L1="milestone 1", L2="pass"),
    TWO_PARTS: judged(M1="rule 2, rule 3, rule 5", M2="rule b", L1="milestone 1", L2="pass"),
}
