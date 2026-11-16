"""Scripted VAD, STT, LLM and TTS for a real AgentSession.

Built only on livekit-agents' public plugin base classes. Each provider writes what it
did to the ledger as a "world" (or "llm") event, so the checker never has to ask the
framework what happened at its edges.

Authors give times in seconds. Every provider converts them to integer microseconds once
and computes in integers; see ``earful.core.timebase``.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import itertools
from array import array
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from livekit import rtc
from livekit.agents import llm as lk_llm
from livekit.agents import stt as lk_stt
from livekit.agents import tts as lk_tts
from livekit.agents import utils
from livekit.agents import vad as lk_vad
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions

from ..core.ledger import Ledger
from ..core.timebase import to_seconds, to_us
from .clock import now_us


async def _sleep_until(t0_us: int, at_us: int) -> None:
    """Sleep until ``at_us`` microseconds after ``t0_us``."""
    delay_us = t0_us + at_us - now_us()
    if delay_us > 0:
        await asyncio.sleep(to_seconds(delay_us))


# --------------------------------------------------------------------------- user speech


@dataclass(frozen=True)
class UserSpeech:
    """One scripted user utterance, in seconds from the world's origin (first audio frame)."""

    start: float
    end: float
    transcript: str  # empty: VAD fires but STT stays silent (noise)
    stt_delay: float = 0.2
    expects_reply: bool | None = None  # None: true for a non-empty, non-backchannel turn
    backchannel: bool = False  # acknowledging, not taking the floor
    retires: tuple[str, ...] = ()  # intents this turn corrects or cancels

    @property
    def start_us(self) -> int:
        return to_us(self.start)

    @property
    def end_us(self) -> int:
        return to_us(self.end)

    @property
    def stt_delay_us(self) -> int:
        return to_us(self.stt_delay)

    @property
    def owes_reply(self) -> bool:
        if self.expects_reply is not None:
            return self.expects_reply
        return bool(self.transcript) and not self.backchannel


class WorldOrigin:
    """The instant the scripted user's clock starts: the first audio frame a scripted
    provider receives. Every scripted speech time is an offset from it.

    VAD and STT each start their own clock at their first frame; both must agree, or the
    scripted world is inconsistent and the run is invalid. On the virtual clock they must
    agree exactly; ``tolerance_us`` exists for real-clock control runs.
    """

    def __init__(self, ledger: Ledger, errors: list[str], *, tolerance_us: int = 0) -> None:
        self._ledger = ledger
        self._errors = errors
        self._tolerance_us = tolerance_us
        self.t0_us: int | None = None
        self.by: str | None = None

    def mark(self, by: str, at_us: int) -> None:
        if self.t0_us is None:
            self.t0_us, self.by = at_us, by
            self._ledger.append("world", "user.origin", by=by)
        elif abs(at_us - self.t0_us) > self._tolerance_us:
            self._errors.append(
                f"{by} started its clock at {at_us} us, {self.by} at {self.t0_us} us"
            )


