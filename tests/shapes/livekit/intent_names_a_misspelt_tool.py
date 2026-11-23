"""A scenario whose intent names a tool the agent does not have, its name misspelt."""

from __future__ import annotations

import dataclasses

from invariants import HISTORY, TWO_PARTS

from earful.livekit import Case, Intent
from shapes.corpus import judged, refused
from shapes.livekit import books_outside_the_backend as booking


def case() -> Case:
    intent = Intent(booking.BOOKING, tools=("create_reservaton",))
    return dataclasses.replace(
        booking.case(), name="intent_names_a_misspelt_tool", intents=[intent]
    )


SHOULD = refused("create_reservaton")
# The intent is held to a tool that never runs.
DEFECT = judged(A3="rule")
DEFECT_VARIED = {
    HISTORY: judged(A3="rule", M2="rule b"),
    TWO_PARTS: judged(A3="rule", M1="rule 2, rule 5"),
}
