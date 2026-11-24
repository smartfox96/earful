"""The scripted user: when they speak and what the recognizer hears."""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass

from ...core.ledger import Ledger
from ...core.timebase import to_seconds, to_us
from ..clock import now_us


async def sleep_until(t0_us: int, at_us: int) -> None:
    delay_us = t0_us + at_us - now_us()
    if delay_us > 0:
        await asyncio.sleep(to_seconds(delay_us))


@dataclass(frozen=True)
class UserSpeech:
    """One scripted user utterance, in seconds from the world's origin (first audio frame)."""

    start: float
    end: float
    transcript: str  # empty: VAD fires but STT stays silent (noise)
    stt_delay: float = 0.2
    expects_reply: bool | None = None  # None: true for a non-empty, non-backchannel turn
    backchannel: bool = False  # acknowledging, not taking the floor
    retires: tuple[str, ...] = ()  # intents this turn corrects or cancels

    @property
    def start_us(self) -> int:
        return to_us(self.start)

    @property
    def end_us(self) -> int:
        return to_us(self.end)

    @property
    def stt_delay_us(self) -> int:
        return to_us(self.stt_delay)

    @property
    def owes_reply(self) -> bool:
        if self.expects_reply is not None:
            return self.expects_reply
        return bool(self.transcript) and not self.backchannel


class WorldOrigin:
    """The instant the scripted user's clock starts: the first audio frame a provider gets.
    VAD and STT must agree on it, or the run is invalid; ``tolerance_us`` is for real-clock runs."""

    def __init__(self, ledger: Ledger, errors: list[str], *, tolerance_us: int = 0) -> None:
        self._ledger = ledger
        self._errors = errors
        self._tolerance_us = tolerance_us
        self.t0_us: int | None = None
        self.by: str | None = None
        self.started = asyncio.Event()  # set once the origin is known

    def mark(self, by: str, at_us: int) -> None:
        if self.t0_us is None:
            self.t0_us, self.by = at_us, by
            self._ledger.append("world", "user.origin", by=by)
            self.started.set()
        elif abs(at_us - self.t0_us) > self._tolerance_us:
            self._errors.append(
                f"{by} started its clock at {at_us} us, {self.by} at {self.t0_us} us"
            )


def _check_speeches(speeches: list[UserSpeech]) -> list[UserSpeech]:
    speeches = sorted(speeches, key=lambda s: s.start)
    for prev, nxt in itertools.pairwise(speeches):
        if prev.end > nxt.start:
            raise ValueError("scripted user speeches overlap")
    return speeches


def vad_segments(speeches: list[UserSpeech], min_silence_us: int) -> list[list[int]]:
    """The indices of ``speeches``, grouped into what a VAD hears as one segment: speech
    that resumes within ``min_silence_us`` belongs to the segment before it."""
    segments: list[list[int]] = []
    for index, speech in enumerate(speeches):
        if segments and speech.start_us - speeches[segments[-1][-1]].end_us < min_silence_us:
            segments[-1].append(index)
        else:
            segments.append([index])
    return segments
