"""A front desk agent that hands the call to a billing agent built with a context of its
own, which then answers a question about an invoice."""

from __future__ import annotations

import livekit.agents as lk

from earful.livekit import Case, ScriptedModel, UserSpeech, tool_returned, tool_succeeded, user_said
from shapes.corpus import judged


class Billing(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You are the billing agent.")

    @lk.function_tool
    async def lookup_invoice(self, number: str) -> str:
        """Look up an invoice.

        Args:
            number: The invoice number
        """
        return "paid"


class FrontDesk(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You are the front desk.")

    @lk.function_tool
    async def to_billing(self) -> lk.Agent:
        """Hand the call to billing."""
        return Billing()


def case() -> Case:
    return Case(
        name="hands_off_to_a_fresh_agent",
        agent=FrontDesk,
        speeches=[
            UserSpeech(start=0.5, end=2.0, transcript="Billing please."),
            UserSpeech(start=8.0, end=9.5, transcript="Is invoice seven paid?"),
        ],
        policy=ScriptedModel()
        .when(tool_succeeded("lookup_invoice"), say="It is paid.")
        .when(user_said("invoice"), say="Checking.", call=("lookup_invoice", {"number": "7"}))
        .when(tool_returned("to_billing"), say="Billing here.")
        .otherwise(say="One moment.", call="to_billing"),
    )


# The fresh context is the application's choice: no memory the framework lost.
SHOULD = judged()
# The turn the new agent answers is said to reach no model call.
DEFECT = judged(M1="rule 2", M2="rule a, rule b", L1="milestone 1")
