"""Scripted speech, transcription, model responses and stamped audio for Pipecat."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from array import array
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any, cast

from pipecat.frames.frames import (
    Frame,
    FunctionCallFromLLM,
    FunctionCallResultFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    StartFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.aggregators import async_tool_messages
from pipecat.processors.aggregators.llm_context import LLMContext, LLMStandardMessage
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import LLMService
from pipecat.services.settings import LLMSettings, STTSettings, TTSSettings
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService
from pipecat.utils.time import time_now_iso8601

from ..core.ledger import Ledger
from ..core.timebase import to_us
from ..scripted.clock import now_us
from ..scripted.model import Policy, ReplyRegister
from ..scripted.voice import StampTable, letters
from ..scripted.world import UserSpeech, WorldOrigin, sleep_until, vad_segments
from .tools import ToolWitness

SAMPLE_RATE = 24000


class ScriptedUser(FrameProcessor):
    """The head of the pipeline: tells the framework when the user starts and stops
    speaking, as a VAD would, at the times the scenario scripts."""

    def __init__(
        self,
        ledger: Ledger,
        speeches: list[UserSpeech],
        origin: WorldOrigin,
        left: asyncio.Event,
        *,
        start_secs: float,
        stop_secs: float,
    ) -> None:
        super().__init__()
        self._ledger, self._origin, self._left = ledger, origin, left
        self._speeches = sorted(speeches, key=lambda s: s.start)
        self.done = False
        self._start_secs, self._stop_secs = start_secs, stop_secs

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if isinstance(frame, StartFrame):
            self.create_task(self._script(), "earful-scripted-vad")

    async def _script(self) -> None:
        t0 = now_us()
        self._origin.mark("vad", t0)
        confirm_us, silence_us = to_us(self._start_secs), to_us(self._stop_secs)
        for segment in vad_segments(self._speeches, silence_us):
            first, last = segment[0], segment[-1]
            await sleep_until(t0, self._speeches[first].start_us + confirm_us)
            if self._left.is_set():
                return  # the user has hung up: there is nobody to hear
            self._ledger.append("world", "user.vad_start", speech=first)
            await self.push_frame(
                VADUserStartedSpeakingFrame(start_secs=self._start_secs, timestamp=time.time())
            )
            await sleep_until(t0, self._speeches[last].end_us + silence_us)
            if self._left.is_set():
                return
            self._ledger.append("world", "user.vad_end", speech=last)
            await self.push_frame(
                VADUserStoppedSpeakingFrame(stop_secs=self._stop_secs, timestamp=time.time())
            )

        self.done = True


class ScriptedSTT(STTService):
    """Delivers each speech's transcript at its scripted time. Hears no audio."""

    def __init__(
        self, ledger: Ledger, speeches: list[UserSpeech], origin: WorldOrigin, left: asyncio.Event
    ) -> None:
        # The longest the framework should wait for a transcript after the VAD's stop.
        super().__init__(ttfs_p99_latency=0.0, settings=STTSettings(model=None, language=None))
        self._ledger, self._origin, self._left = ledger, origin, left
        self._speeches = sorted(speeches, key=lambda s: s.start)
        self.done: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        return
        yield  # an async generator, as the base class asks; this service is given no audio

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            self.create_task(self._script(), "earful-scripted-stt")

    async def _script(self) -> None:
        t0 = now_us()
        self._origin.mark("stt", t0)
        # A slow recognition can finish after a later speech's recognition.
        deadlines: list[tuple[int, int, bool]] = []
        for index, speech in enumerate(self._speeches):
            if speech.transcript:
                deadlines.append((speech.end_us + speech.stt_delay_us // 2, index, False))
            deadlines.append((speech.end_us + speech.stt_delay_us, index, True))
        for at_us, index, final in sorted(deadlines):
            await sleep_until(t0, at_us)
            if self._left.is_set():
                return
            speech = self._speeches[index]
            if not speech.transcript:
                continue  # noise occupies its time in the scenario
            if not final:
                interim = " ".join(speech.transcript.split()[:2])
                await self.push_frame(
                    InterimTranscriptionFrame(interim, "user", time_now_iso8601())
                )
            else:
                self._ledger.append(
                    "world", "user.transcript_final", speech=index, text=speech.transcript
                )
                await self.push_frame(
                    TranscriptionFrame(
                        speech.transcript, "user", time_now_iso8601(), finalized=True
                    )
                )
        if not self.done.done():
            self.done.set_result(None)


IN_PROGRESS = "IN_PROGRESS"


def summarize(
    messages: list[Any], error_results: dict[str, str], interrupted: dict[int, object]
) -> list[dict[str, Any]]:
    """Summarize the messages visible to the model, including deferred tool results."""
    items: list[dict[str, Any]] = []
    names: dict[str, str] = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        role, content = message.get("role"), message.get("content")
        text = content if isinstance(content, str) else None
        if isinstance(content, list):
            text = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
        deferred = async_tool_messages.parse_message(cast(LLMStandardMessage, message))
        if deferred is not None and deferred.kind == "started":
            continue
        if role == "tool" or (deferred is not None and deferred.kind == "final"):
            call_id = deferred.tool_call_id if deferred else message.get("tool_call_id")
            if deferred:
                text = deferred.result
            if text == IN_PROGRESS:
                continue  # no result yet; the summaries have no word for that
            items.append(
                {
                    "type": "function_call_output",
                    "id": None,
                    "name": names.get(call_id or ""),
                    "output": text,
                    "call_id": call_id,
                    "is_error": call_id in error_results and error_results[call_id] == text,
                }
            )
            continue
        if text:
            items.append(
                {
                    "type": "message",
                    "id": None,
                    "role": "system" if role == "developer" else role,
                    "text": text,
                    "interrupted": id(message) in interrupted,
                }
            )
        for call in message.get("tool_calls") or []:
            function = call.get("function", {})
            names[call.get("id", "")] = function.get("name")
            items.append(
                {
                    "type": "function_call",
                    "id": None,
                    "name": function.get("name"),
                    "arguments": function.get("arguments"),
                    "call_id": call.get("id"),
                }
            )
    return items


class ScriptedLLM(LLMService):
    """The scripted model, as a Pipecat service. Records every context it is shown."""

    def __init__(
        self,
        ledger: Ledger,
        policy: Policy,
        errors: list[str],
        witness: ToolWitness,
        *,
        ttft_us: dict[int, int],
        duration_us: dict[int, int],
    ) -> None:
        self.interrupted: dict[int, object] = {}
        self.error_results: dict[str, str] = {}
        super().__init__(
            settings=LLMSettings(
                model=None,
                system_instruction=None,
                temperature=None,
                max_tokens=None,
                top_p=None,
                top_k=None,
                frequency_penalty=None,
                presence_penalty=None,
                seed=None,
                filter_incomplete_user_turns=False,
                user_turn_completion_config=None,
                use_provider_thinking_default=False,
            )
        )
        self._ledger, self._policy, self._errors, self._witness = ledger, policy, errors, witness
        self._ttft_us, self._duration_us = ttft_us, duration_us
        self._call_ids: set[str] = set()
        self._calls = 0
        self.generating = 0
        self.register = ReplyRegister()

    def register_function(self, function_name: str | None, handler: Any, **options: Any) -> None:
        name = function_name or "*"
        super().register_function(function_name, self._witness.wrap(handler), **options)
        self._witness.names.add(name)

    def unregister_function(self, function_name: str | None) -> None:
        super().unregister_function(function_name)
        self._witness.names.discard(function_name or "*")

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return
        await self.push_frame(LLMFullResponseStartFrame())
        self.generating += 1
        try:
            await self._generate(frame.context)
        finally:
            self.generating -= 1
            await self.push_frame(LLMFullResponseEndFrame())

    async def push_frame(
        self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ) -> None:
        if (
            isinstance(frame, FunctionCallResultFrame)
            and direction is FrameDirection.DOWNSTREAM
            and (frame.error or not self.has_function(frame.function_name))
        ):
            self.error_results[frame.tool_call_id] = json.dumps(frame.result, ensure_ascii=False)
        await super().push_frame(frame, direction)

    async def _generate(self, context: LLMContext) -> None:
        self._calls += 1
        call = self._calls
        items = summarize(context.get_messages(), self.error_results, self.interrupted)
        self._ledger.append("llm", "llm.request", call=call, items=items)
        try:
            reply = self._policy(items)
        except Exception as exc:
            self._errors.append(f"policy raised on call {call}: {type(exc).__name__}: {exc}")
            raise
        words = reply.text.split()
        tool_calls = [
            t if t.call_id is not None else dataclasses.replace(t, call_id=f"call-{call}-{n}")
            for n, t in enumerate(reply.tool_calls, 1)
        ]
        for tool in tool_calls:
            assert tool.call_id is not None
            if tool.call_id in self._call_ids:
                self._errors.append(f"call {call}: the policy reused tool call id {tool.call_id!r}")
            self._call_ids.add(tool.call_id)
        claims = [
            {
                "intent": claim.intent,
                "first_word": claim.first_word,
                "last_word": len(words) - 1 if claim.last_word is None else claim.last_word,
            }
            for claim in reply.claims
        ]
        ttft_us = self._ttft_us.get(call, to_us(reply.ttft))
        duration_us = max(self._duration_us.get(call, to_us(reply.duration)), ttft_us)
        self._ledger.append(
            "llm",
            "llm.response",
            call=call,
            text=reply.text,
            tool_calls=[dataclasses.asdict(t) for t in tool_calls],
            claims=claims,
            ttft_us=ttft_us,
            duration_us=duration_us,
        )
        t0 = now_us()
        await sleep_until(t0, ttft_us)
        if words:
            self.register.replies[call] = words
        step = 3
        chunks = range(0, len(reply.text), step)
        for n, i in enumerate(chunks):
            await sleep_until(t0, ttft_us + (duration_us - ttft_us) * n // max(len(chunks) - 1, 1))
            await self.push_frame(LLMTextFrame(reply.text[i : i + step]))
        await sleep_until(t0, duration_us)
        if tool_calls:
            await self.run_function_calls(
                [
                    FunctionCallFromLLM(
                        function_name=t.name,
                        tool_call_id=t.call_id or "",
                        arguments=json.loads(t.arguments),
                        context=context,
                    )
                    for t in tool_calls
                ]
            )


class StampedTTS(TTSService):
    """The LiveKit adapter's stamped voice, as a Pipecat service: each sample's value is
    the stamp of the word it belongs to. Pipecat hands it one sentence at a time."""

    def __init__(
        self,
        ledger: Ledger,
        register: ReplyRegister,
        *,
        seconds_per_letter: float,
        ttfb: float,
        ttfb_us: dict[int, int],
        word_timestamps: bool = False,
    ) -> None:
        # A voice that says when each word starts lets Pipecat keep, of an interrupted
        # reply, the words whose time had come. One that does not gives it a sentence's
        # text only once the whole sentence has been written out.
        self._word_timestamps = word_timestamps
        super().__init__(
            push_text_frames=not word_timestamps,
            push_start_frame=True,
            push_stop_frames=True,
            sample_rate=SAMPLE_RATE,
            settings=TTSSettings(model=None, voice=None, language=None),
        )
        self._ledger, self._register = ledger, register
        self._letter_us, self._ttfb_us, self._ttfb_by_utterance = (
            to_us(seconds_per_letter),
            to_us(ttfb),
            ttfb_us,
        )
        self._utterances = 0
        self._open: dict[str, _Utterance] = {}
        self.synthesizing = 0
        self.table = StampTable(sample_rate=SAMPLE_RATE)

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        # Pipecat hands over one sentence at a time, and keeps one context for a reply.
        # An utterance is the context: its sentences are one utterance, as a reply's
        # stream is one in the LiveKit adapter.
        one = self._open.get(context_id)
        first = one is None
        if one is None:
            self._utterances += 1
            one = self._open[context_id] = _Utterance(
                number=self._utterances,
                started_us=self._ledger.now_us(),
                ttfb_us=self._ttfb_by_utterance.get(self._utterances, self._ttfb_us),
                eligible=set(self._register.replies),
            )
        t0, starts, pcm = now_us(), [], bytearray()
        for word in text.split():
            samples = (SAMPLE_RATE * letters(word) * self._letter_us + 500_000) // 1_000_000
            stamp = self.table.add(one.number, len(one.words), word, samples)
            starts.append((word, one.samples / SAMPLE_RATE))  # from the context's first audio
            one.words.append(word)
            one.stamps.append(stamp)
            one.word_samples.append(samples)
            one.samples += samples
            pcm.extend((array("h", [stamp]) * samples).tobytes())
        self.synthesizing += 1
        try:
            if first:
                await sleep_until(t0, one.ttfb_us)
            if pcm and self._word_timestamps:
                await self.add_word_timestamps(starts, context_id)
            if pcm:
                yield TTSAudioRawFrame(bytes(pcm), SAMPLE_RATE, 1, context_id=context_id)
        finally:
            self.synthesizing -= 1

    async def push_frame(
        self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ) -> None:
        if isinstance(frame, TTSStoppedFrame) and frame.context_id is not None:
            self.close(frame.context_id)  # the reply's last sentence has been synthesized
        await super().push_frame(frame, direction)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        if isinstance(frame, InterruptionFrame):
            self.close_all()  # nothing more of these replies will be synthesized
        await super().process_frame(frame, direction)

    def close_all(self) -> None:
        for context_id in list(self._open):
            self.close(context_id)

    def close(self, context_id: str) -> None:
        one = self._open.pop(context_id, None)
        if one is None:
            return
        text = " ".join(one.words)
        call, attribution, candidates = self._register.attribute(text, eligible=one.eligible)
        self._ledger.append(
            "world",
            "tts.synthesized",
            utterance=one.number,
            call=call,
            attribution=attribution,
            candidates=candidates,
            text=text,
            stamps=one.stamps,
            word_samples=one.word_samples,
            sample_rate=SAMPLE_RATE,
            ttfb_us=one.ttfb_us,
            started_us=one.started_us,
        )


@dataclass
class _Utterance:
    number: int
    started_us: int
    ttfb_us: int
    eligible: set[int]
    words: list[str] = field(default_factory=list)
    stamps: list[int] = field(default_factory=list)
    word_samples: list[int] = field(default_factory=list)
    samples: int = 0
