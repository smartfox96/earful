"""A booking agent whose scenario lets an intent it never declares be written again after
the user retires it."""

from __future__ import annotations

import dataclasses

from invariants import HISTORY, TWO_PARTS

from earful.livekit import Case
from shapes.corpus import judged, refused
from shapes.livekit import booking


def case() -> Case:
    contracts = {"A1": {"reissue_allowed": ["no-such-intent"]}}
    return dataclasses.replace(
        booking.case(), name="reissue_for_unknown_intent", contracts=contracts
    )


SHOULD = refused("no-such-intent")
DEFECT = judged()
DEFECT_VARIED = {
    HISTORY: judged(M2="rule b"),
    TWO_PARTS: judged(M1="rule 2, rule 3, rule 5"),
}
