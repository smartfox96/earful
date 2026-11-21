"""The virtual clock under a LiveKit session: what makes a run invalid, and what a run leaves."""

import asyncio
import contextlib
import dataclasses
import datetime
import gc
import os
import random
import threading
import time

import pytest

pytest.importorskip("livekit.agents")

import livekit.agents as lk
from pydantic import BaseModel, Field, TypeAdapter

from earful.livekit.cases import CASES
from earful.livekit.harness import Case, InvalidRun, run_case
from earful.scripted.clock import WALL_CLOCK_EPOCH, patch_report, run_virtual
from earful.scripted.model import Reply, ToolCall
from earful.scripted.world import UserSpeech


async def _nothing():
    return None


def test_a_model_that_cannot_be_patched_is_reported_and_makes_a_run_invalid():
    class Unbuilt(BaseModel):
        at: float = Field(default_factory=time.time)
        other: "NotDefinedYet"  # pydantic cannot build the model until this name exists

    try:
        run_virtual(_nothing)
        (unpatched,) = patch_report().unpatched
        assert unpatched.endswith("Unbuilt.at")
        with pytest.raises(InvalidRun, match=r"Unbuilt\.at reads the wall clock"):
            run_case(CASES["barge_in_mid_word"]())
    finally:
        NotDefinedYet = int
        Unbuilt.model_rebuild()

    async def main():
        await asyncio.sleep(2)
        return Unbuilt(other=1).at

    assert run_virtual(main) == WALL_CLOCK_EPOCH + 2
    assert patch_report().unpatched == []
    assert not run_case(CASES["barge_in_mid_word"]()).invalid


def _a_tool_that_awaits(wait):
    """A case whose only tool awaits ``wait()``, with the session's timers running beside it."""

    class Agent(lk.Agent):
        def __init__(self):
            super().__init__(instructions="You look things up.")

        @lk.function_tool
        async def look_up(self) -> str:
            """Look something up."""
            return await wait()

    def policy(items):
        if any(item["type"] == "function_call_output" for item in items):
            return Reply(text="Found it.")
        return Reply(text="One moment.", tool_calls=(ToolCall("look_up", "{}"),))

    return Case(
        name="real_work",
        agent=Agent,
        speeches=[UserSpeech(start=0.5, end=1.5, transcript="Look it up.")],
        policy=policy,
    )


_STARTED: list[object] = []  # the threads and child processes the tools below leave behind


async def _a_thread_that_answers():
    loop = asyncio.get_running_loop()
    answer = loop.create_future()

    def work():
        time.sleep(0.03)
        with contextlib.suppress(RuntimeError):  # the run is over by then, and its loop closed
            loop.call_soon_threadsafe(answer.set_result, "found")

    _STARTED.append(threading.Thread(target=work))
    _STARTED[-1].start()
    return await answer


async def _a_thread_that_is_polled():
    found = []
    _STARTED.append(threading.Thread(target=lambda: (time.sleep(0.03), found.append("found"))))
    _STARTED[-1].start()
    while not found:
        await asyncio.sleep(0.01)
    return found[0]


async def _a_child_process():
    child = await asyncio.create_subprocess_exec("/bin/sleep", "0.05")
    _STARTED.append(child)
    await child.wait()
    return "found"


async def _a_file_descriptor():
    loop = asyncio.get_running_loop()
    answer = loop.create_future()
    reading, writing = os.pipe()  # nothing is ever written: the tool waits on the descriptor
    loop.add_reader(reading, answer.set_result, "found")
    try:
        return await answer
    finally:
        loop.remove_reader(reading)
        os.close(reading)
        os.close(writing)


def _tidy_up():
    for started in _STARTED:
        if isinstance(started, threading.Thread):
            started.join()
        else:
            started._transport.close()  # kills a child the cancelled tool left running
    _STARTED.clear()
    for thread in threading.enumerate():
        if thread.name.startswith("asyncio-waitpid"):
            thread.join()


@pytest.mark.parametrize(
    "wait, left",
    [
        (_a_thread_that_answers, "threads started: 1"),
        (_a_thread_that_is_polled, "threads started: 1"),
        (_a_child_process, "child processes started: 1"),
        (_a_file_descriptor, "file descriptors watched: 1"),
    ],
)
def test_a_tool_that_waits_on_work_in_real_time_makes_the_run_invalid(wait, left):
    try:
        result = run_case(_a_tool_that_awaits(wait), require_valid=False)
    finally:
        _tidy_up()
    (reason,) = result.invalid
    assert reason.startswith("harness error: work left the virtual clock (") and left in reason
    assert result.events[-1]["data"]["executor_calls"] == 0


def test_a_tool_that_waits_only_on_the_virtual_clock_leaves_the_run_valid():
    async def wait():
        asyncio.get_running_loop().call_soon_threadsafe(lambda: None)  # from the loop's thread
        await asyncio.sleep(3)
        return "found"

    result = run_case(_a_tool_that_awaits(wait))
    assert result.invalid == []
    assert [e["t_us"] for e in result.events if e["kind"] == "tool.returned"] == [5_000_000]


_REAL_NOW = time.time  # a reference taken before any run, as ``from time import time`` takes


@dataclasses.dataclass
class _Record:
    at: float = dataclasses.field(default_factory=time.time)


class _Inner(BaseModel):
    at: float = Field(default_factory=time.time)


class _Outer(BaseModel):
    inner: _Inner


_BUILT_BEFORE = TypeAdapter(_Inner)


def test_the_reads_the_clock_cannot_reach_return_real_time_and_the_run_is_valid():
    """The reads ``docs/compatibility.md`` lists as real. A change here changes that list."""
    seen = {}

    async def wait():
        seen["datetime.now"] = datetime.datetime.now().timestamp()
        seen["a reference taken before the run"] = _REAL_NOW()
        seen["a dataclass default"] = _Record().at
        seen["a validator built before the run"] = _BUILT_BEFORE.validate_python({}).at
        seen["a model built from a dict inside another"] = _Outer(inner={}).inner.at
        seen["time.time"] = time.time()
        return "found"

    assert run_case(_a_tool_that_awaits(wait)).invalid == []
    assert seen.pop("time.time") < WALL_CLOCK_EPOCH + 60
    assert seen and all(abs(at - time.time()) < 60 for at in seen.values()), seen


@pytest.mark.parametrize("virtual", [True, False])
def test_a_run_started_inside_a_running_loop_is_refused_in_one_line(virtual, recwarn):
    async def inside():
        return run_case(CASES["barge_in_mid_word"](), virtual=virtual)

    with pytest.raises(RuntimeError, match="cannot be started from inside a running one") as no:
        asyncio.run(inside())
    assert "\n" not in str(no.value)
    gc.collect()  # a coroutine made and never run would warn as it is collected
    assert [str(w.message) for w in recwarn if "never awaited" in str(w.message)] == []


def test_a_session_leaves_the_random_module_as_it_found_it():
    random.seed(123)
    before = random.getstate()
    run_case(CASES["barge_in_mid_word"](), seed=3)
    assert random.getstate() == before
