"""Speech boundaries and inference ticks at their scripted times."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from .user import UserSpeech, vad_segments


@dataclass(frozen=True)
class Detection:
    kind: str
    at_us: int
    segment: int
    speech: int
    spans: list[tuple[int, int]]


def detections(
    speeches: list[UserSpeech],
    confirm_us: int,
    silence_us: int,
    *,
    opened: int | None = None,
    ended: set[int] | None = None,
    ticks: bool = False,
) -> Iterator[Detection]:
    for number, segment in enumerate(vad_segments(speeches, silence_us)):
        spans = [(speeches[i].start_us, speeches[i].end_us) for i in segment]
        if opened is not None:
            spans = [(max(start, opened), end) for start, end in spans if end > opened]
        if not spans or (ended is not None and number in ended):
            continue
        start_us, end_us = spans[0][0], spans[-1][1]
        yield Detection("start", start_us + confirm_us, number, segment[0], spans)
        end_at = end_us + silence_us
        if ticks:
            tick = start_us + 2 * confirm_us
            while tick < end_at:
                yield Detection("inference", tick, number, segment[0], spans)
                tick += confirm_us
        yield Detection("end", end_at, number, segment[-1], spans)
