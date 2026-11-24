"""Bind an AgentSession to the run kernel."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import livekit.agents as lk
from livekit import rtc
from livekit.agents import utils

from ..core import backend as backends
from ..core.backend import EffectBackend
from ..core.ledger import Ledger
from ..core.timebase import to_seconds, to_us
from ..scripted.case import (
    check_intent_tools,
)
from ..scripted.clock import (
    now_us,
)
from ..scripted.kernel.ports import FinalHistory, RunPorts, WaitForQuiet
from ..scripted.kernel.run import RunContext
from . import options, tools
from ._compat import TranscriptSynchronizer, io
from .case import Case
from .config import _declared_session, _effective_session, _header, _session_kwargs, _switches
from .providers import (
    ScriptedLLM,
    ScriptedSTT,
    ScriptedVAD,
    StampedTTS,
    WorldOrigin,
    record_errors_in,
    starting_history,
    stop_recording_errors,
    summarize_item,
)
from .witness import _tap, _WarningCollector


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


async def _hang_up(
    case: Case,
    session: lk.AgentSession[Any],
    origin: WorldOrigin,
    ledger: Ledger,
    witness: tools.ToolWitness,
    backend: EffectBackend,
    errors: list[str],
) -> None:
    assert case.hangup is not None
    await origin.started.wait()
    assert origin.t0_us is not None
    wait_us = origin.t0_us + to_us(case.hangup) - now_us()
    if wait_us > 0:
        await asyncio.sleep(to_seconds(wait_us))
    ledger.append("world", "user.hangup")
    closing = asyncio.ensure_future(session.aclose())
    done, _ = await asyncio.wait({closing}, timeout=case.drain_timeout)
    if not done:
        # Some releases cannot close a session while one of its tools is stuck.
        await witness.cancel_running()
        done, _ = await asyncio.wait({closing}, timeout=case.drain_timeout)
    if not done:
        closing.cancel()
        errors.append(f"the session did not close within {case.drain_timeout} s of the hangup")
    else:
        closing.result()  # a close that raises is the run's error, as at the end of a scenario
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(backend.settle_in_flight(), timeout=case.drain_timeout)
    # An output that has not stopped is given the time a scenario gives one at its end.
    await asyncio.sleep(case.drain_delay)


@asynccontextmanager
async def build(case: Case, context: RunContext) -> AsyncIterator[RunPorts]:
    ledger, harness_errors, backend = context.ledger, context.errors, context.backend
    witness = tools.ToolWitness(ledger, case.effects)
    warnings = _WarningCollector(logging.getLogger("livekit.agents"))
    warnings.open()
    witness_token = tools.install(witness)
    backend_token = backends.install(backend)
    errors_token = record_errors_in(harness_errors)
    try:
        yield await _session(case, context, witness)
    finally:
        warnings.close()
        stop_recording_errors(errors_token)
        backends.uninstall(backend_token)
        tools.uninstall(witness_token)
    if witness.refusal is not None:
        raise ValueError(witness.refusal)
    context.warnings = warnings.messages


async def _session(case: Case, context: RunContext, witness: tools.ToolWitness) -> RunPorts:
    ledger, harness_errors, backend = context.ledger, context.errors, context.backend
    declared_us, virtual = context.declared_us, context.virtual
    world_origin = WorldOrigin(ledger, harness_errors, tolerance_us=0 if virtual else 5_000)
    stt = ScriptedSTT(ledger, case.speeches, world_origin)
    model = ScriptedLLM(
        ledger,
        case.policy,
        harness_errors,
        ttft_us=declared_us("llm.ttft"),
        duration_us=declared_us("llm.duration"),
    )
    tts = StampedTTS(
        ledger,
        model.register,
        seconds_per_letter=case.seconds_per_letter,
        ttfb=case.tts_ttfb,
        ttfb_us=declared_us("tts.ttfb"),
    )
    session = lk.AgentSession(
        vad=ScriptedVAD(
            ledger,
            case.speeches,
            world_origin,
            min_speech_duration=case.vad_min_speech,
            min_silence_duration=case.vad_min_silence,
        ),
        stt=stt,
        llm=model,
        tts=tts,
        **_session_kwargs(case),
    )

    audio_in = _ScriptedAudioInput()
    sink = case.sink(ledger, tts.table, can_pause=case.sink_can_pause)
    sync = TranscriptSynchronizer(next_in_chain_audio=sink, next_in_chain_text=_NullTextOutput())
    session.input.audio = audio_in
    session.output.audio = sync.audio_output
    session.output.transcription = sync.text_output

    # The agent is built first, so that the header can name its tools and state its options.
    agent = case.agent()
    refusal = options.agent_refusal(agent, session)
    if refusal is not None:
        raise ValueError(refusal)
    found = {**tools.witness_session_tools(session), **await tools.witness_agent_tools(agent)}
    scenario = _header(case, found, starting_history(agent.chat_ctx.items))

    declared = {**_declared_session(case), **_switches(case)}
    effective = _effective_session(session)
    for name, value in declared.items():
        if value != effective[name]:
            harness_errors.append(
                f"declared {name} = {value}, but the session holds {effective[name]}"
            )

    closed: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
    checked = [agent]

    def on_close(event: Any) -> None:
        if not closed.done():
            closed.set_result(str(getattr(event.reason, "value", event.reason)))

    async def start() -> None:
        _tap(session, ledger, harness_errors, checked=checked)
        session.on("close", on_close)
        await session.start(agent)
        audio_in.push_silence(0.1)

    def is_quiet() -> bool:
        return not (witness.running or session.agent_state in ("thinking", "speaking"))

    async def drive(wait: WaitForQuiet) -> None:
        if case.hangup is None:
            await stt.done
            await asyncio.sleep(case.drain_delay)

            # A draining session asks the model with tool_choice='none': not over a running tool.
            # One that never returns is judged as such, and the session is drained over it.
            if not is_quiet():
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(wait(), timeout=case.drain_timeout)
            with contextlib.suppress(RuntimeError, asyncio.TimeoutError):
                await asyncio.wait_for(session.drain(), timeout=case.drain_timeout)
        else:
            await _hang_up(case, session, world_origin, ledger, witness, backend, harness_errors)

    def history() -> FinalHistory:
        return {
            "read_from": "AgentSession.history",
            "items": [summarize_item(i) for i in session.history.items],
        }

    async def close() -> None:
        # Some releases cannot close a session while one of its tools is stuck.
        await witness.cancel_running()
        await backend.cancel_in_flight()
        try:
            await asyncio.wait_for(session.aclose(), timeout=case.drain_timeout)
        except asyncio.TimeoutError:
            harness_errors.append(f"the session did not close within {case.drain_timeout} s")
        await sync.aclose()
        if not harness_errors:
            available = set(found)
            for active in checked:
                available.update(tools.tool_names(active.tools))
            check_intent_tools(case.intents, available)

        context.stamp_errors, context.underruns = sink.stamp_errors, sink.underruns

    return RunPorts(lambda: scenario, start, drive, history, close, is_quiet, lambda: closed)
