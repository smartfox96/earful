"""Record the stamped audio chunks written to the output transport."""

from __future__ import annotations

import asyncio
from bisect import bisect_right
from dataclasses import dataclass, field

from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    CancelFrame,
    Frame,
    InterruptionFrame,
    OutputAudioRawFrame,
    StartFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import TransportParams

from ..core.ledger import Ledger
from ..core.timebase import US_PER_SECOND, to_seconds
from ..scripted.voice import StampReader, StampTable, words_heard, words_missing_audio
from .providers import SAMPLE_RATE


@dataclass
class _Segment:
    number: int
    runs: list[list[int]] = field(default_factory=list)  # [stamp, sample count], as written
    writes: list[tuple[int, int]] = field(default_factory=list)  # (first sample, started us)
    heard: int = 0  # samples that played
    pushed: int = 0
    cleared: bool = False

    def time_of(self, end: int) -> int:
        """When the audio up to sample ``end`` had played."""
        first, started_us = self.writes[bisect_right(self.writes, (end - 1, 2**62)) - 1]
        return started_us + _duration(end - first)


def _duration(samples: int) -> int:
    return -(-samples * US_PER_SECOND // SAMPLE_RATE)


class ClockedOutput(BaseOutputTransport):
    """The playout witness. Pipecat's output transport paces playout itself: it hands the
    transport one chunk at a time and waits for the write to return. A chunk plays for
    its own length here, and what was written is what was heard."""

    def __init__(self, ledger: Ledger, table: StampTable, *, late_us: int = 0) -> None:
        super().__init__(
            TransportParams(
                audio_out_enabled=True,
                audio_out_sample_rate=SAMPLE_RATE,
                audio_out_end_silence_secs=0,
            )
        )
        self._ledger, self._table = ledger, table
        self._late_us = late_us  # how late a write may come and still join the audio before it
        self._due = 0  # when the audio written so far ends
        self._segments = 0
        self._segment: _Segment | None = None
        self._writing = False
        self._reader = StampReader(table)
        self._altered = 0

    @property
    def stamp_errors(self) -> int:
        return self._reader.errors + self._altered

    def _take(self, segment: _Segment, runs: list[tuple[int, int]]) -> None:
        for stamp, count in runs:
            if segment.runs and segment.runs[-1][0] == stamp:
                segment.runs[-1][1] += count
            else:
                segment.runs.append([stamp, count])

    @property
    def playing(self) -> bool:
        return self._segment is not None or self._writing

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def cancel(self, frame: CancelFrame) -> None:
        await super().cancel(frame)
        if self._segment is not None:
            self._finish(self._segment, interrupted=True)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        if isinstance(frame, InterruptionFrame):
            segment = self._segment
            self._ledger.append("sink", "sink.clear", segment=segment.number if segment else None)
            if segment is not None:
                segment.cleared = True
                if not self._writing:
                    self._finish(segment, interrupted=True)
        elif isinstance(frame, TTSStoppedFrame):
            self._ledger.append(
                "sink", "sink.flush", segment=self._segment.number if self._segment else None
            )
        await super().process_frame(frame, direction)

    async def push_frame(
        self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ) -> None:
        # The transport says the bot has stopped once its queue has played out.
        if (
            isinstance(frame, BotStoppedSpeakingFrame)
            and direction is FrameDirection.DOWNSTREAM
            and self._segment is not None
            and not self._writing
        ):
            self._finish(self._segment, interrupted=self._segment.cleared)
        await super().push_frame(frame, direction)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        samples = memoryview(frame.audio).cast("h")
        if frame.sample_rate != SAMPLE_RATE or frame.num_channels != 1:
            self._altered += 1
        segment = self._segment
        if segment is None and not any(samples):
            return True  # silence between utterances is not a segment
        began = segment is None
        if segment is None:
            self._segments += 1
            segment = self._segment = _Segment(self._segments)
        self._take(segment, self._reader.read(samples))
        if began:
            first = next((stamp for stamp, _count in segment.runs if stamp), None)
            self._ledger.append(
                "sink",
                "playout.started",
                segment=segment.number,
                utterance=self._table.words[first]["utterance"] if first else None,
            )
        # A write plays from its arrival. Gaps between writes are silence. On the real clock a
        # sleep ends late, and a write that late continues the audio before it.
        now = self._ledger.now_us()
        start = self._due if 0 < now - self._due <= self._late_us else now
        segment.writes.append((segment.pushed, start))
        segment.pushed += len(samples)
        self._due = start + _duration(len(samples))
        self._writing = True
        try:
            await asyncio.sleep(to_seconds(self._due - now))
        finally:
            self._writing = False
            elapsed = max(0, self._ledger.now_us() - start)
            segment.heard += min(len(samples), elapsed * SAMPLE_RATE // US_PER_SECOND)
        if segment.cleared:
            self._finish(segment, interrupted=True)
        return True

    def _finish(self, segment: _Segment, *, interrupted: bool) -> None:
        self._take(segment, self._reader.end())
        self._altered += words_missing_audio(segment.runs, self._table)
        played, ended, cut, cut_samples = words_heard(segment.runs, segment.heard, self._table)
        heard = [*played, *([] if cut is None else [cut])]
        self._ledger.append(
            "sink",
            "playout.finished",
            segment=segment.number,
            interrupted=interrupted,
            audible=True,
            utterances=list(dict.fromkeys(self._table.words[s]["utterance"] for s in heard)),
            position_us=_duration(segment.heard),
            pushed_us=_duration(segment.pushed),
            words_played=played,
            words_completed_us=[segment.time_of(end) for end in ended],
            word_cut=cut,
            word_cut_samples=cut_samples,
            text_played=" ".join(self._table.words[s]["text"] for s in played),
        )
        self._segment = None
