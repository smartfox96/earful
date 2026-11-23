"""A scenario whose intent names its tool as a bare string, the comma of a one-element tuple
forgotten."""

from __future__ import annotations

import dataclasses

from invariants import HISTORY, TWO_PARTS

from earful.livekit import Case, Intent
from shapes.corpus import judged, refused
from shapes.livekit import books_outside_the_backend as booking


def case() -> Case:
    intent = Intent(booking.BOOKING, tools=("create_reservation"))  # type: ignore[arg-type]
    return dataclasses.replace(booking.case(), name="intent_tools_as_a_string", intents=[intent])


SHOULD = refused("tools")
# The string is read as a tuple of its letters, none of them a tool.
DEFECT = judged(A3="rule")
DEFECT_VARIED = {
    HISTORY: judged(A3="rule", M2="rule b"),
    TWO_PARTS: judged(A3="rule", M1="rule 2, rule 5"),
}
