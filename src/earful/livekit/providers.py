"""Scripted VAD, STT, LLM and TTS for a real AgentSession.
Authors give times in seconds; the providers compute in integer microseconds."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from typing import Any, NoReturn

from livekit import rtc
from livekit.agents import llm as lk_llm
from livekit.agents import stt as lk_stt
from livekit.agents import tts as lk_tts
from livekit.agents import utils
from livekit.agents import vad as lk_vad

from ..core.ledger import Ledger
from ..core.timebase import to_seconds, to_us
from ..scripted.clock import now_us
from ..scripted.model import Claim, Policy, Reply, ReplyRegister, ToolCall, _words
from ..scripted.voice import SHORTEST, StampTable, letters
from ..scripted.world import UserSpeech, WorldOrigin, _check_speeches, vad_segments
from ..scripted.world import sleep_until as _sleep_until
from ._compat import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions


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

    def stream(self) -> lk_vad.VADStream:
        return _ScriptedVADStream(self)


# VADEvent grew fields across releases; only pass the ones this version defines.
_VAD_EVENT_FIELDS = {f.name for f in dataclasses.fields(lk_vad.VADEvent)}


class _ScriptedVADStream(lk_vad.VADStream):
    async def _main_task(self) -> None:
        vad = self._vad
        assert isinstance(vad, ScriptedVAD)
        if not vad._speeches:
            return

        async for frame in self._input_ch:
            if isinstance(frame, rtc.AudioFrame):
                break
        else:
            return

        t0 = now_us()
        vad._origin.mark("vad", t0)
        for segment in vad_segments(vad._speeches, vad._min_silence_us):
            first, last = segment[0], segment[-1]
            start_us, end_us = vad._speeches[first].start_us, vad._speeches[last].end_us
            await _sleep_until(t0, start_us + vad._min_speech_us)
            self._emit(lk_vad.VADEventType.START_OF_SPEECH, start_us, end_us, now_us() - t0)
            vad._ledger.append("world", "user.vad_start", speech=first)

            end_at = end_us + vad._min_silence_us
            while now_us() - t0 < end_at - vad._min_speech_us * 2:
                await asyncio.sleep(to_seconds(vad._min_speech_us))
                self._emit(lk_vad.VADEventType.INFERENCE_DONE, start_us, end_us, now_us() - t0)

            await _sleep_until(t0, end_at)
            self._emit(lk_vad.VADEventType.END_OF_SPEECH, start_us, end_us, now_us() - t0)
            vad._ledger.append("world", "user.vad_end", speech=last)

        async for _ in self._input_ch:
            pass

    def _emit(self, type: lk_vad.VADEventType, start: int, end: int, at_us: int) -> None:
        # Durations are integer differences converted once: 500 000 us reports exactly 0.5 s.
        speaking = at_us <= end
        values: dict[str, Any] = {
            "type": type,
            "samples_index": 0,
            "timestamp": to_seconds(at_us),
            "speech_duration": to_seconds(min(at_us, end) - start),
            "silence_duration": to_seconds(max(0, at_us - end)),
            "raw_accumulated_speech": to_seconds(at_us - start) if speaking else 0.0,
            "raw_accumulated_silence": 0.0 if speaking else to_seconds(at_us - end),
        }
        kwargs = {k: v for k, v in values.items() if k in _VAD_EVENT_FIELDS}
        self._event_ch.send_nowait(lk_vad.VADEvent(**kwargs))


def _speech_data(text: str) -> lk_stt.SpeechData:
    language: Any = ""
    code = getattr(lk_stt, "LanguageCode", None) or getattr(
        __import__("livekit.agents", fromlist=["LanguageCode"]), "LanguageCode", None
    )
    if code is not None:
        language = code("")
    return lk_stt.SpeechData(text=text, language=language)


# The type argument names a provider's extra events: none here (NoReturn is 3.10's Never).
class ScriptedSTT(lk_stt.STT[NoReturn]):
    def __init__(self, ledger: Ledger, speeches: list[UserSpeech], origin: WorldOrigin) -> None:
        super().__init__(capabilities=lk_stt.STTCapabilities(streaming=True, interim_results=False))
        self._ledger = ledger
        self._origin = origin
        self._speeches = _check_speeches(speeches)
        self.done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        if not self._speeches:
            self.done.set_result(None)

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
    async def _run(self) -> None:
        stt = self._stt
        assert isinstance(stt, ScriptedSTT)
        if stt._speeches and not stt.done.done():
            await self._input_ch.recv()  # clock starts at the first audio frame
            t0 = now_us()
            stt._origin.mark("stt", t0)
            for i, speech in enumerate(stt._speeches):
                final_at = speech.end_us + speech.stt_delay_us
                if speech.transcript:
                    await _sleep_until(t0, speech.end_us + speech.stt_delay_us // 2)
                    interim = " ".join(speech.transcript.split()[:2])
                    self._send(interim, final=False)
                    await _sleep_until(t0, final_at)
                    self._send(speech.transcript, final=True)
                    stt._ledger.append(
                        "world", "user.transcript_final", speech=i, text=speech.transcript
                    )
                else:
                    await _sleep_until(t0, final_at)
            with contextlib.suppress(asyncio.InvalidStateError):
                stt.done.set_result(None)

        async for _ in self._input_ch:
            pass

    def _send(self, text: str, *, final: bool) -> None:
        kind = (
            lk_stt.SpeechEventType.FINAL_TRANSCRIPT
            if final
            else lk_stt.SpeechEventType.INTERIM_TRANSCRIPT
        )
        self._event_ch.send_nowait(lk_stt.SpeechEvent(type=kind, alternatives=[_speech_data(text)]))


def summarize_item(item: Any) -> dict[str, Any]:
    """A chat-context item reduced to the fields contracts care about."""
    out: dict[str, Any] = {"type": item.type, "id": getattr(item, "id", None)}
    if item.type == "message":
        out["role"] = item.role
        out["text"] = item.text_content
        out["interrupted"] = bool(getattr(item, "interrupted", False))
    elif item.type == "function_call":
        out["name"] = item.name
        out["arguments"] = item.arguments
        out["call_id"] = item.call_id
    elif item.type == "function_call_output":
        out["name"] = getattr(item, "name", None)
        out["output"] = item.output
        out["call_id"] = item.call_id
        out["is_error"] = bool(getattr(item, "is_error", False))
    return out


class ScriptedLLM(lk_llm.LLM[NoReturn]):
    """Deterministic model. Also the memory witness: it records every context it is shown."""

    def __init__(
        self,
        ledger: Ledger,
        policy: Policy,
        errors: list[str],
        *,
        ttft_us: dict[int, int] | None = None,
        duration_us: dict[int, int] | None = None,
    ) -> None:
        super().__init__()
        self._ledger = ledger
        self._policy = policy
        self._errors = errors
        # Model call (from 1) -> a delay the scenario declared, in place of the reply's own.
        self._ttft_us, self._duration_us = dict(ttft_us or {}), dict(duration_us or {})
        self._calls = 0
        self._call_ids: set[str] = set()
        self.register = ReplyRegister()

    def chat(
        self,
        *,
        chat_ctx: lk_llm.ChatContext,
        tools: Any = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        **kwargs: Any,
    ) -> lk_llm.LLMStream:
        return _ScriptedLLMStream(
            self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options
        )


class _ScriptedLLMStream(lk_llm.LLMStream):
    async def _run(self) -> None:
        model = self._llm
        assert isinstance(model, ScriptedLLM)
        model._calls += 1
        call = model._calls

        items = [summarize_item(i) for i in self.chat_ctx.items]
        model._ledger.append("llm", "llm.request", call=call, items=items)
        try:
            reply = model._policy(items)
        except Exception as exc:
            # The framework would handle this as a model failure. It is a broken scenario.
            model._errors.append(f"policy raised on call {call}: {type(exc).__name__}: {exc}")
            raise
        words = _words(reply.text)
        claims = []
        for claim in reply.claims:
            last = len(words) - 1 if claim.last_word is None else claim.last_word
            if not 0 <= claim.first_word <= last < len(words):
                model._errors.append(
                    f"call {call}: claim {claim} does not fit reply {reply.text!r}"
                )
                continue
            claims.append(
                {"intent": claim.intent, "first_word": claim.first_word, "last_word": last}
            )
        call_ids = [
            t.call_id if t.call_id is not None else f"call-{call}-{n}"
            for n, t in enumerate(reply.tool_calls, 1)
        ]
        tool_calls = [
            t if t.call_id is not None else dataclasses.replace(t, call_id=call_id)
            for t, call_id in zip(reply.tool_calls, call_ids)
        ]
        for call_id in call_ids:
            # A call id links a tool invocation to its result; a model never repeats one.
            if call_id in model._call_ids:
                model._errors.append(f"call {call}: the policy reused tool call id {call_id!r}")
            model._call_ids.add(call_id)
        ttft_us = model._ttft_us.get(call, to_us(reply.ttft))
        # Generation cannot end before its first token.
        duration_us = max(model._duration_us.get(call, to_us(reply.duration)), ttft_us)
        model._ledger.append(
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
        await _sleep_until(t0, ttft_us)
        # A text can be this reply only from its first token on.
        if words:
            model.register.replies[call] = words
        step = 3
        chunks = range(0, len(reply.text), step)
        for n, i in enumerate(chunks):
            # Absolute integer deadlines avoid accumulating float rounding or scheduler lag.
            at_us = ttft_us + (duration_us - ttft_us) * n // max(len(chunks) - 1, 1)
            await _sleep_until(t0, at_us)
            self._chunk(call, content=reply.text[i : i + step])
        await _sleep_until(t0, duration_us)
        if tool_calls:
            self._chunk(
                call,
                tool_calls=[
                    lk_llm.FunctionToolCall(name=t.name, arguments=t.arguments, call_id=call_id)
                    for t, call_id in zip(tool_calls, call_ids)
                ],
            )

    def _chunk(self, call: int, *, content: str | None = None, tool_calls: Any = None) -> None:
        self._event_ch.send_nowait(
            lk_llm.ChatChunk(
                id=f"scripted-llm-{call}",
                delta=lk_llm.ChoiceDelta(
                    role="assistant", content=content, tool_calls=tool_calls or []
                ),
            )
        )


class StampedTTS(lk_tts.TTS[NoReturn]):
    """Emits PCM from which the sink reads the stamp of the word each sample belongs to.
    A word's audio lasts ``seconds_per_letter`` for each of its letters."""

    def __init__(
        self,
        ledger: Ledger,
        register: ReplyRegister,
        *,
        sample_rate: int = 24000,
        seconds_per_letter: float = 0.06,
        ttfb: float = 0.2,
        ttfb_us: dict[int, int] | None = None,
    ) -> None:
        super().__init__(
            capabilities=lk_tts.TTSCapabilities(streaming=True),
            sample_rate=sample_rate,
            num_channels=1,
        )
        self._ledger = ledger
        self._register = register
        self._ttfb_us = to_us(ttfb)
        # Utterance (from 1) -> a time to first audio the scenario declared for it.
        self._ttfb_by_utterance = dict(ttfb_us or {})
        self._letter_us = to_us(seconds_per_letter)
        if (sample_rate * self._letter_us + 500_000) // 1_000_000 < SHORTEST:
            raise ValueError(f"seconds_per_letter is shorter than {SHORTEST} audio samples")
        self._utterances = 0
        self.table = StampTable(sample_rate=sample_rate)

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> Any:
        return _StampedChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    def stream(self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS) -> Any:
        return _StampedSynthesizeStream(tts=self, conn_options=conn_options)

    def _begin(self) -> _Synthesis:
        self._utterances += 1
        return _Synthesis(self, self._utterances)


