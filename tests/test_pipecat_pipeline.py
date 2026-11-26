"""A Pipecat case's own pipeline: what runs between Earful's ends, and what is refused."""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Any

import pytest

pytest.importorskip("pipecat")

from pipecat.frames.frames import LLMTextFrame, StartFrame, TranscriptionFrame
from pipecat.pipeline.parallel_pipeline import ParallelPipeline
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMAssistantAggregatorParams,
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.turns.user_mute.function_call_user_mute_strategy import FunctionCallUserMuteStrategy
from pipecat.turns.user_start.min_words_user_turn_start_strategy import (
    MinWordsUserTurnStartStrategy,
)
from pipecat.turns.user_start.transcription_user_turn_start_strategy import (
    TranscriptionUserTurnStartStrategy,
)
from pipecat.turns.user_start.vad_user_turn_start_strategy import VADUserTurnStartStrategy
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import (
    StrategyMode,
    UserTurnStrategies,
    UserTurnStrategiesParams,
)

from earful import pipecat as pc
from earful.pipecat.providers import ScriptedSTT, StampedTTS
from earful.pipecat.sink import ClockedOutput

NOTHING: Any = None  # for a processor that is refused before it is used
MENU = "Tonight we have a roasted squash soup to start. Then a grilled sea bass."


class Heard(FrameProcessor):
    """Passes every frame on, and keeps the text of the kind it is asked to hear."""

    def __init__(self, kind, into):
        super().__init__()
        self._kind, self._into = kind, into

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, self._kind):
            self._into.append(frame.text)
        await self.push_frame(frame, direction)


def turns(start=None, speech_timeout=0.6, **stop):
    return UserTurnStrategies(
        start=start or [VADUserTurnStartStrategy()],
        stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=speech_timeout, **stop)],
    )


def around(
    *,
    after_stt=(),
    after_llm=(),
    after_output=(),
    strategies=turns,
    wrap=list,
    assistant=None,
    **user,
):
    """A pipeline function: the standard order, with the given processors and settings."""

    def pipeline(ends):
        params = LLMUserAggregatorParams(user_turn_strategies=strategies(), **user)
        pair = LLMContextAggregatorPair(
            ends.context, user_params=params, assistant_params=assistant
        )
        return wrap(
            [
                ends.input,
                ends.stt,
                *(make() for make in after_stt),
                pair.user(),
                ends.llm,
                *(make() for make in after_llm),
                ends.tts,
                ends.output,
                *(make() for make in after_output),
                pair.assistant(),
            ]
        )

    return pipeline


def menu(**kwargs):
    return pc.Case(
        "menu",
        lambda llm: LLMContext(),
        [pc.UserSpeech(0.25, 0.75, "What is on the menu?"), pc.UserSpeech(3.0, 4.0, "Stop.")],
        pc.ScriptedModel().when(pc.user_said("Stop"), say="Okay.").otherwise(say=MENU),
        tts_word_timestamps=True,
        **kwargs,
    )


def events(result, kind):
    return [e["data"] for e in result.events if e["kind"] == kind]


def options(result):
    return result.events[0]["data"]["scenario"]["session"]["extra_options"]


def timeline(result):
    return [(e["t_us"], e["kind"]) for e in result.events[1:]]


@pytest.mark.parametrize("wrap", [list, Pipeline], ids=["a list", "a Pipeline"])
def test_a_case_runs_its_own_processors_between_earfuls_ends(wrap):
    said, transcribed = [], []
    built = menu(
        pipeline=around(
            after_stt=[lambda: Heard(TranscriptionFrame, transcribed)],
            after_llm=[lambda: Heard(LLMTextFrame, said)],
            wrap=wrap,
        )
    )
    result = pc.run(built)
    result.assert_contracts()
    assert transcribed == ["What is on the menu?", "Stop."]
    assert "".join(said) == MENU + "Okay."
    assert options(result) == {
        "user_turn_start": "VADUserTurnStartStrategy",
        "user_turn_stop": "SpeechTimeoutUserTurnStopStrategy",
        "pipeline": [
            "ScriptedUser",
            "ScriptedSTT",
            "Heard",
            "LLMUserAggregator",
            "ScriptedLLM",
            "Heard",
            "StampedTTS",
            "ClockedOutput",
            "LLMAssistantAggregator",
        ],
        # Pipecat answers a turn without words that cut the bot off, unless told not to.
        "empty_user_turn": options(result)["empty_user_turn"],
    }
    assert options(result)["empty_user_turn"]["max_consecutive_recoveries"] == 1
    # Processors that pass every frame on leave the run as the standard pipeline makes it.
    assert timeline(result) == timeline(pc.run(menu()))
    assert result.hash == pc.run(built).hash != pc.run(menu()).hash


