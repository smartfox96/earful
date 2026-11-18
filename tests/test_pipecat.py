"""Integration tests for the Pipecat adapter, also run in a Pipecat-only environment."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pipecat")

from loguru import logger
from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import CancelFrame, FunctionCallResultProperties, OutputAudioRawFrame
from pipecat.processors.aggregators.llm_context import LLMContext

from earful import pipecat as pc
from earful.cli import main
from earful.core import bundle, case, schema
from earful.core.ledger import Ledger
from earful.core.search import evaluate
from earful.pipecat import scenario
from earful.pipecat.sink import ClockedOutput
from earful.pipecat.tools import ToolWitness
from earful.scripted.clock import now_us, run_virtual
from earful.scripted.voice import StampTable


def greeting():
    return pc.Case(
        "greeting",
        lambda llm: LLMContext(),
        [pc.UserSpeech(0.25, 0.75, "Hello")],
        pc.ScriptedModel().otherwise(say="Good morning. How are you?"),
        seconds_per_letter=0.04,
        tts_word_timestamps=True,
    )


def booking():
    def bot(llm):
        async def book(params):
            raise AssertionError("a declared effect replaces this service call")

        llm.register_function("book", book)
        return LLMContext()

    model = (
        pc.ScriptedModel()
        .when(pc.tool_succeeded(), say="Your table is booked.", claims=["table"])
        .when(pc.tool_failed(), call=("book", {"seats": 4}))
        .otherwise(call=("book", {"seats": 4}))
    )
    return pc.Case(
        "booking",
        bot,
        [pc.UserSpeech(0.25, 0.75, "A table for four.")],
        model,
        effects={"book": pc.Effect("table")},
        backend_mode="keyed",
    )


def events(result, kind):
    return [e["data"] for e in result.events if e["kind"] == kind]


def test_pipecat_fixture_builds_once_and_plays_a_whole_reply_as_one_utterance(earful):
    made = []

    def make():
        made.append(1)
        return greeting()

    result = earful.run(make, seed=14)
    assert made == [1]
    result.assert_contracts()
    assert schema.validate(result.events) == []
    assert result.hash == pc.run(greeting(), seed=14).hash
    assert result.events[0]["data"]["framework"]["name"] == "pipecat-ai"
    assert result.events[-1]["data"]["executor_calls"] == 0
    assert events(result, "user.transcript_final") == [{"speech": 0, "text": "Hello"}]
    (utterance,) = events(result, "tts.synthesized")
    assert utterance["attribution"] == "unique" and utterance["call"] == 1
    assert utterance["text"] == "Good morning. How are you?"
    (played,) = events(result, "playout.finished")
    assert played["words_played"] == utterance["stamps"]
    assert played["text_played"] == utterance["text"]
    assert played["position_us"] == sum(utterance["word_samples"]) * 1_000_000 // 24000


def test_pipecat_booking_effect_is_linked_to_its_callback_and_keyed_retry():
    built = booking()
    result = pc.run(built)
    result.assert_contracts(require=["A2", "A3"])
    with pytest.raises(pc.ContractFailure, match=r"booking \(seed 0, pipecat-ai .*required A1\n"):
        result.assert_contracts(require=["A1"])
    with pytest.raises(ValueError, match="no contract rule 'A2'"):
        result.assert_contracts(allow=["A2"])
    header = result.events[0]["data"]["scenario"]
    assert header["effects"] == [{"tool": "book", "intent": "table"}]
    assert header["intents"] == [{"intent": "table", "tools": ["book"], "authorized": 1}]
    (called,) = events(result, "tool.called")
    (returned,) = events(result, "tool.returned")
    (requested,) = events(result, "effect.requested")
    assert called["arguments"] == requested["payload"] == {"seats": 4}
    assert requested["tool"] == called["tool"] == returned["tool"]
    assert returned["output"] == "R1" and returned["text"] == '"R1"'

    built.faults = [pc.Fault("backend.timeout", 1, 0.02)]
    with pytest.raises(pc.ContractFailure, match="A2/rule"):
        pc.run(built).assert_contracts()
    built.effects["book"] = pc.Effect("table", key=lambda args: f"table-for-{args['seats']}")
    fixed = pc.run(built)
    fixed.assert_contracts()
    assert len(events(fixed, "effect.deduplicated")) == 1


def test_pipecat_transcripts_arrive_at_their_own_deadlines():
    built = greeting()
    built.speeches = [
        pc.UserSpeech(0.1, 0.6, "First", stt_delay=2),
        pc.UserSpeech(0.8, 1.3, "Second", stt_delay=0.1),
    ]
    result = pc.run(built)
    origin = next(e["t_us"] for e in result.events if e["kind"] == "user.origin")
    assert [
        (e["data"]["speech"], e["t_us"] - origin)
        for e in result.events
        if e["kind"] == "user.transcript_final"
    ] == [(1, 1_400_000), (0, 2_600_000)]


def test_pipecat_fixture_accepts_a_prepared_scenario(earful):
    prepared = pc.Scenario.of(greeting)
    assert earful.run(prepared).hash == pc.run(greeting).hash


def test_pipecat_case_settings_and_delays_survive_serialization():
    built = greeting()
    built.delays = [pc.Delay("llm.ttft", 1, 0.7), pc.Delay("tts.ttfb", 1, 0.4)]
    data = scenario.describe(built, "test_pipecat:greeting", seed=21)
    assert case.validate(data) == []
    assert data["settings"]["tts_word_timestamps"] is True
    assert data["settings"]["vad_start_us"] == 200_000
    assert scenario.describe(scenario.build(data), data["scenario"], 21) == data
    result = pc.run(greeting, case=data)
    assert result.hash == pc.run(built, seed=21).hash
    assert events(result, "llm.response")[0]["ttft_us"] == 700_000
    assert events(result, "tts.synthesized")[0]["ttfb_us"] == 400_000
    data["settings"]["tts_word_timestamps"] = 1
    with pytest.raises(ValueError, match="takes a bool"):
        scenario.build(data)
    refused = evaluate(pc.Scenario.of(greeting).run, data)
    assert refused.why_not == "refused" and "takes a bool" in refused.refused


def test_pipecat_rejects_unknown_tools_and_retired_intents():
    built = booking()
    built.effects = {"misspelled": pc.Effect("table")}
    with pytest.raises(ValueError, match="unregistered.*misspelled"):
        pc.run(built)
    built = greeting()
    built.speeches = [replace(built.speeches[0], retires=("unknown",))]
    with pytest.raises(ValueError, match="retires undeclared"):
        pc.run(built)


def test_pipecat_waits_for_the_final_turn_and_slow_tool():
    built = booking()
    built.drain_delay = 0
    built.user_speech_timeout = 2.2
    built.delays = [pc.Delay("backend.latency", 1, 3.0)]
    result = pc.run(built)
    assert len(events(result, "effect.committed")) == len(events(result, "tool.returned")) == 1
    result.assert_contracts()


def test_pipecat_a_stalled_input_yields_invalid_evidence(monkeypatch):
    from earful.pipecat.providers import ScriptedSTT

    async def late(stt):
        await asyncio.sleep(100)
        stt.done.set_result(None)

    monkeypatch.setattr(ScriptedSTT, "_script", late)
    built = greeting()
    built.drain_timeout = 2
    with pytest.raises(pc.InvalidRun, match="scripted input did not complete") as caught:
        pc.run(built)
    assert caught.value.result.events[-1]["t_us"] < 4_000_000
    assert not caught.value.result.report.valid


def test_pipecat_hangup_stops_the_user_and_settles_a_pending_write():
    built = booking()
    built.hangup = 3.0
    built.delays = [pc.Delay("backend.latency", 1, 10)]
    result = pc.run(built)
    kinds = [e["kind"] for e in result.events]
    assert (
        kinds.index("effect.requested")
        < kinds.index("user.hangup")
        < kinds.index("effect.committed")
    )
    assert events(result, "tool.cancelled")
    built = greeting()
    built.hangup = built.speeches[-1].end
    result = pc.run(built)
    assert events(result, "user.hangup") and not events(result, "user.transcript_final")


def test_pipecat_policy_errors_leave_invalid_evidence(earful):
    built = greeting()
    built.policy = pc.ScriptedModel()
    with pytest.raises(pc.InvalidRun, match="policy raised") as caught:
        earful.run(built)
    assert earful.runs == [caught.value.result]
    assert not caught.value.result.report.valid


def test_pipecat_cancellation_counts_only_the_samples_that_played():
    async def play():
        table = StampTable(sample_rate=24000)
        table.add(1, 0, "hi", 240)
        table.add(1, 1, "there", 240)
        ledger = Ledger(now_us, run="test")
        output = ClockedOutput(ledger, table)
        frame = OutputAudioRawFrame((table.audio(1) + table.audio(2)).tobytes(), 24000, 1)
        writing = asyncio.create_task(output.write_audio_frame(frame))
        await asyncio.sleep(0.015)
        writing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await writing
        await output.cancel(CancelFrame())
        return ledger.events[-1]["data"]

    data = run_virtual(play)
    assert data["interrupted"] and data["position_us"] == 15_000 and data["pushed_us"] == 20_000
    assert data["words_played"] == [1] and data["words_completed_us"] == [10_000]
    assert data["word_cut"] == 2 and data["word_cut_samples"] == 120


def test_pipecat_playout_preserves_gaps_between_chunks():
    async def play():
        table = StampTable(sample_rate=24000)
        ledger = Ledger(now_us, run="test")
        output = ClockedOutput(ledger, table)
        for index, word in enumerate(("hi", "there")):
            stamp = table.add(1, index, word, 240)
            if index:
                await asyncio.sleep(0.005)
            frame = OutputAudioRawFrame(table.audio(stamp).tobytes(), 24000, 1)
            await output.write_audio_frame(frame)
        await output.cancel(CancelFrame())
        return ledger.events[-1]["data"]

    data = run_virtual(play)
    assert data["words_completed_us"] == [10_000, 25_000]
    assert data["position_us"] == 20_000


def test_pipecat_witness_forwards_intermediate_callbacks_and_records_only_the_final_result():
    from pipecat.services.llm_service import FunctionCallParams

    async def tool(params):
        await params.result_callback(
            "pending", properties=FunctionCallResultProperties(is_final=False)
        )
        await params.result_callback("café", properties=FunctionCallResultProperties(run_llm=False))

    async def invoke():
        ledger = Ledger(now_us, run="test")
        callback = AsyncMock()
        params = FunctionCallParams("work", "call1", {}, None, None, LLMContext(), callback)
        await ToolWitness(ledger, {}).wrap(tool)(params)
        assert [call.args[0] for call in callback.await_args_list] == ["pending", "café"]
        return [e["data"] for e in ledger.events if e["kind"] == "tool.returned"]

    (result,) = asyncio.run(invoke())
    assert result["text"] == '"café"'
    assert result["output"] == "café" and result["expects_reply"] is False


@pytest.mark.slow
def test_pipecat_search_keeps_and_replays_a_booking_bundle(earful, tmp_path, capsys, monkeypatch):
    found = earful.explore(
        booking, variables=(), faults=("backend.timeout",), words=False, workers=0
    )
    kept = earful.keep(found, "A2/rule", tmp_path / "kept")
    assert kept.manifest["framework"]["name"] == "pipecat-ai"
    loads = []
    load = bundle.load

    def recorded_load(path):
        loads.append(path)
        return load(path)

    monkeypatch.setattr(bundle, "load", recorded_load)
    replayed = earful.replay(tmp_path / "kept", repeats=2)
    assert replayed.reproduced and replayed.identical and replayed.repeatable
    assert main(["replay", str(tmp_path / "kept"), "--expect", "reproduced", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["identical_to_recorded"]
    assert loads == [kept.directory, kept.directory]


@pytest.mark.slow
def test_pipecat_search_from_the_command_line_names_the_kept_bundle_after_pipecat(tmp_path, capsys):
    narrow = ("--variables", "none", "--faults", "backend.timeout", "--no-words", "--workers", "0")
    kept = ("--keep", "A2/rule", "--out", str(tmp_path), "--json")
    assert main(["search", "test_pipecat:booking", *narrow, *kept]) == 1
    (directory,) = tmp_path.iterdir()
    assert directory.name == f"pc-{scenario.environment()['pipecat-ai']}.booking.A2-rule"
    assert json.loads(capsys.readouterr().out)["bundles"]["A2/rule"]["directory"] == str(directory)
    assert bundle.load(directory).manifest["framework"]["name"] == "pipecat-ai"


def test_pipecat_workers_run_an_importable_scenario_in_a_fresh_interpreter(monkeypatch):
    from concurrent.futures import ProcessPoolExecutor

    sent = []
    submit = ProcessPoolExecutor.submit

    def counted(pool, *args, **kwargs):
        sent.append(args)
        return submit(pool, *args, **kwargs)

    monkeypatch.setattr(ProcessPoolExecutor, "submit", counted)
    options = {"variables": ("llm.ttft",), "faults": (), "words": False}
    found = pc.explore("test_pipecat:greeting", workers=1, **options)
    here = pc.explore(greeting, workers=0, **options)
    assert sent and len(found.tried) == len(here.tried) > 0
    assert [t.outcome.hash for t in found.tried] == [t.outcome.hash for t in here.tried]
    assert found.original.hash == pc.run(greeting).hash


@pytest.mark.slow
def test_pipecat_uses_the_real_clock_for_the_control_run():
    built = greeting()
    built.drain_delay = 0
    monotonic = time.monotonic
    checked = pc.cross_check(built, runs=1)
    assert time.monotonic is monotonic
    assert checked.virtual[0]["data"]["clock"] == "virtual"
    assert checked.real[0][0]["data"]["clock"] == "real"
    assert checked.agrees, checked.lines()


@pytest.mark.slow
def test_pipecat_documented_example_runs_from_pytest_and_the_installed_cli(tmp_path):
    page = (Path(__file__).resolve().parents[1] / "docs/pipecat.md").read_text()
    (code,) = re.findall(r"```python\n(.*?)```", page, flags=re.DOTALL)
    (tmp_path / "test_greeting.py").write_text(code)

    def command(*args):
        return subprocess.run(
            [sys.executable, "-m", *args], cwd=tmp_path, capture_output=True, text=True, check=True
        ).stdout

    assert "1 passed" in command("pytest", "-q", "test_greeting.py")
    ledger = tmp_path / "greeting.jsonl"
    command("earful", "run", "test_greeting:greeting", "--ledger", str(ledger))
    assert "pipecat-ai" in command("earful", "report", str(ledger))
    command(
        "earful",
        "search",
        "test_greeting:greeting",
        "--workers",
        "0",
        "--variables",
        "none",
        "--faults",
        "none",
        "--no-words",
    )


def test_pipecat_reads_async_tool_results_from_the_context():
    from pipecat.processors.aggregators import async_tool_messages

    from earful.pipecat.providers import summarize

    messages = [
        {
            "role": "assistant",
            "tool_calls": [{"id": "one", "function": {"name": "book", "arguments": "{}"}}],
        },
        async_tool_messages.build_started_message("one"),
        async_tool_messages.build_intermediate_result_message("one", '"working"'),
        async_tool_messages.build_final_result_message("one", '"café"'),
    ]
    outputs = [i for i in summarize(messages, {}, {}) if i["type"] == "function_call_output"]
    assert len(outputs) == 1
    assert (outputs[0]["name"], outputs[0]["call_id"], outputs[0]["output"]) == (
        "book",
        "one",
        '"café"',
    )


def test_pipecat_recognizes_a_framework_refusal_without_an_application_tool():
    built = greeting()
    built.policy = (
        pc.ScriptedModel()
        .when(pc.tool_failed(), say="I could not do that.")
        .when(pc.tool_returned(), say="That worked.")
        .otherwise(call=("missing", {}))
    )
    result = pc.run(built)
    assert not events(result, "tool.called")
    assert events(result, "llm.response")[-1]["text"] == "I could not do that."


async def _book(params):
    await params.result_callback(await pc.effect("table", dict(params.arguments)))


async def book(params, seats: int):
    """Book a table.

    Args:
        seats: How many people.
    """
    await params.result_callback(await pc.effect("table", {"seats": seats}))


def _registered(llm):
    llm.register_function("book", _book)
    return LLMContext()


def _direct(llm):
    return LLMContext(tools=ToolsSchema(standard_tools=[book]))


def _advertised(llm):
    schema = FunctionSchema(
        name="book",
        description="Book a table.",
        properties={"seats": {"type": "integer"}},
        required=["seats"],
        handler=_book,
    )
    return LLMContext(tools=ToolsSchema(standard_tools=[schema]))


@pytest.mark.parametrize("bot", [_registered, _direct, _advertised])
@pytest.mark.parametrize("declared", [False, True])
def test_pipecat_witnesses_a_tool_however_it_is_registered(bot, declared):
    built = replace(booking(), bot=bot, backend_mode="plain")
    if not declared:
        built.effects, built.intents = {}, [pc.Intent("table")]
    result = pc.run(built)
    result.assert_contracts()
    assert result.events[0]["data"]["scenario"]["witnessed_tools"] == ["book"]
    (called,) = events(result, "tool.called")
    (returned,) = events(result, "tool.returned")
    (requested,) = events(result, "effect.requested")
    assert called["name"] == "book" and called["arguments"] == {"seats": 4}
    assert requested["tool"] == called["tool"] == returned["tool"] and returned["output"] == "R1"
    outcomes = result.report.outcomes()
    assert outcomes["A2"] == outcomes["M2"] == outcomes["L1"] == "pass"


def interruption():
    return pc.Case(
        "interruption",
        lambda llm: LLMContext(),
        [pc.UserSpeech(0.25, 0.75, "What is on the menu?"), pc.UserSpeech(3.0, 4.0, "Stop.")],
        pc.ScriptedModel()
        .when(pc.user_said("Stop"), say="Okay.")
        .otherwise(say="Tonight we have a roasted squash soup to start. Then a grilled sea bass."),
        tts_word_timestamps=True,
    )


def test_pipecat_a_user_who_speaks_over_a_reply_cuts_it_and_the_bot_remembers_what_played():
    result = pc.run(interruption)
    result.assert_contracts()
    assert result.hash == pc.run(interruption).hash
    origin = next(e["t_us"] for e in result.events if e["kind"] == "user.origin")
    cut, after = events(result, "playout.finished")
    assert cut["interrupted"] and cut["text_played"] == "Tonight we have a"
    assert (cut["word_cut"], cut["word_cut_samples"]) == (5, 9600) and not after["interrupted"]
    clears = [e["t_us"] - origin for e in result.events if e["kind"] == "sink.clear"]
    assert clears[-1] == 3_200_000  # the VAD reports the second speech 0.2 s after it starts
    history = events(result, "history.final")[0]["items"]
    replies = [(i["text"], i["interrupted"]) for i in history if i.get("role") == "assistant"]
    assert replies == [("Tonight we have a roasted", True), ("Okay.", False)]
    outcomes = result.report.outcomes()
    assert outcomes["M1"] == outcomes["M2"] == outcomes["L1"] == outcomes["L2"] == "pass"


def test_pipecat_a_reply_spoken_across_two_audio_contexts_is_one_utterance():
    """Pipecat ends an audio context that waits 3 s for its next sentence."""
    built = greeting()
    built.policy = pc.ScriptedModel().otherwise(say="Good morning to you. How are you today?")
    built.delays = [pc.Delay("llm.duration", 1, 8.0)]
    result = pc.run(built)
    result.assert_contracts()
    (utterance,) = events(result, "tts.synthesized")
    assert utterance["text"] == "Good morning to you. How are you today?"
    assert utterance["attribution"] == "unique"
    first, second = events(result, "playout.finished")
    assert first["utterances"] == second["utterances"] == [utterance["utterance"]]
    assert first["words_played"] + second["words_played"] == utterance["stamps"]


def test_pipecat_a_voice_slower_than_pipecat_waits_is_judged():
    """Pipecat reports an audio context that got no audio within 3 s. The run is still
    evidence: what Pipecat said of itself is an event, without the context's random id."""
    built = greeting()
    built.delays = [pc.Delay("tts.ttfb", 1, 3.3)]
    result = pc.run(built)
    assert result.report.valid and result.hash == pc.run(built).hash
    (reported,) = events(result, "fw.error")
    assert reported["error"] == "TTS context # completed with no audio"
    assert result.events[-1]["data"]["framework_warnings"] == 1


def test_pipecat_search_is_quiet():
    logged = []
    sink = logger.add(lambda message: logged.append(message.record["name"]), level="DEBUG")
    try:
        pc.explore(greeting, variables=("llm.ttft",), faults=(), words=False)
        during = [name for name in logged if name.startswith("pipecat")]
        logged.clear()
        pc.run(greeting)
        after = [name for name in logged if name.startswith("pipecat")]
    finally:
        logger.remove(sink)
    assert not during and after
