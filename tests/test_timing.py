"""Scripted times decide framework thresholds exactly: no float rounding in between."""

import asyncio

import pytest

pytest.importorskip("livekit.agents")

import livekit.agents as lk
from livekit import rtc
from livekit.agents import vad as lk_vad

from earful.core.backend import Effect
from earful.core.ledger import Ledger
from earful.core.search import anchors
from earful.core.timebase import to_us
from earful.livekit.cases import LONG_REPLY, BookingAgent, Reservations, _barge_in_policy
from earful.livekit.harness import Case, run_case
from earful.livekit.providers import Reply, ToolCall, UserSpeech, WorldOrigin
from earful.livekit.user import ScriptedVAD
from earful.scripted.clock import now_us, run_virtual


# At 3.6 and 3.85, float subtraction puts a 0.5 s speech just under the 0.5 s threshold.
@pytest.mark.parametrize("start", [3.5, 3.6, 3.85, 4.1, 4.35, 5.0, 5.3])
def test_interruption_lands_exactly_on_the_declared_threshold(start):
    case = Case(
        name="threshold",
        agent=lambda: lk.Agent(instructions="You are a restaurant host."),
        speeches=[
            UserSpeech(start=0.5, end=2.5, transcript="Tell me about the menu."),
            UserSpeech(start=start, end=start + 1.0, transcript="Stop."),
        ],
        policy=_barge_in_policy,
    )
    events = run_case(case).ledger.events
    origin = next(e for e in events if e["kind"] == "user.origin")["t_us"]
    session = events[0]["data"]["scenario"]["session"]
    command = next(e for e in events if e["kind"] in ("sink.pause", "sink.clear"))
    assert command["t_us"] == origin + to_us(start) + session["min_interruption_duration_us"]


def _at(events, kind):
    origin = next(e for e in events if e["kind"] == "user.origin")["t_us"]
    return [e["t_us"] - origin for e in events if e["kind"] == kind]


def test_the_scripted_vad_reports_what_the_silero_plugin_reports():
    """Two speeches 0.3 s apart are one segment. Speech is counted until the end is reported."""

    async def main():
        ledger = Ledger(now_us, run="test")
        speeches = [UserSpeech(1.0, 1.2, "Tell me"), UserSpeech(1.5, 1.6, "more.")]
        vad = ScriptedVAD(ledger, speeches, WorldOrigin(ledger), min_speech_duration=0.1)
        stream = vad.stream()
        silence = bytes(2 * 240)

        async def microphone():  # a VAD reports only while audio reaches it
            while True:
                stream.push_frame(
                    rtc.AudioFrame(
                        silence, sample_rate=24000, num_channels=1, samples_per_channel=240
                    )
                )
                await asyncio.sleep(0.01)

        feeding = asyncio.ensure_future(microphone())
        reports = []
        async for ev in stream:
            reports.append(
                (
                    ev.type.name,
                    to_us(ev.timestamp),
                    to_us(ev.speech_duration),
                    to_us(ev.silence_duration),
                    to_us(ev.raw_accumulated_speech),
                    to_us(ev.raw_accumulated_silence),
                    ev.speaking,
                )
            )
            if ev.type is lk_vad.VADEventType.END_OF_SPEECH:
                break
        feeding.cancel()
        await stream.aclose()
        return reports

    assert run_virtual(main) == [
        ("START_OF_SPEECH", 1_100_000, 100_000, 0, 100_000, 0, True),
        ("INFERENCE_DONE", 1_200_000, 200_000, 0, 200_000, 0, True),
        ("INFERENCE_DONE", 1_300_000, 300_000, 0, 0, 100_000, True),  # the pause
        ("INFERENCE_DONE", 1_400_000, 400_000, 0, 0, 200_000, True),
        ("INFERENCE_DONE", 1_500_000, 500_000, 0, 0, 0, True),  # the second speech starts
        ("INFERENCE_DONE", 1_600_000, 600_000, 0, 100_000, 0, True),
        ("INFERENCE_DONE", 1_700_000, 700_000, 0, 0, 100_000, True),  # silence, still counted
        ("INFERENCE_DONE", 1_800_000, 800_000, 0, 0, 200_000, True),
        ("INFERENCE_DONE", 1_900_000, 900_000, 0, 0, 300_000, True),
        ("INFERENCE_DONE", 2_000_000, 1_000_000, 0, 0, 400_000, True),
        ("END_OF_SPEECH", 2_100_000, 600_000, 500_000, 0, 500_000, False),
    ]


def test_a_reply_that_comes_due_while_the_user_speaks_waits_for_them():
    def policy(items):
        if any(item["type"] == "function_call_output" for item in items):
            return Reply(text="Your table is booked for tonight at seven.")
        return Reply(
            text="One moment.", tool_calls=(ToolCall("create_reservation", '{"party_size": 6}'),)
        )

    case = Case(
        name="due",
        agent=lambda: BookingAgent(Reservations()),
        speeches=[
            UserSpeech(0.5, 2.5, "Book a table for six tonight."),
            UserSpeech(4.9, 5.8, ""),  # under way when the tool returns at 5.0 s
        ],
        effects={"create_reservation": Effect("book", latency=2.0)},
        policy=policy,
        min_interruption_duration=1.0,
    )
    result = run_case(case)
    result.assert_contracts()
    events = result.ledger.events
    assert _at(events, "tool.returned") == [5_000_000]
    assert not [t for t in _at(events, "playout.started") if 4_900_000 <= t <= 5_800_000]


def test_a_speech_shorter_than_the_threshold_interrupts_once_the_vad_has_counted_that_long():
    case = Case(
        name="short",
        agent=lambda: lk.Agent(instructions="You are a restaurant host."),
        speeches=[
            UserSpeech(0.5, 1.5, "Tell me about the menu."),
            UserSpeech(4.0, 4.3, ""),  # 0.3 s of noise; the VAD reports its end at 4.8 s
        ],
        policy=lambda items: Reply(text=LONG_REPLY, ttft=0.1, duration=0.4),
    )
    result = run_case(case)
    result.assert_contracts()
    assert _at(result.ledger.events, "sink.pause") == [4_500_000]


@pytest.mark.parametrize(
    "silence, delay", [(0.5, 0.5), (0.5, 0.8), (0.5, 0.2), (0.3, 1.0), (0.8, 0.3)]
)
def test_the_turn_ends_where_the_header_says_a_search_should_aim(silence, delay):
    """The session commits the user's turn once the VAD's silence and the endpointing delay
    have both passed. It counts the delay as a float, so it can be microseconds early."""
    case = Case(
        name="turn end",
        agent=lambda: lk.Agent(instructions="You are a host."),
        speeches=[UserSpeech(start=0.5, end=1.5, transcript="Hello there.")],
        policy=lambda items: Reply(text="Good morning."),
        vad_min_silence=silence,
        min_endpointing_delay=delay,
    )
    events = run_case(case).events
    header = events[0]["data"]["scenario"]
    assert header["session"]["turn_end_delay_us"] == to_us(max(silence, delay))
    origin = next(e["t_us"] for e in events if e["kind"] == "user.origin")
    aimed = origin + 500_000 + anchors(header, header["speeches"][0])["turn_end"]
    (committed,) = [
        e["t_us"]
        for e in events
        if e["kind"] == "fw.conversation_item_added" and e["data"]["item"].get("role") == "user"
    ]
    assert 0 <= aimed - committed <= 10