def test_the_standard_pipeline_states_only_its_two_strategies():
    assert options(pc.run(menu())) == {
        "user_turn_start": "VADUserTurnStartStrategy",
        "user_turn_stop": "SpeechTimeoutUserTurnStopStrategy",
    }


def test_processors_in_a_parallel_pipeline_run_and_the_header_shows_where():
    said, also = [], []

    def branches():
        return ParallelPipeline([Heard(LLMTextFrame, said)], [Heard(LLMTextFrame, also)])

    result = pc.run(menu(pipeline=around(after_output=[branches], empty_user_turn=None)))
    assert result.report.valid
    assert options(result)["pipeline"][-2] == {
        "ParallelPipeline": [{"Pipeline": ["Heard"]}, {"Pipeline": ["Heard"]}]
    }
    assert "empty_user_turn" not in options(result)


def slow_booking(**kwargs):
    """A booking that takes 4 s, and a user who speaks while it is being made."""

    def bot(llm):
        async def book(params):
            await params.result_callback(await pc.effect("table", dict(params.arguments)))

        llm.register_function("book", book)
        return LLMContext()

    model = (
        pc.ScriptedModel()
        .when(pc.tool_succeeded(), say="Your table is booked.", claims=["table"])
        .when(pc.user_said("window"), say="I will ask for one.")
        .otherwise(call=("book", {"seats": 4}))
    )
    return pc.Case(
        "slow booking",
        bot,
        [
            pc.UserSpeech(0.25, 0.75, "A table for four."),
            pc.UserSpeech(3.0, 4.0, "By the window?", expects_reply=False),
        ],
        model,
        intents=[pc.Intent("table")],
        delays=[pc.Delay("backend.latency", 1, 4.0)],
        **kwargs,
    )


def test_a_mute_strategy_of_the_case_decides_whether_the_user_is_heard():
    heard = pc.run(slow_booking())
    assert events(heard, "tool.cancelled") and not events(heard, "tool.returned")
    muted = pc.run(
        slow_booking(pipeline=around(user_mute_strategies=[FunctionCallUserMuteStrategy()]))
    )
    assert options(muted)["user_mute_strategies"] == ["FunctionCallUserMuteStrategy"]
    assert not events(muted, "tool.cancelled") and len(events(muted, "tool.returned")) == 1
    origin = next(e["t_us"] for e in muted.events if e["kind"] == "user.origin")
    clears = [e["t_us"] - origin for e in muted.events if e["kind"] == "sink.clear"]
    assert clears == [450_000]  # the first speech alone starts a turn
    assert muted.report.outcomes()["A2"] == "pass"


def test_a_start_strategy_that_does_not_interrupt_is_stated_and_the_reply_plays_on():
    def quiet():
        return turns(start=[VADUserTurnStartStrategy(enable_interruptions=False)])

    built = menu(pipeline=around(strategies=quiet, empty_user_turn=None))
    result = pc.run(built)
    assert (
        options(result)["user_turn_start"] == "VADUserTurnStartStrategy(enable_interruptions=False)"
    )
    first, _second = events(result, "playout.finished")
    assert not first["interrupted"] and first["text_played"] == MENU
    with pytest.raises(pc.ContractFailure, match="L2/rule a"):
        result.assert_contracts()
    built.contracts = {"L2": {"interruptions_allowed": False}}
    assert pc.run(built).report.outcomes()["L2"] == "pass"


