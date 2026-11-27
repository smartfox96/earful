"""The scripted user on LiveKit: its microphone, and the VAD and recognizer that hear it."""

from __future__ import annotations

import asyncio
import bisect
import dataclasses
from typing import Any, NoReturn

from livekit import rtc
from livekit.agents import stt as lk_stt
from livekit.agents import utils
from livekit.agents import vad as lk_vad

from ..core.ledger import Ledger
from ..core.timebase import to_seconds, to_us
from ..scripted.ceilings import cancel
from ..scripted.clock import now_us
from ..scripted.parties.stt import Recognition, Transcript
from ..scripted.parties.vad import detections
from ..scripted.provider_errors import scripted
from ..scripted.world import UserSpeech, WorldOrigin, _check_speeches, sleep_until
from . import seams
from .seams import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions, io

# What LiveKit's room input sends by default.
MICROPHONE_FRAME_US = 50_000
SAMPLE_RATE = 24_000
VOICE = (256).to_bytes(2, "little", signed=True)  # one sample of the user's voice


class Microphone(io.AudioInput):
    """Sends a frame every 50 ms from ``open`` until ``close``: voiced while the user speaks,
    silent otherwise."""

    def __init__(self, speeches: list[UserSpeech], origin: WorldOrigin) -> None:
        super().__init__(label="EarfulScriptedInput")
        self._ch = utils.aio.Chan[rtc.AudioFrame]()
        self._sending: asyncio.Task[None] | None = None
        self._speeches = speeches
        self._origin = origin

    async def __anext__(self) -> rtc.AudioFrame:
        return await self._ch.__anext__()

    def open(self) -> None:
        self._sending = asyncio.ensure_future(self._send())

    def close(self) -> None:
        if self._sending is not None:
            cancel(self._sending)
        self._ch.close()

    def _voiced(self, at_us: int) -> bool:
        """Whether the user speaks during the frame that starts at ``at_us``."""
        # Before a provider has heard a frame, this one is the origin if one hears it.
        start = at_us - (self._origin.t0_us if self._origin.t0_us is not None else at_us)
        end = start + MICROPHONE_FRAME_US
        return any(s.start_us < end and s.end_us >= start for s in self._speeches)

    async def _send(self) -> None:
        samples = SAMPLE_RATE * MICROPHONE_FRAME_US // 1_000_000
        sound = {False: b"\x00\x00" * samples, True: VOICE * samples}
        began, sent = now_us(), 0
        while not self._ch.closed:
            # A frame sent late on the real clock still holds the audio of its own 50 ms.
            frame = rtc.AudioFrame(
                data=sound[self._voiced(began + sent * MICROPHONE_FRAME_US)],
                sample_rate=SAMPLE_RATE,
                num_channels=1,
                samples_per_channel=samples,
            )
            self._ch.send_nowait(frame)
            sent += 1
            await sleep_until(began, sent * MICROPHONE_FRAME_US)


class ScriptedVAD(lk_vad.VAD):
    def __init__(
        self,
        ledger: Ledger,
        speeches: list[UserSpeech],
        origin: WorldOrigin,
        *,
        min_speech_duration: float = 0.05,
        min_silence_duration: float = 0.5,
    ) -> None:
        # The framework paces its VAD metrics by this interval.
        capabilities = lk_vad.VADCapabilities(update_interval=min_speech_duration)
        super().__init__(capabilities=capabilities)
        self._ledger = ledger
        self._origin = origin
        self._speeches = _check_speeches(speeches)
        self._min_speech_us = to_us(min_speech_duration)
        self._min_silence_us = to_us(min_silence_duration)
        if self._min_speech_us <= 0:
            raise ValueError("min_speech_duration must be positive")
        self._starts: set[int] = set()  # segments whose start is in the ledger
        self._ends: set[int] = set()  # segments whose end has been reported

    def stream(self) -> lk_vad.VADStream:
        return _ScriptedVADStream(self)


