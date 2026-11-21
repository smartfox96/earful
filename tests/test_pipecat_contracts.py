"""On Pipecat, contracts detect a deliberate break in a bot the user speaks over."""

from __future__ import annotations

from dataclasses import replace

import pytest

pytest.importorskip("pipecat")

from pipecat.frames.frames import LLMContextFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.turns.user_start.vad_user_turn_start_strategy import VADUserTurnStartStrategy
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from earful import pipecat as pc
from earful.core.checker import FAIL, NOT_EXERCISED, PASS

INTENT = "book-table"
MENU = (
    "Tonight we have a roasted squash soup to start. Then grilled sea bass with lemon "
    "butter, or a slow braised short rib. For dessert there is a dark chocolate tart."
)
BOOK = ("create_reservation", {"party_size": 6})
ASK = pc.UserSpeech(0.5, 2.5, "Book a table for six tonight.")
ACK = "One moment while I book that table for you and confirm it with the restaurant."
BOOKED = "Your table is booked for six people tonight at eight."


def _rules(report, contract):
    return sorted({f.rule for v in report.results[contract].failures for f in v.findings})


def _bot(*, key=None, latency=0.0):
    def bot(llm):
        async def create_reservation(params):
            receipt = await pc.effect(INTENT, dict(params.arguments), key=key, latency=latency)
            await params.result_callback(f"Reservation {receipt} confirmed.")

        llm.register_function("create_reservation", create_reservation)
        return LLMContext()

    return bot


def _booking(policy, *then, bot=None, **kwargs):
    return pc.Case(
        "break",
        bot or _bot(),
        [ASK, *then],
        policy,
        intents=[pc.Intent(INTENT, tools=("create_reservation",))],
        tts_word_timestamps=True,
        **kwargs,
    )


def _pipeline(*, before_llm=(), interruptions=True):
    """The standard seven, with processors of the application before the model."""

    def pipeline(ends):
        turns = UserTurnStrategies(
            start=[VADUserTurnStartStrategy(enable_interruptions=interruptions)],
            stop=[SpeechTimeoutUserTurnStopStrategy()],
        )
        pair = LLMContextAggregatorPair(
            ends.context,
            user_params=LLMUserAggregatorParams(user_turn_strategies=turns, empty_user_turn=None),
        )
        return [
            ends.input,
            ends.stt,
            pair.user(),
            *(make() for make in before_llm),
            ends.llm,
            ends.tts,
            ends.output,
            pair.assistant(),
        ]

    return pipeline


# The user corrects themselves over the confirmation, 1 s into it.
CANCEL = pc.UserSpeech(5.0, 6.0, "Actually cancel that.", retires=(INTENT,))


def _correction(honours):
    again = pc.reply("Booking it again.", call=BOOK) if not honours else pc.reply("Cancelled.")
    return (
        pc.ScriptedModel()
        .when(pc.user_said("cancel") & (lambda context: len(context.tool_results) < 2), again)
        .when(pc.user_said("cancel"), say="Done.")
        .when(pc.tool_succeeded(), say=BOOKED, claims=[INTENT])
        .otherwise(call=BOOK)
    )


def test_a1_fails_when_the_bot_books_again_after_the_user_cancelled_over_its_reply():
    result = pc.run(_booking(_correction(honours=False), CANCEL))
    assert result.report.outcome("A1") == FAIL
    (failed,) = result.report.results["A1"].failures
    assert failed.instance == {"speech": 1, "intent": INTENT}
    assert failed.measures["writes_after_retirement"] == 1
    assert _played(result)[0]["interrupted"]  # the user spoke over the confirmation


def test_a1_passes_when_the_correction_is_honoured():
    result = pc.run(_booking(_correction(honours=True), CANCEL))
    assert result.report.outcome("A1") == PASS
    assert _played(result)[0]["interrupted"]  # the user spoke over the confirmation


def _played(result):
    return [e["data"] for e in result.events if e["kind"] == "playout.finished"]


# The user speaks over the acknowledgement while the booking is still being made.
WINDOW = pc.UserSpeech(5.0, 6.0, "Is the table by the window?")
_books_until_it_sees_a_result = (
    pc.ScriptedModel()
    .when(pc.tool_succeeded(), say="Your table is booked.", claims=[INTENT])
    .otherwise(say=ACK, call=BOOK)
)


def test_a2_fails_when_a_booking_the_user_spoke_over_is_made_again_without_a_key():
    result = pc.run(_booking(_books_until_it_sees_a_result, WINDOW, bot=_bot(latency=3.0)))
    assert result.report.outcome("A2") == FAIL
    assert result.report.results["A2"].failures[0].measures["committed"] == 2
    assert [e["kind"] for e in result.events].count("tool.cancelled") == 1


def test_a2_passes_when_the_second_call_carries_the_key_of_the_first():
    keyed = _bot(key="table-for-6", latency=3.0)
    result = pc.run(
        _booking(_books_until_it_sees_a_result, WINDOW, bot=keyed, backend_mode="keyed")
    )
    assert result.report.outcome("A2") == PASS
    measures = result.report.results["A2"].verdicts[0].measures
    assert (measures["committed"], measures["deduplicated"]) == (1, 1)


def _menu(**kwargs):
    return pc.Case(
        "break",
        lambda llm: LLMContext(),
        [pc.UserSpeech(0.5, 2.5, "Tell me about the menu."), pc.UserSpeech(5.0, 6.0, "Stop.")],
        pc.ScriptedModel().when(pc.user_said("stop"), say="Okay, stopping.").otherwise(say=MENU),
        tts_word_timestamps=True,
        **kwargs,
    )