def test_the_default_start_strategies_run_and_a_late_transcript_starts_a_turn():
    def default():
        return turns(start=[VADUserTurnStartStrategy(), TranscriptionUserTurnStartStrategy()])

    # Pipecat ends the turn 5 s after the VAD's stop. The first words come at 6.75 s.
    late = pc.UserSpeech(0.25, 0.75, "What is on the menu?", stt_delay=12.0)

    def run(pipeline):
        result = pc.run(replace(menu(pipeline=pipeline), speeches=[late]))
        origin = next(e["t_us"] for e in result.events if e["kind"] == "user.origin")
        return result, [
            e["t_us"] - origin for e in result.events if e["kind"] == "fw.user_turn_started"
        ]

    _, on_the_vad = run(around(empty_user_turn=None))
    result, on_either = run(around(strategies=default, empty_user_turn=None))
    assert options(result)["user_turn_start"] == (
        "VADUserTurnStartStrategy, TranscriptionUserTurnStartStrategy"
    )
    assert on_the_vad == [450_000]
    assert on_either == [450_000, 6_750_000, 12_750_000]
    result.assert_contracts()


def test_the_case_declares_the_timeouts_its_pipeline_uses():
    patient = around(strategies=lambda: turns(speech_timeout=1.5), user_turn_stop_timeout=8.0)
    with pytest.raises(ValueError, match=r"user_speech_timeout=1.5 and the case declares 0.6"):
        pc.run(menu(pipeline=patient))
    with pytest.raises(ValueError, match=r"user_turn_stop_timeout=8 and the case declares 5"):
        pc.run(menu(pipeline=patient, user_speech_timeout=1.5))
    result = pc.run(menu(pipeline=patient, user_speech_timeout=1.5, user_turn_stop_timeout=8.0))
    session = result.events[0]["data"]["scenario"]["session"]
    assert session["min_endpointing_delay_us"] == 1_500_000
    assert session["max_endpointing_delay_us"] == 8_000_000
    origin = next(e["t_us"] for e in result.events if e["kind"] == "user.origin")
    stopped = [e["t_us"] - origin for e in result.events if e["kind"] == "fw.user_turn_stopped"]
    assert stopped[0] == 750_000 + 200_000 + 1_500_000  # the speech, the VAD, then the strategy


def _outside_the_top_level(ends):
    pair = LLMContextAggregatorPair(
        ends.context, user_params=LLMUserAggregatorParams(user_turn_strategies=turns())
    )
    return [
        ends.input,
        ends.stt,
        pair.user(),
        ends.llm,
        ParallelPipeline([ends.tts]),
        ends.output,
        pair.assistant(),
    ]


def _out_of_order(ends):
    standard = around()(ends)
    return [standard[1], standard[0], *standard[2:]]


def _another_context(ends):
    return around()(replace(ends, context=LLMContext()))


def _a_second_user_aggregator(ends):
    spare = LLMContextAggregatorPair(
        ends.context, user_params=LLMUserAggregatorParams(user_turn_strategies=turns())
    )
    return [*around()(ends), ParallelPipeline([spare.user()])]


def _a_voice(ends):
    return [*around()(ends), StampedTTS(None, None, seconds_per_letter=0.06, ttfb=0.2, ttfb_us={})]


def _named(start=None, **stop):
    return lambda: turns(start=start, **stop)


def _realtime_without_recognizer(ends):
    from pipecat.services.llm_service import LLMService

    # Provider extras are optional in both pinned environments. Match the base class
    # identity used by the adapter, then test a user subclass with a different name.
    base = type("GeminiLiveLLMService", (LLMService,), {"__module__": "pipecat.services.google"})

    class AppVoice(base):
        pass

    return [ends.input, AppVoice(), ends.output]