# VADEvent grew fields across releases; only pass the ones this version defines.
_VAD_EVENT_FIELDS = {f.name for f in dataclasses.fields(lk_vad.VADEvent)}

# A stream that got a voiced frame within this long of an instant heard the user then.
HEARD_WITHIN_US = 2 * MICROPHONE_FRAME_US


class _Ears:
    """The frames of the scripted user's microphone that one provider stream receives."""

    def __init__(self) -> None:
        self.first_us: int | None = None
        self.voiced: list[int] = []  # when each frame that carried the user's voice arrived
        self.opened = asyncio.Event()

    async def listen(self, frames: Any) -> None:
        async for frame in frames:
            if isinstance(frame, rtc.AudioFrame):
                at_us = now_us()
                if self.first_us is None:
                    self.first_us = at_us
                    self.opened.set()
                # The framework may pass silence in place of the user's audio.
                if frame.data.tobytes().strip(b"\x00"):
                    self.voiced.append(at_us)

    async def first_frame(self, listening: asyncio.Future[None]) -> int | None:
        """When the stream got its first frame, or None if its input ended before one."""
        opened = asyncio.ensure_future(self.opened.wait())
        either: set[asyncio.Future[Any]] = {opened, listening}
        await asyncio.wait(either, return_when=asyncio.FIRST_COMPLETED)
        cancel(opened)
        return self.first_us

    def heard(self, at_us: int) -> bool:
        """Whether the user's voice reached the stream around ``at_us``."""
        after = bisect.bisect_left(self.voiced, at_us - HEARD_WITHIN_US)
        return after < len(self.voiced) and self.voiced[after] <= at_us + HEARD_WITHIN_US


class _ScriptedVADStream(lk_vad.VADStream):
    @scripted("VAD")
    async def _main_task(self) -> None:
        vad = seams.vad_of(self)
        assert isinstance(vad, ScriptedVAD)
        ears = _Ears()
        listening = asyncio.ensure_future(ears.listen(seams.stream_input(self)))
        try:
            opened = await ears.first_frame(listening)
            if opened is not None and vad._speeches:
                await self._detect(vad, opened)
            await listening
        finally:
            cancel(listening)

    async def _detect(self, vad: ScriptedVAD, opened: int) -> None:
        """Report what the stream hears of each segment, from its first frame on."""
        vad._origin.mark("vad", opened)
        assert vad._origin.t0_us is not None
        t0 = vad._origin.t0_us
        events = detections(
            vad._speeches,
            vad._min_speech_us,
            vad._min_silence_us,
            opened=opened - t0,
            ended=vad._ends,
            ticks=True,
        )
        kinds = {
            "start": lk_vad.VADEventType.START_OF_SPEECH,
            "inference": lk_vad.VADEventType.INFERENCE_DONE,
            "end": lk_vad.VADEventType.END_OF_SPEECH,
        }
        for event in events:
            await sleep_until(t0, event.at_us)
            self._emit(kinds[event.kind], event.spans, now_us() - t0)
            if event.kind == "start" and event.segment not in vad._starts:
                vad._starts.add(event.segment)
                vad._ledger.append("world", "user.vad_start", speech=event.speech)
            if event.kind == "end":
                vad._ends.add(event.segment)
                vad._ledger.append("world", "user.vad_end", speech=event.speech)

    def _emit(self, type: lk_vad.VADEventType, spans: list[tuple[int, int]], at_us: int) -> None:
        # What the Silero plugin reports for a detector that is never wrong. It counts a
        # segment as speech, and its speech as still growing, until the end is published.
        # Durations are integer differences converted once: 500 000 us reports exactly 0.5 s.
        start, end = spans[0][0], spans[-1][1]
        ended = type is lk_vad.VADEventType.END_OF_SPEECH
        begun, last_end = max(span for span in spans if span[0] <= at_us)
        values: dict[str, Any] = {
            "type": type,
            "samples_index": 0,
            "timestamp": to_seconds(at_us),
            "speech_duration": to_seconds((end if ended else at_us) - start),
            "silence_duration": to_seconds(at_us - end) if ended else 0.0,
            "raw_accumulated_speech": to_seconds(at_us - begun) if at_us <= last_end else 0.0,
            "raw_accumulated_silence": to_seconds(max(0, at_us - last_end)),
            "speaking": not ended,
        }
        kwargs = {k: v for k, v in values.items() if k in _VAD_EVENT_FIELDS}
        seams.send_event(self, lk_vad.VADEvent(**kwargs))


