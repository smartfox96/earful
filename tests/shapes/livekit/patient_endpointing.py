"""An agent for callers who speak slowly: on entering, it makes the session wait three
seconds of silence before it takes the turn."""

from __future__ import annotations

import livekit.agents as lk
from invariants import HISTORY, LONGER

from earful.livekit import Case, ScriptedModel, UserSpeech
from shapes.corpus import judged


class Patient(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You are a host.")

    async def on_enter(self) -> None:
        self.session.update_options(endpointing_opts={"min_delay": 3.0, "max_delay": 6.0})


def case() -> Case:
    return Case(
        name="patient_endpointing",
        agent=Patient,
        speeches=[UserSpeech(start=0.5, end=1.5, transcript="Hello there.")],
        policy=ScriptedModel().otherwise(say="How can I help?"),
    )


SHOULD = judged()
# The deadline is held to the endpointing the run started with.
DEFECT = judged(L1="milestone 3")
DEFECT_VARIED = {
    HISTORY: judged(M1="rule 2", M2="rule b", L1="milestone 3"),
}
NOT_VARIED = {LONGER: "its L1 fail is the defect itself, so a slower run tells nothing"}
