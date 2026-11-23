"""A scenario of five user turns whose scripted model has a rule for none of them: the
script is incomplete."""

from __future__ import annotations

import livekit.agents as lk

from earful.livekit import Case, ScriptedModel, UserSpeech, user_said
from shapes.corpus import ends_within, hangs, invalid


def case() -> Case:
    return Case(
        name="runs_out_of_script",
        agent=lambda: lk.Agent(instructions="You are a host."),
        speeches=[
            UserSpeech(start=1 + 4 * n, end=2 + 4 * n, transcript=f"Hello number {n}.")
            for n in range(5)
        ],
        policy=ScriptedModel().when(user_said("table"), say="Booked."),
    )


SHOULD = ends_within(20, invalid("NoRule"))
# At four turns the missing rule is named at once; at five the run stalls.
DEFECT = hangs()
