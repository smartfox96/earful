"""Clocked audio sink: the playout witness.

Consumes frames on the virtual clock and reads word stamps out of the PCM itself. A word
counts as played only when its last sample has been consumed; a word the playout position
lands inside is recorded as cut.

The sink records what it was told (``sink.pause``, ``sink.resume``, ``sink.clear``,
``sink.flush``) separately from what it did (``playout.*``), so the delay between a command
and silence is measurable. A segment is numbered when its first frame arrives, whether or
not it ever becomes audible.

Playout model:

* Playback of a segment starts at its first frame. Audio may arrive faster than real
  time; it must never arrive late (an underrun is counted and invalidates the run).
* ``flush`` ends a segment once everything pushed has played. ``clear_buffer`` ends it
  at once.
* Pause belongs to the output, not to a segment, as in livekit-agents' room output: once
  paused, the output plays nothing, including a segment that arrives later, until it is
  resumed. Clearing a segment does not resume the output.

All arithmetic is in integer microseconds and whole samples. A sample counts as consumed
once it has played in full, so a position between two samples rounds down, and a word's
completion time is the first microsecond at which its last sample has played.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from livekit import rtc
from livekit.agents.voice import io

from ..core.ledger import Ledger
from ..core.timebase import US_PER_SECOND, to_seconds
from .providers import StampTable


class _Segment:
    """One playout segment: the audio between two flushes or clears."""

    __slots__ = (
        "flushed",
        "number",
        "pause_began_us",
        "paused_total_us",
        "pauses",
        "pushed_samples",
        "runs",
        "sample_rate",
        "started_us",
    )

    def __init__(self, number: int, sample_rate: int) -> None:
        self.number = number
        self.runs: list[list[int]] = []  # [stamp, sample count], in arrival order
        self.pushed_samples = 0
        self.sample_rate = sample_rate
        self.started_us: int | None = None  # None until the segment becomes audible
        self.pause_began_us: int | None = None  # set while an audible segment is paused
        self.paused_total_us = 0
        self.pauses: list[tuple[int, int]] = []  # (audio played when paused, pause length)
        self.flushed = False

    def pushed_us(self) -> int:
        """Length of the audio given so far: the instant its last sample has played."""
        return -(-self.pushed_samples * US_PER_SECOND // self.sample_rate)

    def played_us(self, now: int) -> int:
        """Audio played by ``now``; may exceed ``pushed_us`` if playout ran dry."""
        if self.started_us is None:
            return 0
        end = self.pause_began_us if self.pause_began_us is not None else now
        return end - self.started_us - self.paused_total_us

    def time_of(self, played_us: int) -> int:
        """Absolute time at which playout reached ``played_us`` of audio."""
        assert self.started_us is not None
        paused = sum(length for at, length in self.pauses if at < played_us)
        return self.started_us + played_us + paused


class ClockedSink(io.AudioOutput):
    def __init__(self, ledger: Ledger, table: StampTable, *, can_pause: bool = True) -> None:
        super().__init__(
            label="EarfulClockedSink",
            capabilities=io.AudioOutputCapabilities(pause=can_pause),
            next_in_chain=None,
            sample_rate=None,
        )
        self._ledger = ledger
        self._table = table
        self._can_pause = can_pause
        self._segments = 0
        self._segment: _Segment | None = None
        self._paused = False  # what the framework last asked for; outlives segments
        self._finish_handle: asyncio.TimerHandle | None = None
        self.stamp_errors = 0
        self.underruns = 0

    # ------------------------------------------------------------------ word accounting

    def _words_at(
        self, segment: _Segment, position_samples: int
    ) -> tuple[list[int], list[int], int | None, int]:
        """Words fully consumed by ``position_samples``, the time (us) each one's last sample
        was consumed, the stamp cut there if any, and how many of its samples played.
        """
        consumed: dict[int, int] = {}
        last_sample: dict[int, int] = {}  # stamp -> offset just past its last consumed sample
        offset = 0
        for stamp, count in segment.runs:
            take = max(0, min(count, position_samples - offset))
            if stamp != 0 and take:
                consumed[stamp] = consumed.get(stamp, 0) + take
                last_sample[stamp] = offset + take
            offset += count
        played: list[int] = []
        cut: int | None = None
        cut_samples = 0
        for stamp, got in consumed.items():
            if got >= self._table.words[stamp]["samples"]:
                played.append(stamp)
            else:
                cut, cut_samples = stamp, got
        done = [
            segment.time_of(-(-last_sample[s] * US_PER_SECOND // segment.sample_rate))
            for s in played
        ]
        return played, done, cut, cut_samples

    def _utterances(self, segment: _Segment) -> list[int]:
        seen: list[int] = []
        for stamp, _count in segment.runs:
            if stamp and stamp in self._table.words:
                u = self._table.words[stamp]["utterance"]
                if u not in seen:
                    seen.append(u)
        return seen

    # ------------------------------------------------------------------ segment lifecycle

    def _cancel_finish(self) -> None:
        if self._finish_handle is not None:
            self._finish_handle.cancel()
            self._finish_handle = None

    def _start(self, segment: _Segment) -> None:
        segment.started_us = self._ledger.now_us()
        utterances = self._utterances(segment)
        self._ledger.append(
            "sink",
            "playout.started",
            segment=segment.number,
            utterance=utterances[0] if utterances else None,
        )
        self.on_playback_started(created_at=time.time())

    def _schedule_finish(self, segment: _Segment) -> None:
        self._cancel_finish()
        if self._paused:
            return  # deferred until resume()
        delay_us = max(segment.pushed_us() - segment.played_us(self._ledger.now_us()), 0)
        self._finish_handle = asyncio.get_running_loop().call_later(
            to_seconds(delay_us), lambda: self._finish(segment, interrupted=False)
        )

    def _finish(self, segment: _Segment, *, interrupted: bool) -> None:
        self._cancel_finish()
        pushed_us = segment.pushed_us()
        if segment.started_us is None:  # never audible: nothing was consumed
            position_us, position_samples = 0, 0
        elif interrupted:
            position_us = min(segment.played_us(self._ledger.now_us()), pushed_us)
            position_samples = min(
                position_us * segment.sample_rate // US_PER_SECOND, segment.pushed_samples
            )
        else:
            position_us, position_samples = pushed_us, segment.pushed_samples
        played, done, cut, cut_samples = self._words_at(segment, position_samples)
        self._ledger.append(
            "sink",
            "playout.finished",
            segment=segment.number,
            interrupted=interrupted,
            audible=segment.started_us is not None,
            utterances=self._utterances(segment),
            position_us=position_us,
            pushed_us=pushed_us,
            words_played=played,
            words_completed_us=done,
            word_cut=cut,
            word_cut_samples=cut_samples,
            text_played=" ".join(self._table.words[s]["text"] for s in played),
        )
        self._segment = None
        self.on_playback_finished(
            playback_position=to_seconds(position_us),
            interrupted=interrupted,
            synchronized_transcript=None,
        )

    def _command(self, name: str) -> None:
        self._ledger.append(
            "sink", f"sink.{name}", segment=self._segment.number if self._segment else None
        )

    # ------------------------------------------------------------------ AudioOutput API

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        await super().capture_frame(frame)

        segment = self._segment
        if segment is None:
            self._segments += 1
            segment = self._segment = _Segment(self._segments, frame.sample_rate)
        elif frame.sample_rate != segment.sample_rate:
            self.stamp_errors += 1  # resampled mid-segment: stamps can no longer be trusted
        if (
            segment.started_us is not None
            and segment.pause_began_us is None
            and segment.played_us(self._ledger.now_us()) > segment.pushed_us()
        ):
            # Playout ran out of audio before this frame arrived. The playout model assumes
            # audio arrives ahead of playback, so word times are now unreliable.
            self.underruns += 1

        samples = memoryview(frame.data).cast("B").cast("h")
        for value in samples:
            if value != 0 and value not in self._table.words:
                self.stamp_errors += 1
            if segment.runs and segment.runs[-1][0] == value:
                segment.runs[-1][1] += 1
            else:
                segment.runs.append([value, 1])
        segment.pushed_samples += len(samples)

        if segment.started_us is None and not self._paused:
            self._start(segment)

    def flush(self) -> None:
        super().flush()
        self._command("flush")
        segment = self._segment
        if segment is None or not segment.pushed_samples:
            return
        segment.flushed = True
        self._schedule_finish(segment)

    def clear_buffer(self) -> None:
        self._command("clear")
        if self._segment is not None and self._segment.pushed_samples:
            self._finish(self._segment, interrupted=True)

    def pause(self) -> None:
        super().pause()
        if self._paused:
            return  # the framework repeats pause on every VAD update; the state is unchanged
        self._paused = True
        self._command("pause")
        self._cancel_finish()
        segment = self._segment
        if segment is not None and segment.started_us is not None:
            segment.pause_began_us = self._ledger.now_us()
            self._ledger.append(
                "sink",
                "playout.paused",
                segment=segment.number,
                played_us=segment.played_us(self._ledger.now_us()),
            )

    def resume(self) -> None:
        super().resume()
        if not self._paused:
            return
        self._paused = False
        self._command("resume")
        segment = self._segment
        if segment is None:
            return
        if segment.pause_began_us is not None:
            # A pause during playout is recorded at the playout position where it happened.
            length = self._ledger.now_us() - segment.pause_began_us
            segment.pauses.append((segment.played_us(self._ledger.now_us()), length))
            segment.paused_total_us += length
            segment.pause_began_us = None
            self._ledger.append(
                "sink",
                "playout.resumed",
                segment=segment.number,
                played_us=segment.played_us(self._ledger.now_us()),
            )
        elif segment.started_us is None:
            self._start(segment)  # a pause before the segment was audible only delayed it
        if segment.flushed:
            self._schedule_finish(segment)

    def capabilities_summary(self) -> dict[str, Any]:
        return {"pause": self._can_pause}