class _Synthesis:
    # One utterance, recorded once at its end. Only replies known at its start are eligible.
    def __init__(self, tts: StampedTTS, utterance: int) -> None:
        self.tts, self.utterance = tts, utterance
        self.started_us = tts._ledger.now_us()
        self.t0_us = now_us()
        self.ttfb_us = tts._ttfb_by_utterance.get(utterance, tts._ttfb_us)
        self.eligible = set(tts._register.replies)
        self.words: list[str] = []
        self.stamps: list[int] = []
        self.word_samples: list[int] = []

    def render(self, word: str) -> bytes:
        tts = self.tts
        samples = (tts.sample_rate * letters(word) * tts._letter_us + 500_000) // 1_000_000
        stamp = tts.table.add(self.utterance, len(self.words), word, samples)
        self.words.append(word)
        self.stamps.append(stamp)
        self.word_samples.append(samples)
        return tts.table.audio(stamp).tobytes()

    def finish(self) -> None:
        text = " ".join(self.words)
        call, attribution, candidates = self.tts._register.attribute(text, eligible=self.eligible)
        self.tts._ledger.append(
            "world",
            "tts.synthesized",
            utterance=self.utterance,
            call=call,
            attribution=attribution,
            candidates=candidates,
            text=text,
            stamps=self.stamps,
            word_samples=self.word_samples,
            sample_rate=self.tts.sample_rate,
            ttfb_us=self.ttfb_us,
            started_us=self.started_us,
        )


