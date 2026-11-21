"""Clocked audio sink, the playout witness: it reads word stamps out of the PCM itself.
All arithmetic is in integer microseconds and whole samples."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from livekit import rtc

from ..core.ledger import Ledger
from ..core.timebase import US_PER_SECOND, to_seconds
from ..scripted.voice import StampReader, StampTable, words_heard, words_missing_audio
from ._compat import io
from .providers import scripted, scripted_call


class _Segment:
    # One playout segment: the audio between two flushes or clears.
    __slots__ = (
        "announced",
        "flushed",
        "number",
        "pause_began_us",
        "paused_total_us",
        "pauses",
        "pushed_samples",
        "runs",
        "sample_rate",
        "started_us",
        "starved",
    )

    def __init__(self, number: int, sample_rate: int) -> None:
        self.number = number
        self.runs: list[list[int]] = []  # [stamp, sample count], in arrival order
        self.pushed_samples = 0
        self.sample_rate = sample_rate
        self.announced = False  # the framework has been told playback started
        self.started_us: int | None = None  # None until the segment becomes audible
        self.pause_began_us: int | None = None  # set while an audible segment is paused
        self.paused_total_us = 0
        self.pauses: list[tuple[int, int]] = []  # (audio played when paused, pause length)
        self.flushed = False
        self.starved = False

    def pushed_us(self) -> int:
        # Rounded up: the instant the last sample has played in full.
        return -(-self.pushed_samples * US_PER_SECOND // self.sample_rate)

    def played_us(self, now: int) -> int:
        # May exceed ``pushed_us`` if playout ran dry.
        if self.started_us is None:
            return 0
        end = self.pause_began_us if self.pause_began_us is not None else now
        return end - self.started_us - self.paused_total_us

    def time_of(self, played_us: int) -> int:
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
        self._reader = StampReader(table)
        self._resampled = 0
        self._short = 0  # words that reached the sink with part of their audio missing
        self.underruns = 0

    @property
    def stamp_errors(self) -> int:
        return self._reader.errors + self._resampled + self._short

    def _take(self, segment: _Segment, runs: list[tuple[int, int]]) -> None:
        for stamp, count in runs:
            if segment.runs and segment.runs[-1][0] == stamp:
                segment.runs[-1][1] += count
            else:
                segment.runs.append([stamp, count])

    def _words_at(
        self, segment: _Segment, position_samples: int
    ) -> tuple[list[int], list[int], int | None, int]:
        # Returns the stamps fully played, when each ended (us), the stamp cut and its samples.
        played, ended, cut, cut_samples = words_heard(segment.runs, position_samples, self._table)
        done = [segment.time_of(-(-end * US_PER_SECOND // segment.sample_rate)) for end in ended]
        return played, done, cut, cut_samples

    def _utterances(self, segment: _Segment) -> list[int]:
        seen: list[int] = []
        for stamp, _count in segment.runs:
            if stamp:
                u = self._table.words[stamp]["utterance"]
                if u not in seen:
                    seen.append(u)
        return seen

    def _cancel_finish(self) -> None:
        if self._finish_handle is not None:
            self._finish_handle.cancel()
            self._finish_handle = None

    def _start(self, segment: _Segment) -> None:
        if not segment.announced:
            segment.announced = True
            self.on_playback_started(created_at=time.time())

        def start() -> None:
            if self._segment is not segment or segment.started_us is not None or self._paused:
                return  # cleared or paused before it began; resume() starts it
            segment.started_us = self._ledger.now_us()
            utterances = self._utterances(segment)
            self._ledger.append(
                "sink",
                "playout.started",
                segment=segment.number,
                utterance=utterances[0] if utterances else None,
            )
            self._schedule_finish(segment)

        self._begin(scripted_call("output")(start))

    def _schedule_finish(self, segment: _Segment) -> None:
        self._cancel_finish()
        if segment.started_us is None or segment.pause_began_us is not None:
            return  # not playing: deferred until it starts or resumes
        delay_us = max(segment.pushed_us() - segment.played_us(self._ledger.now_us()), 0)
        self._finish_handle = asyncio.get_running_loop().call_later(
            to_seconds(delay_us), lambda: self._exhausted(segment)
        )

    @scripted_call("output")
    def _exhausted(self, segment: _Segment) -> None:
        if segment.flushed:
            self._finish(segment, interrupted=False)
        elif self._segment is segment and segment.pause_began_us is None:
            segment.starved = True
            self._pause(segment)

    def _pause(self, segment: _Segment) -> None:
        self._cancel_finish()
        segment.pause_began_us = self._ledger.now_us()
        self._ledger.append(
            "sink",
            "playout.paused",
            segment=segment.number,
            played_us=segment.played_us(self._ledger.now_us()),
        )

    def _resume(self, segment: _Segment) -> None:
        assert segment.pause_began_us is not None
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
        self._take(segment, self._reader.end())
        self._short += words_missing_audio(segment.runs, self._table)
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

    # Seams for faults: a subclass overrides these two to start late or take time to obey.

    def _begin(self, start: Callable[[], None]) -> None:
        start()

    def _obey(self, action: Callable[[], None]) -> None:
        action()

    def _command(self, name: str) -> None:
        self._ledger.append(
            "sink", f"sink.{name}", segment=self._segment.number if self._segment else None
        )

    @scripted("output")
    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        await super().capture_frame(frame)

        segment = self._segment
        if segment is None:
            self._segments += 1
            segment = self._segment = _Segment(self._segments, frame.sample_rate)
            if frame.sample_rate != self._table.sample_rate:
                self._resampled += 1  # a word's sample count is at the voice's rate
        elif frame.sample_rate != segment.sample_rate:
            self._resampled += 1  # resampled mid-segment: stamps can no longer be trusted
        if (
            segment.started_us is not None
            and segment.pause_began_us is None
            and segment.played_us(self._ledger.now_us()) > segment.pushed_us()
        ):
            # Playback ran past the buffer's end: word times can no longer be trusted.
            self.underruns += 1

        samples = memoryview(frame.data).cast("B").cast("h")
        self._take(segment, self._reader.read(samples))
        segment.pushed_samples += len(samples)

        if segment.starved:
            segment.starved = False
            if not self._paused:
                self._resume(segment)

        if segment.started_us is None and not self._paused:
            self._start(segment)
        else:
            self._schedule_finish(segment)

    @scripted_call("output")
    def flush(self) -> None:
        super().flush()
        self._command("flush")
        segment = self._segment
        if segment is None or not segment.pushed_samples:
            return
        segment.flushed = True
        if segment.starved:
            self._finish(segment, interrupted=False)
        else:
            self._schedule_finish(segment)

    @scripted_call("output")
    def clear_buffer(self) -> None:
        self._command("clear")
        segment = self._segment
        if segment is None or not segment.pushed_samples:
            return

        def clear() -> None:
            if self._segment is segment:  # not already finished by the time it is obeyed
                self._finish(segment, interrupted=True)

        self._obey(scripted_call("output")(clear))

    @scripted_call("output")
    def pause(self) -> None:
        super().pause()
        if self._paused:
            return  # the framework repeats pause on every VAD update; the state is unchanged
        self._paused = True
        self._command("pause")

        def pause() -> None:
            segment = self._segment
            if not self._paused:
                return  # resumed before the pause was obeyed
            if (
                segment is not None
                and segment.started_us is not None
                and segment.pause_began_us is None
            ):
                self._pause(segment)

        self._obey(scripted_call("output")(pause))

    @scripted_call("output")
    def resume(self) -> None:
        super().resume()
        if not self._paused:
            return
        self._paused = False
        self._command("resume")
        segment = self._segment
        if segment is None:
            return
        if segment.starved:
            return  # still no audio; the next frame resumes playback
        if segment.pause_began_us is not None:
            self._resume(segment)
        elif segment.started_us is None:
            self._start(segment)  # a pause before the segment was audible only delayed it
        self._schedule_finish(segment)

    def capabilities_summary(self) -> dict[str, Any]:
        return {"pause": self._can_pause}
