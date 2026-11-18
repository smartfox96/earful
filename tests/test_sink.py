"""The clocked sink on its own: sample accounting, validity counters, pause semantics."""

import asyncio
from array import array

from livekit import rtc

from earful.core.ledger import Ledger
from earful.livekit.sink import ClockedSink
from earful.scripted.clock import now_us, run_virtual
from earful.scripted.voice import StampTable, encode

RATE = 24000


def _frame(*runs, rate=RATE):
    """An audio frame made of (stamp, sample count) runs, each the start of that word."""
    pcm = array("h")
    for stamp, count in runs:
        pcm.extend(encode(stamp, count))
    return rtc.AudioFrame(
        data=pcm.tobytes(), sample_rate=rate, num_channels=1, samples_per_channel=len(pcm)
    )


def _sink(words, *, rate=RATE):
    """A sink and its ledger, with ``words`` (sample counts) registered as stamps 1, 2, ..."""
    ledger = Ledger(now_us, run="test")
    table = StampTable(sample_rate=rate)
    for index, samples in enumerate(words):
        table.add(1, index, f"w{index + 1}", samples)
    return ClockedSink(ledger, table), ledger


def _kinds(ledger):
    return [e["kind"] for e in ledger.events]


def test_a_sample_of_no_known_word_is_a_stamp_error():
    async def main():
        sink, _ = _sink([2400])
        await sink.capture_frame(_frame((1, 2399), (99, 1)))  # word 99 was never synthesized
        sink.flush()
        await asyncio.sleep(0.2)
        return sink.stamp_errors

    assert run_virtual(main) == 1


def test_a_change_of_sample_rate_mid_segment_is_a_stamp_error():
    async def main():
        sink, _ = _sink([2400])
        await sink.capture_frame(_frame((1, 1200)))
        await sink.capture_frame(_frame((1, 1200), rate=16000))
        return sink.stamp_errors

    assert run_virtual(main) == 1


def test_streaming_gaps_pause_playout_without_moving_word_completion_times():
    async def main():
        sink, ledger = _sink([2400, 2400, 2400])
        await sink.capture_frame(_frame((1, 2400)))  # 0.1 s of audio
        await asyncio.sleep(0.1)
        await sink.capture_frame(_frame((2, 2400)))  # exactly on time
        await asyncio.sleep(0.3)
        await sink.capture_frame(_frame((3, 2400)))  # playback was silent for 0.2 s
        sink.flush()
        await asyncio.sleep(0.1)
        return sink.underruns, ledger.events

    underruns, events = run_virtual(main)
    assert underruns == 0
    done = events[-1]
    assert done["kind"] == "playout.finished"
    assert done["data"]["words_played"] == [1, 2, 3]
    assert done["data"]["words_completed_us"] == [100_000, 200_000, 500_000]
    assert len([e for e in events if e["kind"] == "playout.started"]) == 1


def test_a_cleared_output_stays_paused_until_it_is_resumed():
    """Pause belongs to the output, as in the framework's room output."""

    async def main():
        sink, ledger = _sink([2400, 2400])
        await sink.capture_frame(_frame((1, 2400)))
        await asyncio.sleep(0.05)
        sink.pause()
        sink.pause()  # the framework repeats itself; nothing new to record
        sink.clear_buffer()
        await sink.capture_frame(_frame((2, 2400)))
        await asyncio.sleep(1.0)
        waiting = _kinds(ledger)
        sink.resume()
        return waiting, ledger.events

    waiting, events = run_virtual(main)
    assert waiting == [
        "playout.started",
        "sink.pause",
        "playout.paused",
        "sink.clear",
        "playout.finished",
    ]
    assert [e["kind"] for e in events[5:]] == ["sink.resume", "playout.started"]
    assert events[-1]["data"]["segment"] == 2
    assert events[-1]["t_us"] == 1_050_000


def test_a_sample_counts_once_it_has_played_in_full():
    """At 44.1 kHz a sample lasts 22.68 us, so a 100-sample word is complete at 2268 us.
    One microsecond earlier it is cut, with 99 samples consumed."""

    async def run(clear_after):
        sink, ledger = _sink([100, 341], rate=44100)
        await sink.capture_frame(_frame((1, 100), (2, 341), rate=44100))
        await asyncio.sleep(clear_after)
        sink.clear_buffer()
        return ledger.events[-1]["data"]

    before = run_virtual(lambda: run(0.002267))
    assert before["words_played"] == [] and before["word_cut"] == 1
    assert before["word_cut_samples"] == 99
    at = run_virtual(lambda: run(0.002268))
    assert at["words_played"] == [1] and at["words_completed_us"] == [2268]
    assert at["word_cut"] is None  # the next sample has not finished playing