REFUSED = {
    "a speech-to-speech pipeline": (
        _realtime_without_recognizer,
        "AppVoice is a speech-to-speech model",
    ),
    "an end left out": (lambda ends: around()(ends)[1:], "hold ends.input once"),
    "an end inside another processor": (_outside_the_top_level, "hold ends.tts once"),
    "ends out of order": (_out_of_order, "in order and at its top level, ends.input, ends.stt"),
    "no aggregators": (
        lambda ends: [ends.input, ends.stt, ends.llm, ends.tts, ends.output],
        "in order",
    ),
    "another context": (_another_context, "built on ends.context"),
    "a second user aggregator": (_a_second_user_aggregator, "LLMUserAggregator decides turns"),
    "a service": (_a_voice, "a second provider has no scripted inputs or witnessed output"),
    "a transport": (
        lambda ends: [*around()(ends), ClockedOutput(NOTHING, NOTHING)],
        "ClockedOutput is a transport",
    ),
    "a VAD processor": (
        around(after_stt=[lambda: VADProcessor(vad_analyzer=NOTHING)]),
        "VADProcessor runs a VAD on audio in a thread",
    ),
    "something that is no processor": (lambda ends: [*around()(ends), "tap"], "or a Pipeline"),
    "a VAD analyzer": (around(vad_analyzer=object()), "a vad_analyzer hears audio in a thread"),
    "Pipecat's default stop strategy": (
        around(strategies=lambda: UserTurnStrategies(start=[VADUserTurnStartStrategy()])),
        "TurnAnalyzerUserTurnStopStrategy, Pipecat's default, runs a turn model",
    ),
    "two stop strategies": (
        around(
            strategies=lambda: UserTurnStrategies(
                start=[VADUserTurnStartStrategy()],
                stop=[SpeechTimeoutUserTurnStopStrategy(), SpeechTimeoutUserTurnStopStrategy()],
            )
        ),
        "is not a turn stop the run's header can state",
    ),
    "a stop that does not wait for words": (
        around(strategies=_named(wait_for_transcript=False)),
        "is for a realtime model",
    ),
    "a start on a number of words": (
        around(
            strategies=_named(
                [VADUserTurnStartStrategy(), MinWordsUserTurnStartStrategy(min_words=3)]
            )
        ),
        "MinWordsUserTurnStartStrategy is not a turn start the run's header can state",
    ),
    "no start on the VAD": (
        around(strategies=_named([TranscriptionUserTurnStartStrategy()])),
        "need one VADUserTurnStartStrategy",
    ),
    "strategies that all have to agree": (
        around(user_turn_strategies_params=UserTurnStrategiesParams(start_mode=StrategyMode.ALL)),
        "user_turn_strategies_params other than the defaults",
    ),
    "summaries of the context": (
        around(assistant=LLMAssistantAggregatorParams(enable_auto_context_summarization=True)),
        "summarizes the context with a model call",
    ),
}


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_a_pipeline_earful_cannot_run_or_describe_is_refused_in_one_line(name):
    pipeline, why = REFUSED[name]
    with pytest.raises(ValueError, match=why) as refused:
        pc.run(menu(pipeline=pipeline))
    assert "\n" not in str(refused.value)


def test_the_scripted_stt_is_a_service_and_the_scripted_output_a_transport():
    """So that the refusals above stand for any service and any transport."""
    from pipecat.services.ai_service import AIService
    from pipecat.transports.base_output import BaseOutputTransport

    assert issubclass(ScriptedSTT, AIService) and issubclass(StampedTTS, AIService)
    assert issubclass(ClockedOutput, BaseOutputTransport)


class Threaded(FrameProcessor):
    """Does a piece of work in a thread when the pipeline starts."""

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            worker = threading.Thread(target=lambda: None)
            worker.start()
            worker.join()
        await self.push_frame(frame, direction)


def test_a_processor_that_starts_a_thread_makes_the_run_invalid():
    with pytest.raises(pc.InvalidRun, match=r"work left the virtual clock \(threads started: 1\)"):
        pc.run(menu(pipeline=around(after_llm=[Threaded])))


class Swaps(FrameProcessor):
    """Gives the user aggregator other strategies once the pipeline has started."""

    def __init__(self, pipeline):
        super().__init__()
        self._pipeline = pipeline

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if isinstance(frame, StartFrame):
            user = self._pipeline[2]
            await user._user_turn_controller.update_strategies(turns(speech_timeout=0.1))


