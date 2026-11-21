"""Provider delivery times and the evidence left by interrupted streaming replies."""

import asyncio

import pytest

pytest.importorskip("livekit.agents")

import livekit.agents as lk
from livekit.agents import llm

from earful.core.ledger import Ledger
from earful.core.schema import validate
from earful.livekit import providers
from earful.livekit.cases import CASES, LONG_REPLY
from earful.livekit.harness import Case, run_case
from earful.livekit.providers import (
    Reply,
    ReplyRegister,
    ScriptedLLM,
    StampedTTS,
    ToolCall,
    UserSpeech,
)
from earful.scripted import clock
from earful.scripted.clock import now_us, run_virtual


@pytest.mark.parametrize("ttft,duration", [(0.1, 0.4), (0, 0), (0.4, 0.1)])
def test_model_chunks_are_paced_and_tools_arrive_at_generation_end(ttft, duration):
    async def main():
        ledger = Ledger(now_us, run="test")
        model = ScriptedLLM(
            ledger,
            lambda items: Reply(
                text="hello world!",
                ttft=ttft,
                duration=duration,
                tool_calls=(ToolCall("lookup", "{}"),),
            ),
            [],
        )
        context = llm.ChatContext()
        context.add_message(role="user", content="Hello")
        chunks = []
        async with model.chat(chat_ctx=context) as stream:
            async for chunk in stream:
                chunks.append((now_us(), chunk.delta.content, chunk.delta.tool_calls))
        await model.aclose()
        return chunks

    chunks = run_virtual(main)
    first, last = round(ttft * 1_000_000), round(max(duration, ttft) * 1_000_000)
    assert [(at, text) for at, text, _ in chunks[:-1]] == [
        (first + (last - first) * n // 3, text)
        for n, text in enumerate(("hel", "lo ", "wor", "ld!"))
    ]
    assert chunks[-1][0] == last and chunks[-1][2][0].name == "lookup"


def test_voice_emits_complete_words_before_input_ends():
    async def main():
        ledger = Ledger(now_us, run="test")
        register = ReplyRegister(replies={1: ["hello", "world."]})
        voice = StampedTTS(ledger, register)
        stream = voice.stream()
        frames = []

        async def consume():
            async for audio in stream:
                frames.append((now_us(), audio.frame))

        consumer = asyncio.create_task(consume())
        stream.push_text("hel")
        await asyncio.sleep(0.1)
        assert not frames and not voice.table.words
        stream.push_text("lo ")
        await asyncio.sleep(0.3)
        assert frames and frames[0][0] == 300_000
        assert not ledger.of(kind="tts.synthesized")  # the snapshot is not complete yet
        stream.push_text("wor")
        await asyncio.sleep(0.2)
        assert [w["text"] for w in voice.table.words.values()] == ["hello"]
        stream.push_text("ld.")
        stream.end_input()
        await consumer
        await stream.aclose()
        await voice.aclose()
        return ledger.events, voice.table.words, frames

    events, words, frames = run_virtual(main)
    (event,) = events
    assert event["kind"] == "tts.synthesized"
    assert event["data"]["text"] == "hello world."
    assert event["data"]["started_us"] == 100_000
    assert event["data"]["call"] == 1
    assert [word["text"] for word in words.values()] == ["hello", "world."]
    assert frames[0][0] < event["t_us"]


@pytest.mark.parametrize("cancel_at", [0.1, 0.4])
def test_cancelled_voice_records_one_prefix_and_drops_the_incomplete_word(cancel_at):
    async def main():
        ledger = Ledger(now_us, run="test")
        register = ReplyRegister(replies={1: ["First", "unfinished"]})
        voice = StampedTTS(ledger, register, ttfb=0.2)
        stream = voice.stream()
        frames = []

        async def consume():
            async for audio in stream:
                frames.append(audio.frame)

        consumer = asyncio.create_task(consume())
        stream.push_text("First unfinished")
        await asyncio.sleep(cancel_at)
        await stream.aclose()
        await consumer
        await voice.aclose()
        return ledger.events, frames

    (event,), frames = run_virtual(main)
    assert event["data"]["text"] == "First"
    assert event["data"]["attribution"] == "prefix" and event["data"]["call"] == 1
    assert len(event["data"]["stamps"]) == 1
    assert bool(frames) == (cancel_at > 0.2)


def _slow_case(**kwargs):
    return Case(
        name="streaming",
        agent=lambda: lk.Agent(instructions="Answer briefly."),
        speeches=[UserSpeech(0.5, 1.5, "Please speak.")],
        policy=lambda items: Reply(text="I a I a I a I a.", duration=4),
        **kwargs,
    )


def test_playout_overlaps_generation_and_gaps_preserve_word_times():
    result = run_case(_slow_case())
    events = result.ledger.events
    assert validate(events) == [] and result.underruns == 0
    response = next(e for e in events if e["kind"] == "llm.response")
    started = next(e for e in events if e["kind"] == "playout.started")
    (synthesized,) = [e for e in events if e["kind"] == "tts.synthesized"]
    assert started["t_us"] < response["t_us"] + response["data"]["duration_us"]
    assert started["seq"] < synthesized["seq"]
    assert len([e for e in events if e["kind"] == "playout.started"]) == 1
    assert any(e["kind"] == "playout.paused" for e in events)
    assert synthesized["data"]["call"] == response["data"]["call"]
    pauses = [e for e in events if e["kind"] == "playout.paused"]
    assert pauses  # generation is slower than the voice; silence is measured explicitly
    finished = next(e for e in events if e["kind"] == "playout.finished")["data"]
    assert finished["words_played"] == synthesized["data"]["stamps"]
    assert finished["words_completed_us"][-1] > finished["words_completed_us"][0] + 2_000_000


def test_hangup_during_generation_keeps_a_single_partial_utterance():
    result = run_case(_slow_case(hangup=3.0))
    events = result.ledger.events
    assert validate(events) == []
    (synthesized,) = [e for e in events if e["kind"] == "tts.synthesized"]
    data = synthesized["data"]
    assert data["call"] == 1 and data["attribution"] == "prefix"
    assert 0 < len(data["stamps"]) < 8
    assert synthesized["t_us"] >= data["started_us"]


def test_what_is_remembered_of_a_cut_reply_does_not_depend_on_the_words_said_before_it():
    def remembered(opening):
        def policy(items):
            said = [i["text"] for i in items if i["type"] == "message" and i["role"] == "user"]
            if "menu" in said[-1]:
                return Reply(text=LONG_REPLY, ttft=0.1, duration=0.4)
            return Reply(text="Okay." if "Stop" in said[-1] else opening, ttft=0.1, duration=0.4)

        case = Case(
            name="long",
            agent=lambda: lk.Agent(instructions="Answer."),
            speeches=[
                UserSpeech(0.5, 1.5, "Hello there."),
                UserSpeech(60.0, 62.0, "Tell me about the menu."),
                UserSpeech(64.5, 65.5, "Stop."),
            ],
            policy=policy,
        )
        result = run_case(case)
        result.assert_contracts()
        history = next(e for e in result.ledger.events if e["kind"] == "history.final")
        return [
            item["text"]
            for item in history["data"]["items"]
            if item["type"] == "message" and item["interrupted"]
        ]

    few, many = remembered("Hello to you."), remembered(" ".join([LONG_REPLY] * 6))
    assert few == many and len(few) == 1


def _a_bug(*args, **kwargs):
    raise KeyError("a bug in Earful")


@pytest.mark.parametrize(
    "owner, method, provider",
    [
        (providers._ScriptedVADStream, "_emit", "VAD"),
        (providers._ScriptedRecognizeStream, "_send", "recognizer"),
        (providers._ScriptedLLMStream, "_chunk", "model"),
        (providers._Synthesis, "render", "voice"),
    ],
)
def test_an_exception_inside_a_scripted_provider_makes_the_run_invalid(
    monkeypatch, owner, method, provider
):
    """The framework takes it for a provider that failed, and carries on."""
    monkeypatch.setattr(owner, method, _a_bug)
    monkeypatch.setattr(clock, "STALL_SECONDS", 0.2)  # a run left waiting fails here, and soon
    result = run_case(CASES["barge_in_mid_word"](), require_valid=False)
    assert result.invalid == [
        f"harness error: the scripted {provider} raised KeyError: 'a bug in Earful'"
    ]


def test_an_exception_while_a_whole_text_is_synthesized_is_recorded(monkeypatch):
    async def main():
        errors = []
        token = providers.record_errors_in(errors)
        voice = StampedTTS(Ledger(now_us, run="test"), ReplyRegister())
        monkeypatch.setattr(providers._Synthesis, "render", _a_bug)
        with pytest.raises(Exception, match="a bug in Earful|failed"):
            async for _ in voice.synthesize("hello"):
                pass
        providers.stop_recording_errors(token)
        await voice.aclose()
        return errors

    assert run_virtual(main) == ["the scripted voice raised KeyError: 'a bug in Earful'"]


def test_a_policy_that_raises_is_recorded_once_and_as_the_policy():
    case = _slow_case()
    case.policy = _a_bug
    result = run_case(case, require_valid=False)
    assert result.invalid == ["harness error: policy raised on call 1: KeyError: 'a bug in Earful'"]
