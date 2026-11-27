"""Scripted LLM and TTS for a real AgentSession.
Authors give times in seconds; the providers compute in integer microseconds."""

from __future__ import annotations

from typing import Any, NoReturn

from livekit.agents import llm as lk_llm
from livekit.agents import tts as lk_tts
from livekit.agents import utils

from ..core.ledger import Ledger
from ..core.timebase import to_us
from ..scripted.model import Claim, Policy, Reply, ReplyRegister, ToolCall, misfit
from ..scripted.parties.activity import Activity
from ..scripted.parties.model import claim_spans, fragments, identified_calls, reply_for
from ..scripted.parties.voice import Synthesis as _Synthesis
from ..scripted.provider_errors import (
    record_errors_in,
    scripted,
    scripted_call,
    stop_recording_errors,
)
from ..scripted.voice import SHORTEST, StampTable, letters
from ..scripted.world import UserSpeech, WorldOrigin, vad_segments
from ..scripted.world import sleep_until as _sleep_until
from . import seams
from .seams import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions, summarize_item
from .tools import unwitnessed


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
        self.activity = Activity()
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
        claims = claim_spans(reply, call, model._errors)
        tool_calls = list(identified_calls(call, reply.tool_calls, model._call_ids, model._errors))
        with model.activity.working():
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
                    lk_llm.FunctionToolCall(
                        name=t.name, arguments=t.arguments, call_id=t.call_id or ""
                    )
                    for t in tool_calls
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
        self.activity = Activity()
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
        with tts.activity.working():
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
        with tts.activity.working():
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
    "StampTable",
    "StampedTTS",
    "ToolCall",
    "UserSpeech",
    "WorldOrigin",
    "letters",
    "record_errors_in",
    "scripted",
    "scripted_call",
    "stop_recording_errors",
    "summarize_item",
    "vad_segments",
]