def _push_pcm(emitter: Any, pcm: bytes, sample_rate: int) -> None:
    chunk = (sample_rate // 100) * 2  # 10 ms of int16 mono
    for i in range(0, len(pcm), chunk):
        emitter.push(pcm[i : i + chunk])


class _StampedChunkedStream(lk_tts.ChunkedStream):
    async def _run(self, output_emitter: Any) -> None:
        tts = self._tts
        assert isinstance(tts, StampedTTS)
        output_emitter.initialize(
            request_id=utils.shortuuid("earful_tts_"),
            sample_rate=tts.sample_rate,
            num_channels=1,
            mime_type="audio/pcm",
        )
        synthesis = tts._begin()
        try:
            pcm = b"".join(synthesis.render(word) for word in self._input_text.split())
            if pcm:
                await _sleep_until(synthesis.t0_us, synthesis.ttfb_us)
                _push_pcm(output_emitter, pcm, tts.sample_rate)
            output_emitter.flush()
        finally:
            synthesis.finish()


class _StampedSynthesizeStream(lk_tts.SynthesizeStream):
    async def _run(self, output_emitter: Any) -> None:
        tts = self._tts
        assert isinstance(tts, StampedTTS)
        output_emitter.initialize(
            request_id=utils.shortuuid("earful_tts_"),
            sample_rate=tts.sample_rate,
            num_channels=1,
            mime_type="audio/pcm",
            stream=True,
        )
        pending = ""
        synthesis: _Synthesis | None = None

        async def push(words: list[str]) -> None:
            nonlocal synthesis
            for word in words:
                first = synthesis is None
                if synthesis is None:
                    synthesis = tts._begin()
                    if "_mark_started" in dir(self):
                        self._mark_started()
                pcm = synthesis.render(word)
                if first:
                    await _sleep_until(synthesis.t0_us, synthesis.ttfb_us)
                    output_emitter.start_segment(segment_id=utils.shortuuid("earful_segment_"))
                _push_pcm(output_emitter, pcm, tts.sample_rate)

        try:
            async for data in self._input_ch:
                if isinstance(data, str):
                    pending += data
                    words = pending.split()
                    pending = words.pop() if words and not pending[-1].isspace() else ""
                    await push(words)
                else:
                    await push(pending.split())
                    pending = ""
                    if synthesis is not None:
                        synthesis.finish()
                        synthesis = None
                        output_emitter.end_segment()
        finally:
            if synthesis is not None:
                synthesis.finish()


__all__ = [
    "Claim",
    "Policy",
    "Reply",
    "ReplyRegister",
    "ScriptedLLM",
    "ScriptedSTT",
    "ScriptedVAD",
    "StampTable",
    "StampedTTS",
    "ToolCall",
    "UserSpeech",
    "WorldOrigin",
    "letters",
    "summarize_item",
    "vad_segments",
]
