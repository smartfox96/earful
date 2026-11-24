"""Scripted VAD, STT, LLM and TTS for a real AgentSession.
Authors give times in seconds; the providers compute in integer microseconds."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
from collections.abc import Callable, Coroutine
from contextvars import ContextVar, Token
from typing import Any, NoReturn, ParamSpec, TypeVar

from livekit import rtc
from livekit.agents import llm as lk_llm
from livekit.agents import stt as lk_stt
from livekit.agents import tts as lk_tts
from livekit.agents import utils
from livekit.agents import vad as lk_vad

from ..core.ledger import Ledger
from ..core.timebase import to_seconds, to_us
from ..scripted.clock import now_us
from ..scripted.model import Claim, Policy, Reply, ReplyRegister, ToolCall, _words, misfit
from ..scripted.parties.model import fragments, reply_for
from ..scripted.parties.stt import transcripts
from ..scripted.parties.vad import detections
from ..scripted.parties.voice import Synthesis as _Synthesis
from ..scripted.voice import SHORTEST, StampTable, letters
from ..scripted.world import UserSpeech, WorldOrigin, _check_speeches, vad_segments
from ..scripted.world import sleep_until as _sleep_until
from . import seams
from .seams import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions, summarize_item
from .tools import unwitnessed

P = ParamSpec("P")
R = TypeVar("R")

# The harness errors of the run in progress. Tasks the framework starts inherit it.
_errors: ContextVar[list[str] | None] = ContextVar("earful_harness_errors", default=None)


def record_errors_in(errors: list[str] | None) -> Token[list[str] | None]:
    """Record what Earful's own providers raise from here on in ``errors``."""
    return _errors.set(errors)


def stop_recording_errors(token: Token[list[str] | None]) -> None:
    _errors.reset(token)


def _record(what: str, exc: Exception) -> None:
    errors, said = _errors.get(), f"{type(exc).__name__}: {exc}"
    # A policy that raised is already there, recorded by the scripted model.
    if errors is not None and not (errors and errors[-1].endswith(said)):
        errors.append(f"the scripted {what} raised {said}")


