"""Run one scripted case against a real AgentSession and return its ledger."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import livekit.agents as lk
from livekit import rtc
from livekit.agents import utils
from livekit.agents.voice import io
from livekit.agents.voice.transcription.synchronizer import TranscriptSynchronizer

from ..core.backend import EffectBackend
from ..core.ledger import Ledger, ledger_hash
from .clock import WALL_CLOCK_EPOCH, ClockPatchReport, VirtualTimeLoop, now_us, run_virtual
from .providers import (
    Policy,
    ScriptedLLM,
    ScriptedSTT,
    ScriptedVAD,
    StampedTTS,
    UserSpeech,
    summarize_item,
)
from .sink import ClockedSink


@dataclass
class Case:
    """Everything that defines a run. Same case + same seed must give the same ledger."""

    name: str
    agent: Callable[[EffectBackend], lk.Agent]
    speeches: list[UserSpeech]
    policy: Policy
    seconds_per_word: float = 0.3
    tts_ttfb: float = 0.2
    sink_can_pause: bool = True
    min_interruption_duration: float = 0.5
    false_interruption_timeout: float = 2.0
    min_endpointing_delay: float = 0.5
    max_endpointing_delay: float = 6.0
    drain_delay: float = 5.0
    session_kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunResult:
    case: str
    seed: int
    ledger: Ledger
    hash: str
    versions: dict[str, str]
    clock: ClockPatchReport
    stamp_errors: int
    executor_calls: int
    framework_warnings: list[str]


class _ScriptedAudioInput(io.AudioInput):
    def __init__(self) -> None:
        super().__init__(label="EarfulScriptedInput")
        self._ch = utils.aio.Chan[rtc.AudioFrame]()

    async def __anext__(self) -> rtc.AudioFrame:
        return await self._ch.__anext__()

    def push_silence(self, seconds: float, sample_rate: int = 16000) -> None:
        n = round(sample_rate * seconds)
        self._ch.send_nowait(
            rtc.AudioFrame(
                data=b"\x00\x00" * n, sample_rate=sample_rate, num_channels=1, samples_per_channel=n
            )
        )


class _NullTextOutput(io.TextOutput):
    def __init__(self) -> None:
        super().__init__(label="EarfulNullText", next_in_chain=None)

    async def capture_text(self, text: str) -> None:
        pass

    def flush(self) -> None:
        pass


def _session_kwargs(case: Case) -> dict[str, Any]:
    """Turn-handling options, spelled the way this livekit-agents version expects."""
    kwargs: dict[str, Any] = {}
    if hasattr(lk, "TurnHandlingOptions"):
        kwargs["turn_handling"] = {
            # The default turn detector is an acoustic model; it cannot fire
            # deterministically on synthetic audio, so endpointing is VAD-based.
            "turn_detection": None,
            "endpointing": lk.EndpointingOptions(
                min_delay=case.min_endpointing_delay, max_delay=case.max_endpointing_delay
            ),
            "interruption": lk.InterruptionOptions(
                min_duration=case.min_interruption_duration,
                false_interruption_timeout=case.false_interruption_timeout,
            ),
        }
        kwargs["aec_warmup_duration"] = None
    else:  # pre-TurnHandlingOptions releases
        kwargs.update(
            min_endpointing_delay=case.min_endpointing_delay,
            max_endpointing_delay=case.max_endpointing_delay,
            min_interruption_duration=case.min_interruption_duration,
            false_interruption_timeout=case.false_interruption_timeout,
        )
    kwargs.update(case.session_kwargs)
    return kwargs


def _created_us(ev: Any) -> int | None:
    created = getattr(ev, "created_at", None)
    return round((created - WALL_CLOCK_EPOCH) * 1_000_000) if created is not None else None


def _tap(session: lk.AgentSession, ledger: Ledger, errors: list[str]) -> None:
    """Record public session events. Explanatory evidence, never ground truth.

    A handler must never raise into the framework: an exception here would change the
    behavior under test. Failures are collected and fail the run afterwards.
    """

    def on(name: str, fields: Callable[[Any], dict[str, Any]]) -> None:
        def handler(ev: Any) -> None:
            try:
                ledger.append("framework", f"fw.{name}", created_us=_created_us(ev), **fields(ev))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"tap {name}: {type(exc).__name__}: {exc}")

        session.on(name, handler)

    on("agent_state_changed", lambda e: {"old": e.old_state, "new": e.new_state})
    on("user_state_changed", lambda e: {"old": e.old_state, "new": e.new_state})
    on("user_input_transcribed", lambda e: {"text": e.transcript, "final": e.is_final})
    on("conversation_item_added", lambda e: {"item": summarize_item(e.item)})
    on(
        "function_tools_executed",
        lambda e: {
            "calls": [summarize_item(c) for c in e.function_calls],
            "outputs": [summarize_item(o) for o in e.function_call_outputs if o is not None],
        },
    )
    on(
        "speech_created",
        lambda e: {
            "speech_id": e.speech_handle.id,
            "source": e.source,
            "user_initiated": e.user_initiated,
        },
    )
    on("agent_false_interruption", lambda e: {})
    on("close", lambda e: {"reason": str(getattr(e.reason, "value", e.reason))})
    on("error", lambda e: {"error": type(e.error).__name__})


class _WarningCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(f"{record.name}: {record.getMessage()}")


async def _run(case: Case, seed: int) -> RunResult:
    random.seed(seed)
    loop = asyncio.get_running_loop()
    if isinstance(loop, VirtualTimeLoop):
        ledger = Ledger(now_us)
    else:  # real-clock control run: times are relative to the start of the run
        origin = loop.time()
        ledger = Ledger(lambda: round((loop.time() - origin) * 1_000_000))
    ledger.append("harness", "run.start", case=case.name, seed=seed)

    warnings = _WarningCollector()
    lk_logger = logging.getLogger("livekit.agents")
    lk_logger.addHandler(warnings)

    stt = ScriptedSTT(ledger, case.speeches)
    tts = StampedTTS(ledger, seconds_per_word=case.seconds_per_word, ttfb=case.tts_ttfb)
    session = lk.AgentSession(
        vad=ScriptedVAD(ledger, case.speeches),
        stt=stt,
        llm=ScriptedLLM(ledger, case.policy),
        tts=tts,
        **_session_kwargs(case),
    )

    audio_in = _ScriptedAudioInput()
    sink = ClockedSink(ledger, tts.table, can_pause=case.sink_can_pause)
    sync = TranscriptSynchronizer(next_in_chain_audio=sink, next_in_chain_text=_NullTextOutput())
    session.input.audio = audio_in
    session.output.audio = sync.audio_output
    session.output.transcription = sync.text_output
    harness_errors: list[str] = []
    _tap(session, ledger, harness_errors)

    try:
        agent = case.agent(EffectBackend(ledger))
        await session.start(agent)
        audio_in.push_silence(0.1)  # first frame starts the scripted user's clock
        await stt.done
        await asyncio.sleep(case.drain_delay)
        with contextlib.suppress(RuntimeError):
            await session.drain()
        ledger.append(
            "harness",
            "history.final",
            items=[summarize_item(i) for i in session.history.items],
        )
        await session.aclose()
        await sync.aclose()
    finally:
        lk_logger.removeHandler(warnings)

    if harness_errors:
        raise RuntimeError(f"harness errors, run is invalid: {harness_errors}")
    ledger.append("harness", "run.end", stamp_errors=sink.stamp_errors)
    from .clock import _REPORT

    return RunResult(
        case=case.name,
        seed=seed,
        ledger=ledger,
        hash=ledger_hash(ledger.events),
        versions=versions(),
        clock=_REPORT,
        stamp_errors=sink.stamp_errors,
        executor_calls=getattr(loop, "executor_calls", -1),
        framework_warnings=warnings.messages,
    )


def versions() -> dict[str, str]:
    import platform

    return {
        "livekit-agents": lk.__version__,
        "livekit": rtc.__version__,
        "python": platform.python_version(),
        "platform": f"{platform.system()}-{platform.machine()}",
    }


def run_case(case: Case, seed: int = 0, *, virtual: bool = True) -> RunResult:
    """Run ``case`` once on a fresh loop.

    ``virtual=False`` runs on the real clock with nothing patched. It exists only as a
    negative control: it shows what the ledger hash does when timing is not controlled.
    """
    if virtual:
        return run_virtual(lambda: _run(case, seed))
    return asyncio.run(_run(case, seed))