def test_strategies_changed_during_the_run_make_it_invalid():
    def pipeline(ends):
        built = around()(ends)
        return [*built, Swaps(built)]

    with pytest.raises(pc.InvalidRun, match="changed its turn strategies during the run"):
        pc.run(menu(pipeline=pipeline))


class Shouts(FrameProcessor):
    """Changes the model's words on their way to the voice."""

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMTextFrame):
            frame.text = frame.text.upper()
        await self.push_frame(frame, direction)


def test_words_a_processor_changed_belong_to_no_reply_and_are_not_judged_as_one():
    result = pc.run(menu(pipeline=around(after_llm=[Shouts], empty_user_turn=None)))
    assert [said["attribution"] for said in events(result, "tts.synthesized")] == ["none", "none"]
    outcomes = result.report.outcomes()
    assert outcomes["M1"] == outcomes["L1"] == "unobservable"


def menu_with_a_listener():
    return menu(pipeline=around(after_llm=[lambda: Heard(LLMTextFrame, [])], empty_user_turn=None))


def test_a_case_with_its_own_pipeline_is_searched_and_built_again_from_its_data():
    from earful.pipecat import scenario

    found = pc.explore(menu_with_a_listener, variables=("llm.ttft",), faults=(), words=False)
    assert found.tried and all(t.outcome.ran for t in found.tried)
    data = scenario.describe(menu_with_a_listener(), "test_pipecat_pipeline:menu_with_a_listener")
    assert scenario.build(data).pipeline is not None
    assert pc.run(menu_with_a_listener, data=data).hash == pc.run(menu_with_a_listener()).hash
    kept = menu_with_a_listener()
    assert pc.run(kept).hash == pc.run(kept).hash  # the pipeline is built for each run


@pytest.mark.parametrize("on_frame", [True, False])
def test_a_manual_summary_with_its_own_model_is_refused_before_inference(on_frame):
    from pipecat.frames.frames import LLMSummarizeContextFrame
    from pipecat.services.llm_service import LLMService
    from pipecat.utils.context.llm_context_summarization import (
        LLMAutoContextSummarizationConfig,
        LLMContextSummaryConfig,
    )

    invoked = []

    class ForeignModel(LLMService):
        async def run_inference(self, *args, **kwargs):
            invoked.append(True)
            return "unreachable"

    class Request(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)
            if isinstance(frame, StartFrame):
                await self.push_frame(
                    LLMSummarizeContextFrame(config=foreign if on_frame else None)
                )

    foreign = LLMContextSummaryConfig(llm=ForeignModel())
    assistant = LLMAssistantAggregatorParams(
        auto_context_summarization_config=LLMAutoContextSummarizationConfig(
            summary_config=LLMContextSummaryConfig() if on_frame else foreign
        )
    )
    pipeline = around(after_output=[Request], assistant=assistant)
    with pytest.raises(ValueError, match="LLMSummarizeContextFrame uses its own summary model"):
        pc.run(menu(pipeline=pipeline))
    assert not invoked


def test_a_function_advertised_with_another_handler_is_refused_before_either_runs():
    from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
    from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema

    invoked = []

    async def placeholder(params):
        invoked.append("placeholder")

    async def real(params):
        invoked.append("real")

    def bot(llm):
        llm.register_function("book", placeholder)
        tool = FunctionSchema(
            name="book", description="Book", properties={}, required=[], handler=real
        )
        return LLMContext(tools=ToolsSchema(standard_tools=[tool]))

    with pytest.raises(ValueError, match="different handler"):
        pc.run(replace(menu(), bot=bot, policy=pc.ScriptedModel().otherwise(call="book")))
    assert not invoked


def test_a_catch_all_effect_is_refused_before_the_bot_runs():
    with pytest.raises(ValueError, match="catch-all effect"):
        pc.run(replace(menu(), effects={"*": pc.Effect("anything")}))
