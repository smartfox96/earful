"""Bind a Pipecat worker to the run kernel."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pipecat.frames.frames import (
    EndFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.workers.runner import WorkerRunner

from ..core import backend as backends
from ..scripted.ceilings import cancel
from ..scripted.kernel.ports import Ended, FinalHistory, RunPorts
from ..scripted.kernel.run import RunContext
from ..scripted.parties.activity import Observed
from . import pipeline as pipelines
from .case import Case
from .providers import (
    summarize,
)
from .scaffold import scaffold
from .worker import _built, _Scripted, _scripted, _worker


def _history(
    case: Case,
    plan: pipelines.Plan,
    context: LLMContext,
    s: _Scripted,
    errors: list[str],
) -> FinalHistory:
    """Close the voice, check the turn strategies held, and record the final history."""
    if s.witness.refusal is not None:
        raise ValueError(s.witness.refusal)
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
        loop = asyncio.get_running_loop()
        ended: asyncio.Future[Ended] = loop.create_future()
        running: list[asyncio.Future[None]] = []

        def finished(task: asyncio.Future[None]) -> None:
            if ended.done():
                return
            if task.cancelled():
                ended.set_result(Ended(ledger.now_us(), "cancelled"))
            elif task.exception() is not None:
                ended.set_exception(task.exception())  # type: ignore[arg-type]
            else:
                ended.set_result(Ended(ledger.now_us(), "the pipeline ended"))

        async def start() -> asyncio.Future[Ended]:
            await runner.add_workers(worker)
            running.append(asyncio.ensure_future(runner.run()))
            running[0].add_done_callback(finished)
            return ended

        async def drain() -> None:
            await worker.queue_frame(EndFrame())

        async def close() -> None:
            if running and not ended.done():
                await worker.cancel()
                await asyncio.wait(running, timeout=case.drain_timeout)
                if not running[0].done():
                    cancel(running[0])
                    if not errors:
                        errors.append(f"the pipeline did not stop within {case.drain_timeout:g} s")
            run.stamp_errors = s.output.stamp_errors

        yield RunPorts(
            config=lambda: header,
            start=start,
            input=s.stt.script,
            parties={
                "the scripted user is still to speak": s.stt.script,
                "the scripted user is speaking": s.user.activity,
                "the model is answering": s.model.activity,
                "the voice is synthesizing": s.voice.activity,
                "the pipeline holds the user's turn open": Observed(lambda: tap.user_speaking),
                "the output is playing": Observed(lambda: s.output.playing),
            },
            running=s.witness.running,
            drain=drain,
            disconnect=worker.cancel,
            history=lambda: _history(case, plan, context, s, errors),
            close=close,
        )
    finally:
        backends.uninstall(token)


__all__ = ["build", "scaffold"]
