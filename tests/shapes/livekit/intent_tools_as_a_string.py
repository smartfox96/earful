"""A scenario whose intent names its tool as a bare string, the comma of a one-element tuple
forgotten."""

from __future__ import annotations

import dataclasses

from earful.livekit import Case, Intent
from shapes.corpus import refused
from shapes.livekit import books_outside_the_backend as booking


def case() -> Case:
    intent = Intent(booking.BOOKING, tools=("create_reservation"))  # type: ignore[arg-type]
    return dataclasses.replace(booking.case(), name="intent_tools_as_a_string", intents=[intent])


SHOULD = refused("tools")
