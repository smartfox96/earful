"""Scenarios with a deliberate break, and their controls, for the search tests.

Each is a no-argument function returning a ``Case``, so a case file can name it
(``breaks:slow_to_stop``) and a worker process can build it. Every break is an output
that keeps playing for 400 ms after it is told to stop; they differ in when.
"""
import asyncio

from earful.livekit.cases import CASES, LONG_REPLY
from earful.livekit.harness import Case
from earful.livekit.providers import UserSpeech, letters
from earful.livekit.sink import ClockedSink

# How much of the long reply has played when its seventh word ends: a boundary well
# inside the reply, with nothing else happening near it.
SEVENTH_WORD_ENDS_US = sum(letters(word) for word in LONG_REPLY.split()[:7]) * 60_000


def _slow_between(after_us: int, before_us: int) -> type[ClockedSink]:
    """An output that is slow to stop only if told to while the audio it has played is at
    least ``after_us`` and less than ``before_us``."""

    class Slow(ClockedSink):
        def _obey(self, action):
            segment = self._segment
            started = segment is not None and segment.started_us is not None
            played = segment.played_us(self._ledger.now_us()) if started else None
            if played is not None and after_us <= played < before_us:
                asyncio.get_running_loop().call_later(0.4, action)
            else:
                action()

    return Slow


def host() -> Case:
    """The control: a long reply, and a user who says "Stop." once it is over. As written
    nothing is interrupted, so every scenario built on it passes until a search moves
    "Stop." into the reply."""
    case = CASES["barge_in_mid_word"]()
    case.speeches = [case.speeches[0], UserSpeech(start=14.0, end=15.0, transcript="Stop.")]
    case.drain_delay = 4.0
    return case


def _with(sink: type[ClockedSink]) -> Case:
    case = host()
    case.sink = sink
    return case


def slow_to_stop() -> Case:
    """Slow to stop whenever it is told to."""
    return _with(_slow_between(0, 10**12))


def slow_to_stop_for_30ms() -> Case:
    """Slow to stop only in the 30 ms after the seventh word ends."""
    return _with(_slow_between(SEVENTH_WORD_ENDS_US, SEVENTH_WORD_ENDS_US + 30_000))


def slow_to_stop_on_an_instant() -> Case:
    """Slow to stop only if told to at the very microsecond the seventh word ends."""
    return _with(_slow_between(SEVENTH_WORD_ENDS_US, SEVENTH_WORD_ENDS_US + 1))


def interrupted_and_slow_to_stop() -> Case:
    """The reference barge-in, which interrupts the reply as written, with an output that
    is slow to stop: a scenario that fails before anything is moved."""
    case = CASES["barge_in_mid_word"]()
    case.sink = _slow_between(0, 10**12)
    return case

