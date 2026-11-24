"""Schedule scripted input, disconnection and drain on the run's clock."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from ...core.timebase import to_seconds, to_us
from ..case import Case, DrainOptions
from ..clock import quiet
from ..timing import sleep_until
from .ports import RunPorts

if TYPE_CHECKING:
    from .run import RunContext


async def drive(
    case: Case,
    drain: DrainOptions,
    run: RunContext,
    ports: RunPorts,
    completion: asyncio.Future[None] | None,
) -> None:
    """A worker runs independently and can finish or stall before it delivers the input."""
    timeout = drain.drain_timeout
    input_end = ports.input.done if case.hangup is None else run.origin.started.wait()
    delivered = True
    if completion is None:
        await input_end
    else:
        allowed = timeout
        if case.hangup is None:
            allowed += to_seconds(ports.input.last_us)
        ended: asyncio.Future[Any] = asyncio.ensure_future(input_end)
        await asyncio.wait(
            {completion, ended}, timeout=allowed, return_when=asyncio.FIRST_COMPLETED
        )
        delivered = ended.done() and not completion.done()
        if completion.done():
            ended.cancel()
            completion.result()
            run.errors.append("the pipeline ended before the scenario did")
        elif not delivered:
            ended.cancel()
            run.errors.append(f"the scripted input did not complete within {allowed:g} s")
            run.disconnected.set()
            await ports.disconnect()

    if delivered:
        if case.hangup is None:
            await asyncio.sleep(drain.drain_delay)

            def busy() -> bool:
                return any(not party.quiet() for party in ports.parties)

            # An independent worker may still have a turn in its queue at the first idle read.
            if completion is not None or busy():
                try:
                    await asyncio.wait_for(quiet(busy), timeout=timeout)
                except asyncio.TimeoutError:
                    if completion is not None:
                        run.errors.append(f"the bot was still busy {timeout} s after the scenario")
            await ports.drain()
        else:
            assert run.origin.t0_us is not None
            await sleep_until(run.origin.t0_us, to_us(case.hangup))
            run.disconnected.set()
            run.ledger.append("world", "user.hangup")
            await ports.disconnect()

    if completion is not None:
        try:
            await asyncio.wait_for(completion, timeout=timeout)
        except asyncio.TimeoutError:
            run.errors.append(f"the pipeline did not end within {timeout} s")
    if completion is not None or case.hangup is not None:
        try:
            await asyncio.wait_for(run.backend.settle_in_flight(), timeout=timeout)
        except asyncio.TimeoutError:
            if completion is not None:
                run.errors.append("backend writes did not settle before the drain timeout")
    if case.hangup is not None:
        await asyncio.sleep(drain.drain_delay)
