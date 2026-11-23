"""An agent with a long reply that the user interrupts inside a word, heard through a voice
activity detector that needs 0.35 s of speech before it reports any."""

from __future__ import annotations

import dataclasses

from invariants import HISTORY, TWO_PARTS

from earful.livekit import Case
from earful.livekit.cases import barge_in_mid_word
from shapes.corpus import judged


def case() -> Case:
    return dataclasses.replace(
        barge_in_mid_word(), name="slow_voice_detection", vad_min_speech=0.35
    )


SHOULD = judged(L2="pass")
DEFECT = judged(L2="rule a")  # the interruption point falls before the detector reports speech
DEFECT_VARIED = {
    HISTORY: judged(M2="rule a, rule b", L2="rule a"),
    TWO_PARTS: judged(M1="rule 2, rule 3, rule 5", L2="rule a"),
}
