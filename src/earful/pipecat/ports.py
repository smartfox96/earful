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
from ..scripted.kernel.ports import FinalHistory, RunPorts
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

        async def start() -> asyncio.Future[None]:
            await runner.add_workers(worker)
            return asyncio.ensure_future(runner.run())

        async def drain() -> None:
            await worker.queue_frame(EndFrame())

        async def close() -> None:
            run.stamp_errors = s.output.stamp_errors

        yield RunPorts(
            config=lambda: header,
            start=start,
            input=s.stt.script,
            parties=(
                s.stt.script,
                s.user.activity,
                s.model.activity,
                s.voice.activity,
                Observed(lambda: tap.user_speaking),
                Observed(lambda: bool(s.witness.in_flight)),
                Observed(lambda: s.output.playing),
            ),
            drain=drain,
            disconnect=worker.cancel,
            history=lambda: _history(case, plan, context, s, errors),
            close=close,
        )
    finally:
        await s.backend.cancel_in_flight()
        backends.uninstall(token)


__all__ = ["build", "scaffold"]
