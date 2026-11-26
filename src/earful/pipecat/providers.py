"""Scripted speech, transcription, model responses and stamped audio for Pipecat."""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any, cast

from pipecat.adapters.llm.schemas.direct_function import DirectFunctionWrapper
from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
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
from ..scripted.model import Policy, ReplyRegister, ToolCall, misfit
from ..scripted.parties.activity import Activity, Schedule
from ..scripted.parties.model import claim_spans, fragments, identified_calls, reply_for
from ..scripted.parties.stt import Recognition
from ..scripted.parties.vad import detections
from ..scripted.parties.voice import Synthesis
from ..scripted.voice import SHORTEST, StampTable
from ..scripted.world import UserSpeech, WorldOrigin, sleep_until
from . import seams
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
        self.activity = Schedule()
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
        for event in detections(self._speeches, confirm_us, silence_us):
            await sleep_until(t0, event.at_us)
            if self._left.is_set():
                return
            self._ledger.append("world", f"user.vad_{event.kind}", speech=event.speech)
            if event.kind == "start":
                await self.push_frame(
                    VADUserStartedSpeakingFrame(start_secs=self._start_secs, timestamp=time.time())
                )
            else:
                await self.push_frame(
                    VADUserStoppedSpeakingFrame(stop_secs=self._stop_secs, timestamp=time.time())
                )
        self.activity.finish()