def _check_speeches(speeches: list[UserSpeech]) -> list[UserSpeech]:
    speeches = sorted(speeches, key=lambda s: s.start)
    for prev, nxt in itertools.pairwise(speeches):
        if prev.end > nxt.start:
            raise ValueError("scripted user speeches overlap")
    return speeches


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
        super().__init__(capabilities=lk_vad.VADCapabilities(update_interval=0.1))
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
        for i, speech in enumerate(vad._speeches):
            await _sleep_until(t0, speech.start_us + vad._min_speech_us)
            self._emit(lk_vad.VADEventType.START_OF_SPEECH, speech, now_us() - t0)
            vad._ledger.append("world", "user.vad_start", speech=i)

            end_at = speech.end_us + vad._min_silence_us
            while now_us() - t0 < end_at - vad._min_speech_us * 2:
                await asyncio.sleep(to_seconds(vad._min_speech_us))
                self._emit(lk_vad.VADEventType.INFERENCE_DONE, speech, now_us() - t0)

            await _sleep_until(t0, end_at)
            self._emit(lk_vad.VADEventType.END_OF_SPEECH, speech, now_us() - t0)
            vad._ledger.append("world", "user.vad_end", speech=i)

        async for _ in self._input_ch:
            pass

    def _emit(self, type: lk_vad.VADEventType, speech: UserSpeech, at_us: int) -> None:
        """Report to the framework at ``at_us`` microseconds after the origin.

        Durations are differences of integers, converted once. A speech that has lasted
        500 000 us reports exactly 0.5 s, so the framework's own thresholds are decided by
        the scripted times and not by float rounding.
        """
        start, end = speech.start_us, speech.end_us
        speaking = at_us <= end
        values = {
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


class ScriptedSTT(lk_stt.STT):
    def __init__(self, ledger: Ledger, speeches: list[UserSpeech], origin: WorldOrigin) -> None:
        super().__init__(
            capabilities=lk_stt.STTCapabilities(streaming=True, interim_results=False)
        )
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


# --------------------------------------------------------------------------- LLM


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


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: str  # JSON text
    call_id: str


@dataclass(frozen=True)
class Claim:
    """A span of a reply that tells the user ``intent`` succeeded.

    Word indices are 0-based and inclusive; ``last_word=None`` means the reply's last word.
    """

    intent: str
    first_word: int = 0
    last_word: int | None = None


@dataclass(frozen=True)
class Reply:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    ttft: float = 0.1  # time to first token
    duration: float = 0.3  # total generation time
    claims: tuple[Claim, ...] = ()


def _words(text: str) -> list[str]:
    return text.split()


@dataclass
class ReplyRegister:
    """Every reply the scripted model gave, by call. Shared with the TTS for attribution."""

    replies: dict[int, list[str]] = field(default_factory=dict)
    synthesized: set[int] = field(default_factory=set)

    def attribute(self, text: str) -> tuple[int | None, str]:
        """Which reply a synthesized text belongs to.

        Only replies not yet synthesized are candidates. ``unique``: exactly one has this
        text. ``prefix``: none matches in full but exactly one starts with it (generation
        stopped early). ``ambiguous``: more than one candidate fits. ``none``: nothing fits.
        Earful never guesses between candidates.
        """
        words = _words(text)
        open_ = [c for c in self.replies if c not in self.synthesized]
        for kind, fits in (
            ("unique", lambda w: w == words),
            ("prefix", lambda w: len(w) > len(words) and w[: len(words)] == words),
        ):
            hits = [c for c in open_ if words and fits(self.replies[c])]
            if len(hits) == 1:
                self.synthesized.add(hits[0])
                return hits[0], kind
            if len(hits) > 1:
                return None, "ambiguous"
        return None, "none"


# A model policy: a pure function of the context the framework presents. It sees item
# summaries, not framework objects, so the same policy can run against any adapter.
Policy = Callable[[list[dict[str, Any]]], Reply]


class ScriptedLLM(lk_llm.LLM):
    """Deterministic model. Also the memory witness: it records every context it is shown."""

    def __init__(self, ledger: Ledger, policy: Policy, errors: list[str]) -> None:
        super().__init__()
        self._ledger = ledger
        self._policy = policy
        self._errors = errors
        self._calls = 0
        self.register = ReplyRegister()

    def chat(
        self,
        *,
        chat_ctx: lk_llm.ChatContext,
        tools: Any = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        **kwargs: Any,
    ) -> lk_llm.LLMStream:
        return _ScriptedLLMStream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options)


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
                model._errors.append(f"call {call}: claim {claim} does not fit reply {reply.text!r}")
                continue
            claims.append({"intent": claim.intent, "first_word": claim.first_word, "last_word": last})
        ttft_us, duration_us = to_us(reply.ttft), to_us(reply.duration)
        if words:
            model.register.replies[call] = words
        model._ledger.append(
            "llm",
            "llm.response",
            call=call,
            text=reply.text,
            tool_calls=[dataclasses.asdict(t) for t in reply.tool_calls],
            claims=claims,
            ttft_us=ttft_us,
            duration_us=duration_us,
        )

        t0 = now_us()
        await _sleep_until(t0, ttft_us)
        step = 3
        for i in range(0, len(reply.text), step):
            self._chunk(call, content=reply.text[i : i + step])
        await _sleep_until(t0, duration_us)
        if reply.tool_calls:
            self._chunk(
                call,
                tool_calls=[
                    lk_llm.FunctionToolCall(name=t.name, arguments=t.arguments, call_id=t.call_id)
                    for t in reply.tool_calls
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


# --------------------------------------------------------------------------- TTS

MAX_STAMP = 32767  # int16; 0 is reserved for "no word"


@dataclass
class StampTable:
    """Every word the TTS ever synthesized, by stamp. Shared with the sink."""

    sample_rate: int
    words: dict[int, dict[str, Any]] = field(default_factory=dict)

    def add(self, utterance: int, index: int, text: str, samples: int) -> int:
        stamp = len(self.words) + 1
        if stamp > MAX_STAMP:
            raise OverflowError("more than 32767 words synthesized in one run")
        self.words[stamp] = {"utterance": utterance, "index": index, "text": text, "samples": samples}
        return stamp


class StampedTTS(lk_tts.TTS):
    """Emits PCM in which every sample's value is the stamp of the word it belongs to.

    The audio itself carries the ground truth, so the sink needs no side channel and no
    framework estimate to know which words it consumed.
    """

    def __init__(
        self,
        ledger: Ledger,
        register: ReplyRegister,
        *,
        sample_rate: int = 24000,
        seconds_per_word: float = 0.3,
        ttfb: float = 0.2,
    ) -> None:
        super().__init__(
            capabilities=lk_tts.TTSCapabilities(streaming=True),
            sample_rate=sample_rate,
            num_channels=1,
        )
        self._ledger = ledger
        self._register = register
        self._ttfb_us = to_us(ttfb)
        # Whole samples per word; the ledger records this number, not the requested seconds.
        self._samples_per_word = (sample_rate * to_us(seconds_per_word) + 500_000) // 1_000_000
        if self._samples_per_word < 1:
            raise ValueError("seconds_per_word is shorter than one audio sample")
        self._utterances = 0
        self.table = StampTable(sample_rate=sample_rate)

    def synthesize(self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS) -> Any:
        return _StampedChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    def stream(self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS) -> Any:
        return _StampedSynthesizeStream(tts=self, conn_options=conn_options)

    def _render(self, text: str) -> bytes:
        """PCM for ``text``; registers its words and records the synthesis."""
        self._utterances += 1
        utterance = self._utterances
        per_word = self._samples_per_word
        pcm = array("h")
        stamps = []
        for index, word in enumerate(text.split()):
            stamp = self.table.add(utterance, index, word, per_word)
            stamps.append(stamp)
            pcm.extend([stamp] * per_word)
        call, attribution = self._register.attribute(text)
        self._ledger.append(
            "world",
            "tts.synthesized",
            utterance=utterance,
            call=call,
            attribution=attribution,
            text=text,
            stamps=stamps,
            samples_per_word=per_word,
            sample_rate=self.sample_rate,
            ttfb_us=self._ttfb_us,
        )
        return pcm.tobytes()


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
        pcm = tts._render(self._input_text)
        if pcm:
            await asyncio.sleep(to_seconds(tts._ttfb_us))
            _push_pcm(output_emitter, pcm, tts.sample_rate)
        output_emitter.flush()


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
        text = ""
        async for data in self._input_ch:
            if isinstance(data, str):
                text += data
                continue
            if not text.strip():
                text = ""
                continue
            if "_mark_started" in dir(self):
                self._mark_started()
            pcm = tts._render(text)
            text = ""
            await asyncio.sleep(to_seconds(tts._ttfb_us))
            output_emitter.start_segment(segment_id=utils.shortuuid("earful_segment_"))
            _push_pcm(output_emitter, pcm, tts.sample_rate)
            output_emitter.flush()


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
    "summarize_item",
]