class ForgetsToolResults(FrameProcessor):
    """Takes the results of function calls out of the context before the model reads it."""

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            kept = [m for m in frame.context.get_messages() if m.get("role") != "tool"]
            frame.context.set_messages(kept)
        await self.push_frame(frame, direction)


_books_then_answers = (
    pc.ScriptedModel()
    .when(pc.user_said("window"), say="I will ask for one.")
    .when(pc.tool_succeeded(), say=BOOKED, claims=[INTENT])
    .when(pc.user_said("window") | pc.user_ever_said("table") & ~pc.tool_returned(), call=BOOK)
)


def test_m2_fails_when_a_processor_takes_tool_results_out_of_the_context():
    result = pc.run(
        _booking(
            _books_then_answers.otherwise(say="How can I help?"),
            WINDOW,
            pipeline=_pipeline(before_llm=[ForgetsToolResults]),
        )
    )
    assert result.report.outcome("M2") == FAIL
    assert "rule d" in _rules(result.report, "M2")


def test_m2_passes_with_the_same_pipeline_without_the_processor():
    result = pc.run(
        _booking(_books_then_answers.otherwise(say="How can I help?"), WINDOW, pipeline=_pipeline())
    )
    assert result.report.outcome("M2") == PASS


def _asked_about_dessert(answer):
    """The user asks about dessert over the menu, and the model gives ``answer``."""
    asked = _menu()
    return replace(
        asked,
        speeches=[asked.speeches[0], pc.UserSpeech(5.0, 6.0, "And for dessert?")],
        policy=pc.ScriptedModel().when(pc.user_said("dessert"), answer).otherwise(say=MENU),
    )


def test_l1_fails_when_the_model_says_nothing_to_the_user_who_spoke_over_its_reply():
    result = pc.run(_asked_about_dessert(pc.Reply()))
    assert result.report.outcome("L1") == FAIL
    (failed,) = result.report.results["L1"].failures
    assert failed.instance == {"speech": 1}


def test_l1_passes_when_the_model_answers():
    result = pc.run(_asked_about_dessert(pc.reply("A dark chocolate tart.")))
    assert result.report.outcome("L1") == PASS


def test_l2_fails_when_the_bot_cannot_be_interrupted_but_the_policy_expects_it():
    result = pc.run(_menu(pipeline=_pipeline(interruptions=False)))
    assert result.report.outcome("L2") == FAIL
    assert _rules(result.report, "L2") == ["rule a"]


def test_l2_passes_when_the_policy_says_interruptions_are_not_allowed():
    deaf = _menu(
        pipeline=_pipeline(interruptions=False),
        contracts={"L2": {"interruptions_allowed": False}},
    )
    assert pc.run(deaf).report.outcome("L2") == PASS


def test_l2_passes_with_a_bot_that_stops_when_the_user_speaks():
    result = pc.run(_menu(pipeline=_pipeline()))
    assert result.report.outcome("L2") == PASS
    assert result.report.results["L2"].verdicts[1].measures["stop_latency_us"] <= 100_000


# "Your table is booked." is the claim, and the reply goes on after it.
CONFIRMS = "Your table is booked. I will see you tonight at eight."
_confirms_whatever_came_back = (
    pc.ScriptedModel()
    .when(pc.user_said("wait"), say="Yes?")
    .when(pc.tool_returned(), say=CONFIRMS, claims=[pc.Claim(INTENT, 0, 3)])
    .otherwise(say=ACK, call=BOOK)
)
REJECTED = [pc.Fault("backend.error", 1)]


def _confirmed(wait_at, **kwargs):
    """The user says "Wait." over the confirmation, which starts playing at 8.1 s."""
    wait = pc.UserSpeech(wait_at, wait_at + 0.6, "Wait.")
    result = pc.run(_booking(_confirms_whatever_came_back, wait, **kwargs))
    cut = _played(result)[1]
    assert cut["interrupted"]
    return result, len(cut["words_played"])


def test_a3_fails_when_the_user_heard_a_failed_booking_confirmed_before_speaking_over_the_rest():
    result, heard = _confirmed(8.9, faults=REJECTED)
    assert heard == 5  # the four words of the claim, and one more
    assert result.report.outcome("A3") == FAIL
    (failed,) = result.report.results["A3"].failures
    assert failed.instance["intent"] == INTENT


def test_a3_passes_when_the_booking_the_user_heard_confirmed_was_made():
    result, heard = _confirmed(8.9)
    assert heard == 5 and result.report.outcome("A3") == PASS


def test_a3_has_no_claim_to_judge_when_the_user_cut_the_confirmation_before_it_was_said():
    result, heard = _confirmed(8.5, faults=REJECTED)
    assert heard == 3  # "Your table is"
    assert result.report.outcome("A3") == NOT_EXERCISED


def _stopped(*, word_times):
    return pc.run_case(replace(_menu(), tts_word_timestamps=word_times), require_valid=False)


def test_m1_fails_when_a_voice_without_word_times_is_cut_and_the_bot_forgets_what_it_said():
    """Pipecat cannot tell how much of the reply had played, and keeps none of it."""
    report = _stopped(word_times=False).report
    assert report.outcome("M1") == FAIL and _rules(report, "M1") == ["rule 2"]
    (forgotten,) = report.results["M1"].failures
    assert (forgotten.measures["played"], forgotten.measures["cut"]) == (5, 1)


def test_m1_passes_when_the_voice_sends_word_times_and_the_bot_remembers_what_was_heard():
    report = _stopped(word_times=True).report
    assert report.outcome("M1") == PASS
    assert report.results["M1"].verdicts[0].measures["played"] == 5
