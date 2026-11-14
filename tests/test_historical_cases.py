"""The two historical defects, red on affected releases and green on repaired ones.

The expectation is derived from the installed livekit-agents version; the case and the
ledger queries are identical on both sides of each fix.
"""
import livekit.agents as lk

from earful.core.observations import (
    effect_counts,
    memory_gaps,
    played_vs_remembered,
    playouts_started_over_user,
)
from earful.livekit.cases import BOOKING_INTENT, CASES
from earful.livekit.harness import run_case

VERSION = tuple(int(part) for part in lk.__version__.split(".")[:3])
FIX_3702 = (1, 6, 6)  # livekit/agents PR #6349
FIX_5509 = (1, 5, 7)  # livekit/agents PR #5535


def test_3702_interrupted_tool_result():
    events = run_case(CASES["book_then_interrupt"]()).ledger.events
    reservations = effect_counts(events).get(BOOKING_INTENT, 0)
    gaps = memory_gaps(events)
    if VERSION < FIX_3702:
        assert reservations == 2, "affected release: the booking should be duplicated"
        assert gaps == [2], "affected release: model call 2 should not see the tool result"
    else:
        assert reservations == 1
        assert gaps == []


def test_5509_stale_reply_over_new_user_turn():
    events = run_case(CASES["speak_over_new_turn"]()).ledger.events
    overlaps = playouts_started_over_user(events)
    if VERSION < FIX_5509:
        assert len(overlaps) == 1, "affected release: the stale reply should start playing"
        assert overlaps[0]["started_after_user_us"] == 250_000
    else:
        assert overlaps == []


def test_5509_fix_needs_a_pausable_output():
    """The repair pauses the output, so an output that cannot pause is still affected."""
    case = CASES["speak_over_new_turn"]()
    case.sink_can_pause = False
    overlaps = playouts_started_over_user(run_case(case).ledger.events)
    assert len(overlaps) == 1


def test_interrupted_reply_is_cut_mid_word():
    events = run_case(CASES["barge_in_mid_word"]()).ledger.events
    (cut,) = played_vs_remembered(events)
    assert cut["played"] == "Tonight we have a roasted squash soup"
    assert cut["word_cut"] == 8
