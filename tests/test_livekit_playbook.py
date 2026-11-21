"""The scripted model written as rules, in a LiveKit session."""

import pytest

pytest.importorskip("livekit.agents")

from earful.core.ledger import first_difference, normalize
from earful.livekit.cases import BOOKING_ACK, BOOKING_INTENT, CASES
from earful.livekit.harness import InvalidRun, run_case
from earful.scripted.playbook import ScriptedModel, tool_returned, user_ever_said, user_said

BOOKING_AS_RULES = (
    ScriptedModel()
    .when(tool_returned(), say="Your table is booked.", claims=[BOOKING_INTENT])
    .when(user_ever_said("table"), say=BOOKING_ACK, call=("create_reservation", {"party_size": 6}))
    .otherwise(say="How can I help?")
)


def test_a_scenario_whose_model_is_written_as_rules_gives_the_very_same_ledger(reference_ledger):
    path = reference_ledger("book_then_interrupt")
    case = CASES["book_then_interrupt"]()
    case.policy = BOOKING_AS_RULES
    lines = normalize(run_case(case).ledger.events)
    assert first_difference(path.read_text("ascii").splitlines(), lines) is None


def test_a_run_whose_model_has_no_rule_for_a_context_is_invalid():
    case = CASES["barge_in_mid_word"]()
    case.policy = ScriptedModel().when(user_said("menu"), say="We have soup.")
    with pytest.raises(InvalidRun, match="policy raised on call 2: NoRule"):
        run_case(case)  # the second turn is "Stop."
