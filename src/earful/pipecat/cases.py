"""Reference cases: one per behaviour the recorded Pipecat ledgers rest on."""

from __future__ import annotations

from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.llm_service import FunctionCallParams, LLMService

from ..core.backend import effect
from ..scripted.case import Intent
from ..scripted.playbook import ScriptedModel, tool_succeeded, user_said
from ..scripted.world import UserSpeech
from .harness import Case

MENU = (
    "Tonight we have a roasted squash soup to start. Then grilled sea bass with lemon "
    "butter, or a slow braised short rib. For dessert there is a dark chocolate tart."
)  # 29 words, 124 letters -> 7.44 s of audio at 0.06 s per letter


def _no_functions(llm: LLMService) -> LLMContext:
    return LLMContext()


def greeting() -> Case:
    """One user turn, and a reply of two sentences that plays to its end."""
    return Case(
        name="greeting",
        bot=_no_functions,
        speeches=[UserSpeech(start=0.5, end=1.0, transcript="Hello.")],
        policy=ScriptedModel().otherwise(say="Good morning. How can I help you today?"),
    )


RESERVATION_INTENT = "reserve-table-1"


def _takes_reservations(llm: LLMService) -> LLMContext:
    async def create_reservation(params: FunctionCallParams) -> None:
        receipt = await effect(RESERVATION_INTENT, dict(params.arguments))
        await params.result_callback(f"Reservation {receipt} confirmed.")

    llm.register_function("create_reservation", create_reservation)
    return LLMContext()


def reserve_table() -> Case:
    """A function call that makes one backend write, and the reply that confirms it."""
    return Case(
        name="reserve_table",
        bot=_takes_reservations,
        speeches=[UserSpeech(start=0.5, end=2.5, transcript="A table for six tonight, please.")],
        policy=ScriptedModel()
        .when(tool_succeeded(), say="Your table is booked.", claims=[RESERVATION_INTENT])
        .otherwise(say="One moment.", call=("create_reservation", {"party_size": 6})),
        intents=[Intent(RESERVATION_INTENT, tools=("create_reservation",))],
    )


def barge_in() -> Case:
    """A long reply, and a user who speaks over it inside a word."""
    return Case(
        name="barge_in",
        bot=_no_functions,
        speeches=[
            UserSpeech(start=0.5, end=2.5, transcript="Tell me about the menu."),
            UserSpeech(start=5.0, end=6.0, transcript="Stop."),
        ],
        policy=ScriptedModel().when(user_said("stop"), say="Okay, stopping.").otherwise(say=MENU),
        tts_word_timestamps=True,
    )


CASES = {
    "greeting": greeting,
    "reserve_table": reserve_table,
    "barge_in": barge_in,
}
