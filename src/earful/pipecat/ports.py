"""Bind a Pipecat worker to the run kernel."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from pipecat.frames.frames import (
    EndFrame,
)
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.workers.runner import WorkerRunner

from ..core import backend as backends
from ..core.ledger import Ledger
from ..core.timebase import to_seconds, to_us
from ..scripted.kernel.ports import FinalHistory, RunPorts, WaitForQuiet
from ..scripted.kernel.run import RunContext
from ..scripted.world import sleep_until
from . import pipeline as pipelines
from .case import Case
from .providers import (
    summarize,
)
from .worker import _built, _Scripted, _scripted, _worker


async def _play(
    case: Case,
    worker: PipelineWorker,
    s: _Scripted,
    ledger: Ledger,
    errors: list[str],
    running: asyncio.Future[Any],
    wait: WaitForQuiet,
) -> None:
    """Run the pipeline through the scenario, and end it as the scenario does: once all is
    quiet, or when the user hangs up."""
    origin, left, backend = s.origin, s.left, s.backend
    ended = asyncio.ensure_future(s.stt.done if case.hangup is None else origin.started.wait())
    # A provider can fail before signaling completion. Allow the entire script to
    # play, plus the same startup/scheduling allowance as for pipeline shutdown.
    timeout = case.drain_timeout
    if case.hangup is None:
        last_transcript_us = max(
            (speech.end_us + speech.stt_delay_us for speech in case.speeches), default=0
        )
        timeout += to_seconds(last_transcript_us)
    await asyncio.wait({running, ended}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    if running.done():
        errors.append("the pipeline ended before the scenario did")
        ended.cancel()
        running.result()
    elif not ended.done():
        errors.append(f"the scripted input did not complete within {timeout:g} s")
        ended.cancel()
        left.set()
        await worker.cancel()
    elif case.hangup is None:
        await asyncio.sleep(case.drain_delay)
        # Pipecat's EndFrame cancels a function call still running: wait until all is quiet.
        try:
            await asyncio.wait_for(wait(), timeout=case.drain_timeout)
        except asyncio.TimeoutError:
            errors.append(f"the bot was still busy {case.drain_timeout} s after the scenario")
        await worker.queue_frame(EndFrame())
    else:
        # The user leaves. A Pipecat application cancels its pipeline when the client
        # disconnects, which is what the transports' examples do.
        assert origin.t0_us is not None
        await sleep_until(origin.t0_us, to_us(case.hangup))
        left.set()
        ledger.append("world", "user.hangup")
        await worker.cancel()
    try:
        await asyncio.wait_for(running, timeout=case.drain_timeout)
    except asyncio.TimeoutError:
        errors.append(f"the pipeline did not end within {case.drain_timeout} s")
    try:
        await asyncio.wait_for(backend.settle_in_flight(), timeout=case.drain_timeout)
    except asyncio.TimeoutError:
        errors.append("backend writes did not settle before the drain timeout")
    if case.hangup is not None:
        await asyncio.sleep(case.drain_delay)


def _history(
    case: Case,
    plan: pipelines.Plan,
    context: LLMContext,
    s: _Scripted,
    errors: list[str],
) -> FinalHistory:
    """Close the voice, check the turn strategies held, and record the final history."""
    s.voice.close_all()
    timeouts = (case.user_speech_timeout, case.user_turn_stop_timeout)
    # The header states the turn strategies the run began with.
    try:
        now = pipelines.turns(plan.user, *timeouts)
        changed = ", ".join(f"{k} is {v}" for k, v in now.items() if plan.options[k] != v)
    except ValueError as exc:
        changed = str(exc)
    if changed:
        errors.append(f"the pipeline changed its turn strategies during the run: {changed}")
    return {
        "read_from": "LLMContext.get_messages",
        "items": summarize(context.get_messages(), s.model.error_results, s.model.interrupted),
    }


@asynccontextmanager
async def build(case: Case, run: RunContext) -> AsyncIterator[RunPorts]:
    ledger, errors = run.ledger, run.errors
    s = _scripted(case, run)
    token = backends.install(s.backend)
    try:
        context, plan, header = _built(case, s)
        worker, tap = _worker(plan, s, ledger, errors, run.warnings, run.virtual)
        runner = WorkerRunner(handle_sigint=False)
        running: asyncio.Future[Any]

        async def start() -> None:
            nonlocal running
            await runner.add_workers(worker)
            running = asyncio.ensure_future(runner.run())

        def busy() -> bool:
            return bool(
                not s.user.done
                or tap.user_speaking
                or s.witness.in_flight
                or s.model.generating
                or s.voice.synthesizing
                or s.output.playing
            )

        async def drive(wait: WaitForQuiet) -> None:
            await _play(case, worker, s, ledger, errors, running, wait)
            if s.witness.refusal is not None:
                raise ValueError(s.witness.refusal)

        async def closed() -> None:
            await asyncio.shield(running)

        async def close() -> None:
            run.stamp_errors = s.output.stamp_errors

        yield RunPorts(
            lambda: header,
            start,
            drive,
            lambda: _history(case, plan, context, s, errors),
            close,
            lambda: not busy(),
            closed,
        )
    finally:
        await s.backend.cancel_in_flight()
        backends.uninstall(token)
