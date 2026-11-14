"""Clocked audio sink: the playout witness.

Consumes frames on the virtual clock and reads word stamps out of the PCM itself. A word
counts as played only when its last sample has been consumed; a word the playout position
lands inside is recorded as cut.

Playout semantics follow livekit-agents' own test sink (tests/fake_io.py): playback starts
at the first captured frame, audio may be pushed faster than real time, ``flush`` ends a
segment once everything pushed has played, and ``clear_buffer`` stops immediately.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from livekit import rtc
from livekit.agents.voice import io

from ..core.ledger import Ledger
from .providers import StampTable


def _us(seconds: float) -> int:
    return round(seconds * 1_000_000)


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
        self.stamp_errors = 0
        self._reset()

    # ------------------------------------------------------------------ playout clock

    def _reset(self) -> None:
        self._runs: list[list[int]] = []  # [stamp, sample count], in arrival order
        self._pushed_samples = 0
        self._sample_rate: int | None = None
        self._started_at: float | None = None
        self._paused_at: float | None = None
        self._total_paused = 0.0
        self._flushed = False
        self._finish_handle: asyncio.TimerHandle | None = None

    def _now(self) -> float:
        return asyncio.get_running_loop().time()

    def _played_seconds(self) -> float:
        if self._started_at is None:
            return 0.0
        end = self._paused_at if self._paused_at is not None else self._now()
        return end - self._started_at - self._total_paused

    def _pushed_seconds(self) -> float:
        return self._pushed_samples / self._sample_rate if self._sample_rate else 0.0

    def _cancel_finish(self) -> None:
        if self._finish_handle is not None:
            self._finish_handle.cancel()
            self._finish_handle = None

    # ------------------------------------------------------------------ word accounting

    def _words_at(self, position_samples: int) -> tuple[list[int], int | None]:
        """Stamps fully consumed by ``position_samples``, and the stamp cut there, if any."""
        played: list[int] = []
        cut: int | None = None
        consumed: dict[int, int] = {}
        offset = 0
        for stamp, count in self._runs:
            take = max(0, min(count, position_samples - offset))
            offset += count
            if stamp == 0:
                continue
            consumed[stamp] = consumed.get(stamp, 0) + take
        for stamp, got in consumed.items():
            total = self._table.words[stamp]["samples"]
            if got >= total:
                played.append(stamp)
            elif got > 0:
                cut = stamp
        return played, cut

    def _finish(self, *, interrupted: bool) -> None:
        pushed = self._pushed_seconds()
        position = min(self._played_seconds(), pushed) if interrupted else pushed
        position_samples = round(position * (self._sample_rate or 0))
        played, cut = self._words_at(position_samples)
        segment = self._segments
        self._ledger.append(
            "sink",
            "playout.finished",
            segment=segment,
            interrupted=interrupted,
            position_us=_us(position),
            pushed_us=_us(pushed),
            words_played=played,
            word_cut=cut,
            text_played=" ".join(self._table.words[s]["text"] for s in played),
        )
        self._reset()
        self.on_playback_finished(
            playback_position=position, interrupted=interrupted, synchronized_transcript=None
        )

    def _schedule_finish(self) -> None:
        self._cancel_finish()
        if self._paused_at is not None:
            return  # deferred until resume()
        delay = max(self._pushed_seconds() - self._played_seconds(), 0.0)
        self._finish_handle = asyncio.get_running_loop().call_later(
            delay, lambda: self._finish(interrupted=False)
        )

    def _start(self) -> None:
        self._started_at = self._now()
        self._segments += 1
        self._ledger.append("sink", "playout.started", segment=self._segments)
        self.on_playback_started(created_at=time.time())

    # ------------------------------------------------------------------ AudioOutput API

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        await super().capture_frame(frame)

        if self._sample_rate is None:
            self._sample_rate = frame.sample_rate
        elif frame.sample_rate != self._sample_rate:
            self.stamp_errors += 1  # resampled mid-segment: stamps can no longer be trusted

        samples = memoryview(frame.data).cast("B").cast("h")
        for value in samples:
            if value != 0 and value not in self._table.words:
                self.stamp_errors += 1
            if self._runs and self._runs[-1][0] == value:
                self._runs[-1][1] += 1
            else:
                self._runs.append([value, 1])
        self._pushed_samples += len(samples)

        if self._paused_at is None and self._started_at is None:
            self._start()

    def flush(self) -> None:
        super().flush()
        if not self._pushed_samples:
            return
        self._flushed = True
        self._schedule_finish()

    def clear_buffer(self) -> None:
        if not self._pushed_samples:
            return
        self._cancel_finish()
        self._finish(interrupted=True)

    def pause(self) -> None:
        super().pause()
        if self._paused_at is None:
            self._paused_at = self._now()
            self._ledger.append("sink", "playout.paused", played_us=_us(self._played_seconds()))
        self._cancel_finish()

    def resume(self) -> None:
        super().resume()
        if self._paused_at is not None:
            self._total_paused += self._now() - self._paused_at
            self._paused_at = None
            self._ledger.append("sink", "playout.resumed", played_us=_us(self._played_seconds()))
        if self._started_at is None and self._pushed_samples > 0:
            self._start()
        if self._flushed:
            self._schedule_finish()

    def capabilities_summary(self) -> dict[str, Any]:
        return {"pause": self._can_pause}
