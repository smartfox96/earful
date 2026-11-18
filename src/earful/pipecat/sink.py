"""Record the stamped audio chunks written to the output transport."""

from __future__ import annotations

import asyncio
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
from ..scripted.voice import StampTable
from .providers import SAMPLE_RATE


@dataclass
class _Segment:
    number: int
    consumed: dict[int, int] = field(default_factory=dict)
    completed_us: dict[int, int] = field(default_factory=dict)
    samples: int = 0
    pushed: int = 0
    cleared: bool = False


def _duration(samples: int) -> int:
    return -(-samples * US_PER_SECOND // SAMPLE_RATE)


class ClockedOutput(BaseOutputTransport):
    """The playout witness. Pipecat's output transport paces playout itself: it hands the
    transport one chunk at a time and waits for the write to return. A chunk plays for
    its own length here, and what was written is what was heard."""

    def __init__(self, ledger: Ledger, table: StampTable) -> None:
        super().__init__(
            TransportParams(
                audio_out_enabled=True,
                audio_out_sample_rate=SAMPLE_RATE,
                audio_out_end_silence_secs=0,
            )
        )
        self._ledger, self._table = ledger, table
        self._segments = 0
        self._segment: _Segment | None = None
        self._writing = False
        self.stamp_errors = 0

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
            self.stamp_errors += 1
        segment = self._segment
        if segment is None:
            if not any(samples):
                return True  # silence between utterances is not a segment
            self._segments += 1
            segment = self._segment = _Segment(self._segments)
            first = next(value for value in samples if value)
            self._ledger.append(
                "sink",
                "playout.started",
                segment=segment.number,
                utterance=self._table.words[first]["utterance"]
                if first in self._table.words
                else None,
            )
        # A write plays from its arrival. Gaps between writes are silence.
        start = self._ledger.now_us()
        segment.pushed += len(samples)
        self._writing = True
        try:
            await asyncio.sleep(to_seconds(_duration(len(samples))))
        finally:
            self._writing = False
            elapsed = max(0, self._ledger.now_us() - start)
            heard = min(len(samples), elapsed * SAMPLE_RATE // US_PER_SECOND)
            segment.samples += heard
            for offset, stamp in enumerate(samples[:heard], 1):
                if not stamp:
                    continue
                if stamp not in self._table.words:
                    self.stamp_errors += 1
                    continue
                count = segment.consumed.get(stamp, 0) + 1
                segment.consumed[stamp] = count
                if count == self._table.words[stamp]["samples"]:
                    segment.completed_us[stamp] = start + _duration(offset)
        if segment.cleared:
            self._finish(segment, interrupted=True)
        return True

    def _finish(self, segment: _Segment, *, interrupted: bool) -> None:
        played = list(segment.completed_us)
        cut = next((s for s in segment.consumed if s not in segment.completed_us), None)
        utterances = list(
            dict.fromkeys(self._table.words[s]["utterance"] for s in segment.consumed)
        )
        self._ledger.append(
            "sink",
            "playout.finished",
            segment=segment.number,
            interrupted=interrupted,
            audible=True,
            utterances=utterances,
            position_us=_duration(segment.samples),
            pushed_us=_duration(segment.pushed),
            words_played=played,
            words_completed_us=[segment.completed_us[s] for s in played],
            word_cut=cut,
            word_cut_samples=segment.consumed[cut] if cut is not None else 0,
            text_played=" ".join(self._table.words[s]["text"] for s in played),
        )
        self._segment = None
