"""Timed interim and final transcripts for a scripted input."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

from ..timing import sleep_until
from .activity import Schedule
from .user import UserSpeech


@dataclass(frozen=True)
class Transcript:
    at_us: int
    speech: int
    final: bool
    text: str


def transcripts(speeches: list[UserSpeech], *, chronological: bool) -> Iterator[Transcript]:
    deadlines: list[tuple[int, int, bool]] = []
    for index, speech in enumerate(speeches):
        if speech.transcript:
            deadlines.append((speech.end_us + speech.stt_delay_us // 2, index, False))
        deadlines.append((speech.end_us + speech.stt_delay_us, index, True))
    for at_us, index, final in sorted(deadlines) if chronological else deadlines:
        text = speeches[index].transcript
        yield Transcript(at_us, index, final, text if final else " ".join(text.split()[:2]))


class Recognition(Schedule):
    def __init__(self, speeches: list[UserSpeech], *, chronological: bool) -> None:
        super().__init__()
        self.speeches = speeches
        self.chronological = chronological

    @property
    def last_us(self) -> int:
        return max((s.end_us + s.stt_delay_us for s in self.speeches), default=0)

    async def events(self, origin_us: int) -> AsyncIterator[Transcript]:
        for event in transcripts(self.speeches, chronological=self.chronological):
            await sleep_until(origin_us, event.at_us)
            yield event
        self.finish()
