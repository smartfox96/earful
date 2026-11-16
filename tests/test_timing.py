"""Scripted times decide framework thresholds exactly: no float rounding in between."""
import livekit.agents as lk
import pytest

from earful.core.timebase import to_seconds, to_us
from earful.livekit.cases import _barge_in_policy
from earful.livekit.harness import Case, run_case
from earful.livekit.providers import UserSpeech


def test_conversion_is_exact_for_decimal_seconds():
    assert to_us(0.3) == 300_000 and to_us(5.5) == 5_500_000
    assert to_seconds(500_000) == 0.5
    # the subtraction a float clock would do is not exact; the integer one is
    assert 4.1 - 3.6 < 0.5
    assert to_seconds(to_us(4.1) - to_us(3.6)) == 0.5


# 3.6 and 3.85 are start times at which float subtraction puts a 0.5 s speech just under
# the framework's 0.5 s interruption threshold, delaying the interruption by one VAD tick.
@pytest.mark.parametrize("start", [3.5, 3.6, 3.85, 4.1, 4.35, 5.0, 5.3])
def test_interruption_lands_exactly_on_the_declared_threshold(start):
    case = Case(
        name="threshold",
        agent=lambda backend: lk.Agent(instructions="You are a restaurant host."),
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