# The type argument names a provider's extra events: none here (NoReturn is 3.10's Never).
class ScriptedSTT(lk_stt.STT[NoReturn]):
    def __init__(self, ledger: Ledger, speeches: list[UserSpeech], origin: WorldOrigin) -> None:
        # It sends one interim transcript per speech, half way through the recognizer's delay.
        super().__init__(capabilities=lk_stt.STTCapabilities(streaming=True, interim_results=True))
        self._ledger = ledger
        self._origin = origin
        self.script = Recognition(_check_speeches(speeches), chronological=False)
        if not speeches:
            self.script.finish()
        # The framework opens a stream for each agent or task it starts, and may hand one on.
        self._listening: list[tuple[_ScriptedRecognizeStream, _Ears]] = []
        self._delivering: asyncio.Task[None] | None = None

    def begin(self) -> None:
        """Start the run's timetable of transcripts. It counts from the origin."""
        if not self.script.quiet():
            self._delivering = asyncio.ensure_future(self._deliver())
            self._delivering.add_done_callback(lambda task: task.cancelled() or task.exception())

    def end(self) -> None:
        if self._delivering is not None:
            cancel(self._delivering)

    @scripted("recognizer")
    async def _deliver(self) -> None:
        try:
            await self._origin.started.wait()
            assert self._origin.t0_us is not None
            await self.script.deliver(self._origin.t0_us, self._hearer, self._send)
        except Exception:
            # The scenario ends on the last transcript: a run must not wait for it now.
            self.script.finish()
            raise

    def _hearer(self, at_us: int) -> _ScriptedRecognizeStream | None:
        return next((stream for stream, ears in self._listening if ears.heard(at_us)), None)

    async def _send(self, stream: _ScriptedRecognizeStream, event: Transcript) -> None:
        stream._send(event.text, final=event.final)
        if event.final:
            self._ledger.append(
                "world", "user.transcript_final", speech=event.speech, text=event.text
            )

    async def _recognize_impl(self, buffer: Any, **kwargs: Any) -> lk_stt.SpeechEvent:
        raise NotImplementedError("ScriptedSTT only supports streaming recognition")

    def stream(
        self,
        *,
        language: Any = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> lk_stt.RecognizeStream:
        return _ScriptedRecognizeStream(stt=self, conn_options=conn_options)


class _ScriptedRecognizeStream(lk_stt.RecognizeStream):
    @scripted("recognizer")
    async def _run(self) -> None:
        stt = seams.recognizer_of(self)
        assert isinstance(stt, ScriptedSTT)
        ears = _Ears()
        listening = asyncio.ensure_future(ears.listen(seams.stream_input(self)))
        stt._listening.append((self, ears))
        try:
            opened = await ears.first_frame(listening)
            if opened is not None and not stt.script.quiet():
                stt._origin.mark("stt", opened)
            await listening
        finally:
            stt._listening.remove((self, ears))
            cancel(listening)

    def _send(self, text: str, *, final: bool) -> None:
        kind = (
            lk_stt.SpeechEventType.FINAL_TRANSCRIPT
            if final
            else lk_stt.SpeechEventType.INTERIM_TRANSCRIPT
        )
        seams.send_event(
            self, lk_stt.SpeechEvent(type=kind, alternatives=[seams.speech_data(text)])
        )
