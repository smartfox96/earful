"""A scenario whose intent names a tool the agent does not have, its name misspelt."""

from __future__ import annotations

import dataclasses

from earful.livekit import Case, Intent
from shapes.corpus import refused
from shapes.livekit import books_outside_the_backend as booking


def case() -> Case:
    intent = Intent(booking.BOOKING, tools=("create_reservaton",))
    return dataclasses.replace(
        booking.case(), name="intent_names_a_misspelt_tool", intents=[intent]
    )


SHOULD = refused("create_reservaton")
