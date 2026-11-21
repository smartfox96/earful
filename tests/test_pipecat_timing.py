"""Scripted times decide Pipecat's thresholds exactly: no float rounding in between."""

from __future__ import annotations

import pytest

pytest.importorskip("pipecat")

from pipecat.processors.aggregators.llm_context import LLMContext

from earful import pipecat as pc
from earful.core.search import anchors
from earful.core.timebase import to_us
from earful.pipecat.cases import MENU


def _at(result, kind):
    origin = next(e["t_us"] for e in result.events if e["kind"] == "user.origin")
    return [e["t_us"] - origin for e in result.events if e["kind"] == kind]


def _shown(result):
    """The user messages of each model call."""
    requests = [e["data"]["items"] for e in result.events if e["kind"] == "llm.request"]
    return [[i["text"] for i in items if i.get("role") == "user"] for items in requests]


# At 3.6 and 3.85 a float sum of the start and the 0.2 s the VAD waits is not exact.
@pytest.mark.parametrize("start", [3.5, 3.6, 3.85, 4.1, 4.35, 5.0, 5.3])
def test_pipecat_interruption_lands_exactly_on_the_declared_threshold(start):
    case = pc.Case(
        "threshold",
        lambda llm: LLMContext(),
        [
            pc.UserSpeech(0.5, 2.5, "Tell me about the menu."),
            pc.UserSpeech(start, start + 1.0, "Stop."),
        ],
        pc.ScriptedModel().when(pc.user_said("stop"), say="Okay, stopping.").otherwise(say=MENU),
        tts_word_timestamps=True,
    )
    result = pc.run(case)
    result.assert_contracts()
    assert _at(result, "sink.clear")[-1] == to_us(start) + to_us(case.vad_start_secs)
    cut, _ = [e["data"] for e in result.events if e["kind"] == "playout.finished"]
    assert cut["interrupted"]


def _two_speeches(second_us, **settings):
    """The user says "Tell me" until 1.0 s and "about the menu." from ``second_us``."""
    second = second_us / 1_000_000
    return pc.Case(
        "two speeches",
        lambda llm: LLMContext(),
        [
            pc.UserSpeech(0.5, 1.0, "Tell me"),
            pc.UserSpeech(second, second + 0.5, "about the menu."),
        ],
        pc.ScriptedModel().otherwise(say="Good morning."),
        **settings,
    )


@pytest.mark.parametrize("gap_us, segments", [(199_999, 1), (200_000, 2), (200_001, 2)])
def test_pipecat_speech_that_resumes_within_the_vads_silence_window_is_one_segment(
    gap_us, segments
):
    """The VAD reports silence after 0.2 s of it."""
    result = pc.run(_two_speeches(1_000_000 + gap_us))
    result.assert_contracts()
    started = [700_000, 1_200_000 + gap_us][:segments]
    ended = [1_200_000, 1_700_000 + gap_us][-segments:]
    assert (_at(result, "user.vad_start"), _at(result, "user.vad_end")) == (started, ended)
    # Either way the turn is one: the second speech began before the first turn could end.
    assert _shown(result) == [["Tell me about the menu."]]


# 0.7 and 0.35 s are not exact as floats, and neither is their sum with the 0.2 s before.
@pytest.mark.parametrize("timeout", [0.6, 0.7, 0.35])
@pytest.mark.parametrize("early_us", [1, 0, -1])
def test_pipecat_a_turn_ends_exactly_user_speech_timeout_after_the_vad_reports_silence(
    timeout, early_us
):
    """The VAD reports the end of "Tell me" at 1.2 s and the next speech 0.2 s after it starts."""
    over_us = 1_200_000 + to_us(timeout)
    result = pc.run(_two_speeches(over_us - 200_000 - early_us, user_speech_timeout=timeout))
    result.assert_contracts()
    if early_us > 0:
        # The user spoke again a microsecond before the turn could end: it goes on.
        assert _shown(result) == [["Tell me about the menu."]]
        assert _at(result, "llm.request") == [over_us - early_us + 500_000 + to_us(timeout)]
    else:
        assert _shown(result) == [["Tell me"], ["Tell me", "about the menu."]]
        assert _at(result, "llm.request")[0] == over_us


@pytest.mark.parametrize("stop_secs, timeout", [(0.2, 0.6), (0.3, 0.35), (0.5, 0.2)])
def test_pipecat_the_turn_ends_where_the_header_says_a_search_should_aim(stop_secs, timeout):
    """Pipecat waits for the VAD's silence and then for the timeout, one after the other."""
    case = pc.Case(
        "turn end",
        lambda llm: LLMContext(),
        [pc.UserSpeech(0.5, 1.5, "Hello there.")],
        pc.ScriptedModel().otherwise(say="Good morning."),
        vad_stop_secs=stop_secs,
        user_speech_timeout=timeout,
    )
    result = pc.run(case)
    header = result.events[0]["data"]["scenario"]
    assert header["session"]["turn_end_delay_us"] == to_us(stop_secs) + to_us(timeout)
    aimed = 500_000 + anchors(header, header["speeches"][0])["turn_end"]
    assert _at(result, "llm.request") == [aimed]