def scripted(
    what: str,
) -> Callable[[Callable[P, Coroutine[Any, Any, R]]], Callable[P, Coroutine[Any, Any, R]]]:
    """The framework handles what a provider raises as that provider failing. In Earful's
    own providers it is a broken simulation, so it is recorded as a harness error first."""

    def guard(run: Callable[P, Coroutine[Any, Any, R]]) -> Callable[P, Coroutine[Any, Any, R]]:
        @functools.wraps(run)
        async def guarded(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return await run(*args, **kwargs)
            except Exception as exc:
                _record(what, exc)
                raise

        return guarded

    return guard


def scripted_call(what: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """``scripted`` for a method that is not a coroutine."""

    def guard(call: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(call)
        def guarded(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return call(*args, **kwargs)
            except Exception as exc:
                _record(what, exc)
                raise

        return guarded

    return guard


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
        # The framework opens a new stream for each agent it hands over to.
        self._streams = 0
        self._starts: set[int] = set()  # segments whose start is in the ledger
        self._ends: set[int] = set()  # segments whose end has been reported

    def stream(self) -> lk_vad.VADStream:
        return _ScriptedVADStream(self)


# VADEvent grew fields across releases; only pass the ones this version defines.
_VAD_EVENT_FIELDS = {f.name for f in dataclasses.fields(lk_vad.VADEvent)}


class _ScriptedVADStream(lk_vad.VADStream):
    @scripted("VAD")
    async def _main_task(self) -> None:
        vad = seams.vad_of(self)
        assert isinstance(vad, ScriptedVAD)
        if not vad._speeches:
            return
        vad._streams += 1
        if vad._streams == 1:  # the scripted user's clock starts at the first audio frame
            async for frame in seams.stream_input(self):
                if isinstance(frame, rtc.AudioFrame):
                    break
            else:
                return
            vad._origin.mark("vad", now_us())
        assert vad._origin.t0_us is not None
        t0 = vad._origin.t0_us
        opened = now_us() - t0
        events = detections(
            vad._speeches,
            vad._min_speech_us,
            vad._min_silence_us,
            opened=opened,
            ended=vad._ends,
            ticks=True,
        )
        kinds = {
            "start": lk_vad.VADEventType.START_OF_SPEECH,
            "inference": lk_vad.VADEventType.INFERENCE_DONE,
            "end": lk_vad.VADEventType.END_OF_SPEECH,
        }
        for event in events:
            await _sleep_until(t0, event.at_us)
            self._emit(kinds[event.kind], event.spans, now_us() - t0)
            if event.kind == "start" and event.segment not in vad._starts:
                vad._starts.add(event.segment)
                vad._ledger.append("world", "user.vad_start", speech=event.speech)
            if event.kind == "end":
                vad._ends.add(event.segment)
                vad._ledger.append("world", "user.vad_end", speech=event.speech)
        async for _ in seams.stream_input(self):
            pass

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
    @scripted("recognizer")
    async def _run(self) -> None:
        stt = seams.recognizer_of(self)
        assert isinstance(stt, ScriptedSTT)
        if stt._speeches and not stt.done.done():
            try:
                await self._script(stt)
            except Exception:
                # The scenario ends on the last transcript: a run must not wait for it now.
                with contextlib.suppress(asyncio.InvalidStateError):
                    stt.done.set_result(None)
                raise

        async for _ in seams.stream_input(self):
            pass

    async def _script(self, stt: ScriptedSTT) -> None:
        await seams.stream_input(self).recv()  # clock starts at the first audio frame
        t0 = now_us()
        stt._origin.mark("stt", t0)
        for event in transcripts(stt._speeches, chronological=False):
            await _sleep_until(t0, event.at_us)
            if event.text:
                self._send(event.text, final=event.final)
                if event.final:
                    stt._ledger.append(
                        "world", "user.transcript_final", speech=event.speech, text=event.text
                    )
        with contextlib.suppress(asyncio.InvalidStateError):
            stt.done.set_result(None)

    def _send(self, text: str, *, final: bool) -> None:
        kind = (
            lk_stt.SpeechEventType.FINAL_TRANSCRIPT
            if final
            else lk_stt.SpeechEventType.INTERIM_TRANSCRIPT
        )
        seams.send_event(
            self, lk_stt.SpeechEvent(type=kind, alternatives=[seams.speech_data(text)])
        )


def starting_history(items: list[Any]) -> list[dict[str, Any]]:
    """The items an agent's context holds before the run, for the header. Their identifiers
    are random and the header is not normalized, so each is null."""
    return [{**summarize_item(item), "id": None} for item in items]


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
        parallel_tool_calls: Any = None,
        tool_choice: Any = None,
        **kwargs: Any,
    ) -> lk_llm.LLMStream:
        stream = _ScriptedLLMStream(
            self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options
        )
        stream.asked = (tool_choice, parallel_tool_calls)
        return stream


def _unfit(tools: list[Any], calls: tuple[ToolCall, ...]) -> list[str]:
    """What is wrong with each scripted call that the tools offered for it cannot take."""
    kinds = (lk_llm.FunctionTool, lk_llm.RawFunctionTool)
    offered = {tool.info.name: tool for tool in tools if isinstance(tool, kinds)}
    out: list[str | None] = []
    for call in calls:
        tool = offered.get(call.name)
        if tool is None:
            out.append(
                f"the scripted model called {call.name!r}, which the agent does not offer; "
                f"it offers {sorted(offered)}"
            )
        elif isinstance(tool, lk_llm.FunctionTool):  # a raw tool's schema is its author's own
            schema = lk_llm.utils.build_legacy_openai_schema(tool, internally_tagged=True)
            taken = schema["parameters"]
            out.append(misfit(call, taken.get("properties", {}), taken.get("required", ())))
    return [wrong for wrong in out if wrong is not None]


def _unserved(tool_choice: Any, parallel: Any, calls: tuple[ToolCall, ...]) -> str | None:
    """How the scripted reply breaks what the request allowed, which a real model keeps to."""
    names = [call.name for call in calls]
    if tool_choice == "none" and names:
        return (
            f"the request allowed no tool call (tool_choice='none'), and the scripted model "
            f"called {names}. livekit-agents asks so once max_tool_steps rounds of tool calls "
            "have run, and while the session closes: script a reply without a call for that "
            "request"
        )
    if tool_choice == "required" and not names:
        return (
            "the request required a tool call (tool_choice='required'), and the scripted "
            "model made none"
        )
    if isinstance(tool_choice, dict):
        wanted = tool_choice.get("function", {}).get("name")
        if not names or any(name != wanted for name in names):
            return (
                f"the request asked for a call of {wanted!r}, and the scripted model called {names}"
            )
    if parallel is False and len(names) > 1:
        return f"the request allowed one tool call at a time, and the scripted model called {names}"
    return None


class _ScriptedLLMStream(lk_llm.LLMStream):
    asked: tuple[Any, Any] = (None, None)  # tool_choice and parallel_tool_calls

    @scripted("model")
    async def _run(self) -> None:
        model = seams.model_of(self)
        assert isinstance(model, ScriptedLLM)
        model._calls += 1
        call = model._calls

        items = [summarize_item(i) for i in self.chat_ctx.items]
        model._ledger.append("llm", "llm.request", call=call, items=items)
        missed = unwitnessed(list(self.tools))
        if missed:
            model._errors.append(f"call {call}: the model is offered {missed} without the witness")
        reply = reply_for(model._policy, items, model._errors, call)
        model._errors += [f"call {call}: {wrong}" for wrong in _unfit(self.tools, reply.tool_calls)]
        unserved = _unserved(*self.asked, reply.tool_calls)
        if unserved is not None:
            model._errors.append(f"call {call}: {unserved}")
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
        async for text in fragments(
            model._ledger,
            call,
            reply,
            tool_calls,
            claims,
            model._ttft_us,
            model._duration_us,
            model.register,
        ):
            self._chunk(call, content=text)
        if tool_calls:
            self._chunk(
                call,
                tool_calls=[
                    lk_llm.FunctionToolCall(name=t.name, arguments=t.arguments, call_id=call_id)
                    for t, call_id in zip(tool_calls, call_ids)
                ],
            )

    def _chunk(self, call: int, *, content: str | None = None, tool_calls: Any = None) -> None:
        seams.send_event(
            self,
            lk_llm.ChatChunk(
                id=f"scripted-llm-{call}",
                delta=lk_llm.ChoiceDelta(
                    role="assistant", content=content, tool_calls=tool_calls or []
                ),
            ),
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
        return _Synthesis(
            self._utterances,
            self._ledger,
            self.table,
            self._register,
            self.sample_rate,
            self._letter_us,
            self._ttfb_by_utterance.get(self._utterances, self._ttfb_us),
        )


def _push_pcm(emitter: Any, pcm: bytes, sample_rate: int) -> None:
    chunk = (sample_rate // 100) * 2  # 10 ms of int16 mono
    for i in range(0, len(pcm), chunk):
        emitter.push(pcm[i : i + chunk])


class _StampedChunkedStream(lk_tts.ChunkedStream):
    @scripted("voice")
    async def _run(self, output_emitter: Any) -> None:
        tts = seams.voice_of(self)
        assert isinstance(tts, StampedTTS)
        output_emitter.initialize(
            request_id=utils.shortuuid("earful_tts_"),
            sample_rate=tts.sample_rate,
            num_channels=1,
            mime_type="audio/pcm",
        )
        synthesis = tts._begin()
        try:
            pcm = b"".join(synthesis.render(word) for word in seams.input_text(self).split())
            if pcm:
                await _sleep_until(synthesis.t0_us, synthesis.ttfb_us)
                _push_pcm(output_emitter, pcm, tts.sample_rate)
            output_emitter.flush()
        finally:
            synthesis.finish()


class _StampedSynthesizeStream(lk_tts.SynthesizeStream):
    @scripted("voice")
    async def _run(self, output_emitter: Any) -> None:
        tts = seams.voice_of(self)
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
                    seams.mark_started(self)
                pcm = synthesis.render(word)
                if first:
                    await _sleep_until(synthesis.t0_us, synthesis.ttfb_us)
                    output_emitter.start_segment(segment_id=utils.shortuuid("earful_segment_"))
                _push_pcm(output_emitter, pcm, tts.sample_rate)

        try:
            async for data in seams.stream_input(self):
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