class ScriptedSTT(STTService):
    """Delivers each speech's transcript at its scripted time. Hears no audio."""

    def __init__(
        self, ledger: Ledger, speeches: list[UserSpeech], origin: WorldOrigin, left: asyncio.Event
    ) -> None:
        # The longest the framework should wait for a transcript after the VAD's stop.
        super().__init__(ttfs_p99_latency=0.0, settings=STTSettings(model=None, language=None))
        self._ledger, self._origin, self._left = ledger, origin, left
        self.script = Recognition(sorted(speeches, key=lambda s: s.start), chronological=True)

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
        async for event in self.script.events(t0):
            if self._left.is_set():
                return
            if not event.text:
                continue
            if not event.final:
                await self.push_frame(
                    InterimTranscriptionFrame(event.text, "user", time_now_iso8601())
                )
            else:
                self._ledger.append(
                    "world", "user.transcript_final", speech=event.speech, text=event.text
                )
                await self.push_frame(
                    TranscriptionFrame(event.text, "user", time_now_iso8601(), finalized=True)
                )


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
        self._explicit: dict[str, Any] = {}
        self._witnessed: list[object] = []  # handlers the witness is already around
        self.listed: frozenset[str] | None = None  # the functions the header names, once written
        self.activity = Activity()
        self.register = ReplyRegister()

    def witness_tools(self, advertised: Any = None) -> None:
        """Put the witness around every function the application has registered, however
        it did so. ``advertised`` is a context's tools, whose handlers Pipecat registers
        when it first sees the context."""
        functions, cancel = seams.registered(self), seams.cancel_tools(self)
        if advertised is not None:
            self._explicit = {name: item.handler for name, item in functions.items() if name}
            seams.sync_handlers(self, advertised)
        for name, item in functions.items():
            if name in cancel or item.handler in self._witnessed:
                continue  # Pipecat's own tool, or one already wrapped
            if isinstance(item.handler, DirectFunctionWrapper):
                item.handler.function = self._witness.wrap(item.handler.function, direct=True)
                self._witnessed.append(item.handler)
            else:
                item.handler = self._witness.wrap(item.handler)
                self._witnessed.append(item.handler)
        names = {name or "*" for name in functions if name not in cancel}
        if self.listed is not None:
            new = sorted(names - self.listed)
            if new:
                self._witness.refusal = (
                    f"the bot registered {new[0]!r} after bot() returned, so the run's header "
                    "does not list it; dynamically registered handlers cannot be tested"
                )
        self._witness.names = names

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        if isinstance(frame, LLMContextFrame):
            for tool in seams.schema_handlers(frame.context.tools):
                handler = tool.handler
                if (
                    handler is not None
                    and tool.name in self._explicit
                    and handler != self._explicit[tool.name]
                ):
                    self._witness.refusal = (
                        f"bot() registered {tool.name!r} ahead of a different handler advertised "
                        "by the pipeline; Pipecat would call the bot's handler in its place"
                    )
                    return
        await super().process_frame(frame, direction)
        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return
        self.witness_tools()  # the base class has just registered what this context advertises
        if self._witness.refusal:
            return
        await self.push_frame(LLMFullResponseStartFrame())
        try:
            with self.activity.working():
                await self._generate(frame.context)
        finally:
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

    def _unfit(self, tool: ToolCall, context: LLMContext) -> str | None:
        """What is wrong with a scripted call that no registered function can take, or None."""
        if not self.has_function(tool.name):
            return (
                f"the scripted model called {tool.name!r}, which the bot has not registered; "
                f"it has {sorted(self._witness.names)}"
            )
        # A function the context does not advertise declares no parameters to check against.
        advertised = context.tools.standard_tools if isinstance(context.tools, ToolsSchema) else []
        declared = next((schema for schema in advertised if schema.name == tool.name), None)
        return None if declared is None else misfit(tool, declared.properties, declared.required)

    async def _generate(self, context: LLMContext) -> None:
        self._calls += 1
        call = self._calls
        items = summarize(context.get_messages(), self.error_results, self.interrupted)
        self._ledger.append("llm", "llm.request", call=call, items=items)
        reply = reply_for(self._policy, items, self._errors, call)
        tool_calls = []
        for tool in identified_calls(call, reply.tool_calls, self._call_ids, self._errors):
            tool_calls.append(tool)
            wrong = self._unfit(tool, context)
            if wrong is not None:
                self._errors.append(f"call {call}: {wrong}")
        claims = claim_spans(reply, call)
        async for text in fragments(
            self._ledger,
            call,
            reply,
            tool_calls,
            claims,
            self._ttft_us,
            self._duration_us,
            self.register,
        ):
            await self.push_frame(LLMTextFrame(text))
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
    """The LiveKit adapter's stamped voice, as a Pipecat service: the sink reads from the
    audio the stamp of the word each sample belongs to."""

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
        if (SAMPLE_RATE * self._letter_us + 500_000) // 1_000_000 < SHORTEST:
            raise ValueError(f"seconds_per_letter is shorter than {SHORTEST} audio samples")
        self._utterances = 0
        self._replies = 0
        self._reply: str | None = None  # the model reply being generated, if one is
        self._open: dict[str, _Utterance] = {}  # by reply, or by audio context outside one
        self.activity = Activity()
        self.table = StampTable(sample_rate=SAMPLE_RATE)

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        # Pipecat hands over one sentence at a time. A reply's sentences are one utterance,
        # as a reply's stream is one in the LiveKit adapter, even where Pipecat ends an
        # audio context between two of them because the model was slow.
        key = self._reply or context_id
        one = self._open.get(key)
        first = one is None  # the utterance waits for its first audio once
        if one is None:
            self._utterances += 1
            one = self._open[key] = _Utterance(
                number=self._utterances,
                ledger=self._ledger,
                table=self.table,
                register=self._register,
                sample_rate=SAMPLE_RATE,
                letter_us=self._letter_us,
                ttfb_us=self._ttfb_by_utterance.get(self._utterances, self._ttfb_us),
            )
        one.contexts.add(context_id)
        t0, starts, pcm = now_us(), [], bytearray()
        for word in text.split():
            starts.append((word, one.spoken[context_id] / SAMPLE_RATE))
            pcm.extend(one.render(word))
            one.spoken[context_id] += one.word_samples[-1]
        with self.activity.working():
            if first:
                await sleep_until(t0, one.ttfb_us)
            if pcm and self._word_timestamps:
                await self.add_word_timestamps(starts, context_id)
            if pcm:
                yield TTSAudioRawFrame(bytes(pcm), SAMPLE_RATE, 1, context_id=context_id)

    async def push_frame(
        self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ) -> None:
        if isinstance(frame, TTSStoppedFrame) and frame.context_id is not None:
            for key, one in list(self._open.items()):
                one.contexts.discard(frame.context_id)
                if not one.contexts and key != self._reply:
                    self.close(key)  # the reply's last sentence has been synthesized
        await super().push_frame(frame, direction)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        if isinstance(frame, InterruptionFrame):
            self.close_all()  # nothing more of these replies will be synthesized
            self._reply = None
        elif isinstance(frame, LLMFullResponseStartFrame):
            self._replies += 1
            self._reply = f"reply {self._replies}"
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMFullResponseEndFrame) and self._reply is not None:
            reply, self._reply = self._reply, None
            if reply in self._open and not self._open[reply].contexts:
                self.close(reply)  # its audio contexts had all ended before the reply did

    def close_all(self) -> None:
        for key in list(self._open):
            self.close(key)

    def close(self, key: str) -> None:
        one = self._open.pop(key, None)
        if one is None:
            return
        one.finish()


@dataclass
class _Utterance(Synthesis):
    contexts: set[str] = field(default_factory=set)  # audio contexts still open
    spoken: Counter[str] = field(default_factory=Counter)  # samples so far, by context
