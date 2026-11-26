"""The one end of a run for every framework: the scenario, settling, and the wind-down.
Every wait on the framework is bounded and never waits on a cancellation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, TypeVar

from ...core.checker import open_deadline
from ...core.timebase import to_seconds, to_us
from ..case import Case, DrainOptions
from ..ceilings import cancel
from ..clock import quiet
from ..timing import sleep_until
from .ports import Ended, RunPorts

if TYPE_CHECKING:
    from .run import RunContext

T = TypeVar("T")


async def within(work: Awaitable[T], seconds: float) -> tuple[bool, T | None]:
    """Whether ``work`` finished within ``seconds``, and its result."""
    task = asyncio.ensure_future(work)
    done, _ = await asyncio.wait({task}, timeout=max(seconds, 0.0))
    if task not in done:
        cancel(task)  # not awaited: a task can ignore it
        return False, None
    return True, task.result()


async def _first(one: asyncio.Future[Any], other: asyncio.Future[Any], seconds: float) -> None:
    await asyncio.wait([one, other], timeout=max(seconds, 0.0), return_when=asyncio.FIRST_COMPLETED)


class _Ending:
    def __init__(self, case: Case, drain: DrainOptions, run: RunContext, ports: RunPorts) -> None:
        self.case, self.drain, self.run, self.ports = case, drain, run, ports
        self.timeout = drain.drain_timeout
        self.closed: asyncio.Future[Ended] = asyncio.get_running_loop().create_future()
        self.asked = False  # whether Earful asked the framework to end
        self.own: Ended | None = None  # the end the framework chose before it was asked
        self.stuck = False  # work was still running when the run stopped waiting for it

    def ended(self) -> bool:
        if not self.closed.done():
            return False
        if not self.asked and self.own is None:
            self.own = self.closed.result()
        return True

    def busy(self) -> bool:
        return any(not p.quiet() for p in self.ports.parties.values()) or bool(self.ports.running())

    def doing(self) -> str:
        busy = [doing for doing, party in self.ports.parties.items() if not party.quiet()]
        return "; ".join([*busy, *(f"tool {name} is running" for name in self.ports.running())])

    def error(self, message: str) -> None:
        self.run.errors.append(message)

    def stopped(self, message: str) -> None:
        """Work was left running. Named only if nothing before it explains why."""
        self.stuck = True
        if not self.run.errors:
            self.error(message)

    async def start(self) -> bool:
        started, closed = await within(self.ports.start(), self.timeout)
        if not started:
            self.error(f"the framework did not start within {self.timeout:g} s")
            return False
        assert closed is not None
        self.closed = closed
        return True

    async def scenario(self) -> None:
        """Deliver the scripted input, or the hangup, unless the framework ends first."""
        case, run = self.case, self.run
        if case.hangup is None:
            until: asyncio.Future[Any] = self.ports.input.done
            allowed = to_seconds(self.ports.input.last_us) + self.timeout
        else:
            until = asyncio.ensure_future(self._hangup_time(to_us(case.hangup)))
            allowed = case.hangup + self.timeout
        await _first(until, self.closed, allowed)
        if self.ended() or not until.done():
            if until is not self.ports.input.done:
                cancel(until)
            if not self.ended():
                self.error(f"the scripted input did not complete within {allowed:g} s")
                await self.leave()
            return
        if case.hangup is not None:
            run.ledger.append("world", "user.hangup")
            await self.leave()

    async def _hangup_time(self, hangup_us: int) -> None:
        origin = self.run.origin
        await origin.started.wait()
        assert origin.t0_us is not None
        await sleep_until(origin.t0_us, hangup_us)

    async def leave(self) -> None:
        """The user is gone: the framework is told, as a disconnected call tells it."""
        self.asked = True
        self.run.disconnected.set()
        left, _ = await within(self.ports.disconnect(), 3 * self.timeout)
        if not left:
            self.stopped(f"the framework did not end within {3 * self.timeout:g} s of the hangup")

    async def settle(self) -> None:
        """Wait until the agent is quiet, every write has its outcome and every deadline a
        contract reads has passed, within the drain timeout."""
        await asyncio.wait({self.closed}, timeout=self.drain.drain_delay)
        loop = asyncio.get_running_loop()
        settle_by = loop.time() + self.timeout
        while not self.ended():
            quieted = asyncio.ensure_future(quiet(self.busy))
            await _first(quieted, self.closed, settle_by - loop.time())
            cancel(quieted)
            if self.ended():
                return
            if not quieted.done() and self.busy():
                doing = self.doing()
                self.stopped(
                    f"the agent was still busy {self.timeout:g} s after the scenario: {doing}"
                )
                return
            if not await self.writes(settle_by - loop.time()):
                return
            due = open_deadline(self.run.ledger.events, self.run.ledger.now_us())
            if due is None:
                return
            wait = to_seconds(due + 1 - self.run.ledger.now_us())
            if loop.time() + wait > settle_by:
                return  # a deadline past the drain timeout stays unjudged
            await asyncio.wait({self.closed}, timeout=wait)

    async def writes(self, seconds: float) -> bool:
        """Whether every backend write under way had its outcome within ``seconds``."""
        settled, _ = await within(self.run.backend.settle_in_flight(), seconds)
        if not settled:
            pending = ", ".join(str(n) for n in self.run.backend.in_flight())
            self.stopped(
                f"backend write {pending} had no outcome {self.timeout:g} s after the scenario"
            )
        return settled

    async def wind_down(self) -> None:
        """Ask the framework to finish its work and end, as at the end of a call."""
        if self.stuck or self.ended():
            return
        self.asked = True
        asked, _ = await within(self.ports.drain(), self.timeout)
        if asked:
            await asyncio.wait({self.closed}, timeout=self.timeout)
        if not self.closed.done():
            doing = self.doing()
            self.stopped(
                f"the framework did not end within {self.timeout:g} s after the scenario"
                + (f": {doing}" if doing else "")
            )


async def drive(case: Case, drain: DrainOptions, run: RunContext, ports: RunPorts) -> Ended | None:
    """Run the scenario to its end. Returns the end the framework chose before Earful asked
    for one, if it did."""
    ending = _Ending(case, drain, run, ports)
    if not await ending.start():
        return None
    await ending.scenario()
    if case.hangup is None:
        await ending.settle()
        await ending.wind_down()
    else:
        await asyncio.sleep(drain.drain_delay)  # what the application does after the user left
    if not ending.stuck:
        await ending.writes(ending.timeout)
    ending.ended()
    return ending.own
