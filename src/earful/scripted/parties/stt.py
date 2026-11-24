"""Timed interim and final transcripts for a scripted input."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

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
