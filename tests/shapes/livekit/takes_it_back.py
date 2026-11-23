"""A user who asks about the menu and takes the question back before the answer plays. The
agent rightly says nothing more."""

from __future__ import annotations

import livekit.agents as lk
from invariants import LONGER

from earful.livekit import Case, Reply, ScriptedModel, UserSpeech, user_said
from shapes.corpus import judged


def case() -> Case:
    return Case(
        name="takes_it_back",
        agent=lambda: lk.Agent(instructions="You are a host."),
        speeches=[
            UserSpeech(start=0.5, end=2.5, transcript="Tell me about the menu."),
            UserSpeech(
                start=2.95, end=3.95, transcript="Actually never mind.", expects_reply=False
            ),
        ],
        policy=ScriptedModel()
        .when(user_said("never mind"), Reply())
        .otherwise(say="Tonight we have soup to start and a tart for dessert."),
    )


SHOULD = judged(L1="pass")
DEFECT = judged(L1="milestone 3")  # the taken-back turn is still owed a reply
DEFECT_VARIED = {
    LONGER: judged(L1="not_exercised"),
}
