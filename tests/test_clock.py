"""The virtual clock, and the patch that makes pydantic models read it."""

import asyncio
import logging
import random
import socket
import threading
import time

import pytest
from pydantic import BaseModel, Field

from earful.core.timebase import to_seconds, to_us
from earful.scripted.clock import (
    VIRTUAL_READS,
    WALL_CLOCK_EPOCH,
    left_the_clock,
    patch_report,
    run_real,
    run_virtual,
)


def test_time_is_exact_on_the_grid():
    async def main():
        loop = asyncio.get_running_loop()
        seen = []
        for _ in range(3):
            await asyncio.sleep(0.1)  # 0.1 + 0.1 + 0.1 != 0.3 in floating point
            seen.append(loop.time())
        await asyncio.sleep(5.2)
        seen.append(loop.time())
        return seen

    assert run_virtual(main) == [0.1, 0.2, 0.3, 5.5]


def test_same_microsecond_fires_in_scheduling_order():
    async def main():
        loop = asyncio.get_running_loop()
        order = []
        # 0.1 * 3 is 0.30000000000000004: later than 0.3 as floats, same microsecond.
        loop.call_at(0.1 * 3, order.append, "first")
        loop.call_at(0.3, order.append, "second")
        loop.call_at(0.3 - 2e-7, order.append, "third")
        loop.call_at(0.3000004, order.append, "fourth")
        loop.call_at(0.300001, order.append, "next microsecond")
        await asyncio.sleep(1)
        return order

    assert run_virtual(main) == ["first", "second", "third", "fourth", "next microsecond"]


def test_cancelled_timers_do_not_fire():
    async def main():
        loop = asyncio.get_running_loop()
        fired = []
        handles = [loop.call_later(0.5, fired.append, i) for i in range(200)]
        for h in handles[:150]:
            h.cancel()  # enough to make asyncio rebuild its heap
        await asyncio.sleep(1)
        return fired

    assert run_virtual(main) == list(range(150, 200))


def test_due_time_half_a_tick_past_the_grid_does_not_stall():
    async def main():
        loop = asyncio.get_running_loop()
        fired = []
        for k in range(1, 50):
            on_grid = round(k / 7 * 1_000_000) / 1_000_000
            loop.call_at(on_grid + 0.5e-6, fired.append, k)
        await asyncio.sleep(10)
        return fired

    assert run_virtual(main) == list(range(1, 50))


def test_a_deadlocked_run_fails_instead_of_hanging(monkeypatch):
    from earful.scripted import clock

    monkeypatch.setattr(clock, "STALL_SECONDS", 0.2)

    async def main():
        await asyncio.get_running_loop().create_future()

    with pytest.raises(clock.VirtualTimeStall):
        run_virtual(main)


async def _nothing():
    return None


def test_a_model_defined_after_the_first_run_reads_the_virtual_clock():
    """A ``default_factory=time.time`` field holds the real function, so every run patches
    the model classes that arrived since the last."""
    run_virtual(_nothing)

    class Late(BaseModel):
        at: float = Field(default_factory=time.time)

    async def main():
        await asyncio.sleep(5)
        return Late().at

    assert run_virtual(main) == WALL_CLOCK_EPOCH + 5
    assert any(name.endswith("Late.at") for name in patch_report().model_fields)
    assert abs(Late().at - time.time()) < 60  # outside a run it reads the real clock


def test_a_wake_up_from_a_thread_that_was_already_running_is_counted():
    ask = threading.Event()
    loops = []

    def work():
        ask.wait()
        loops[0].call_soon_threadsafe(answer.set_result, "found")

    thread = threading.Thread(target=work)
    thread.start()  # before the run: the loop never sees it start

    async def main():
        nonlocal answer
        loop = asyncio.get_running_loop()
        answer = loop.create_future()
        loops.append(loop)
        ask.set()
        return await answer, left_the_clock(loop)

    answer = None
    assert run_virtual(main) == (
        "found",
        ["work left the virtual clock (wake-ups from another thread: 1)"],
    )
    thread.join()


def test_a_socket_the_loop_waits_on_is_counted():
    async def main():
        loop = asyncio.get_running_loop()
        near, far = socket.socketpair()
        near.setblocking(False)
        reader, writer = await asyncio.open_connection(sock=near)
        far.sendall(b"found\n")
        line = await reader.readline()
        writer.close()
        far.close()
        return line, left_the_clock(loop)

    line, left = run_virtual(main)
    assert line == b"found\n"
    assert left == ["work left the virtual clock (file descriptors watched: 1)"]


def test_a_descriptor_the_loop_waits_to_write_to_is_counted():
    async def main():
        loop = asyncio.get_running_loop()
        near, far = socket.socketpair()
        writable = loop.create_future()
        loop.add_writer(near, writable.set_result, None)
        await writable
        loop.remove_writer(near)
        near.close()
        far.close()
        return left_the_clock(loop)

    assert run_virtual(main) == ["work left the virtual clock (file descriptors watched: 1)"]


def test_a_child_process_started_through_a_shell_is_counted():
    async def main():
        child = await asyncio.create_subprocess_shell("true")
        code = await child.wait()  # nothing else is scheduled, so the loop waits for it
        return code, left_the_clock(asyncio.get_running_loop())

    code, (left,) = run_virtual(main)
    assert code == 0 and "child processes started: 1" in left


def test_work_handed_to_a_thread_pool_is_counted_as_before():
    async def main():
        loop = asyncio.get_running_loop()
        await asyncio.to_thread(lambda: None)
        return loop.executor_calls

    assert run_virtual(main) == 1


def test_a_loop_that_only_ran_its_own_timers_left_nothing():
    async def main():
        await asyncio.sleep(1)
        return left_the_clock(asyncio.get_running_loop())

    assert run_virtual(main) == []


_CLOCKS = ["monotonic", "monotonic_ns", "perf_counter", "perf_counter_ns", "time", "time_ns"]


def test_the_clock_functions_a_run_reads_from_the_loop_are_the_six_of_the_time_module():
    assert sorted(VIRTUAL_READS) == _CLOCKS


@pytest.mark.parametrize("name", _CLOCKS)
def test_a_clock_function_of_the_time_module_reads_the_loop_during_a_run(name):
    async def main():
        await asyncio.sleep(5)
        first, second = getattr(time, name)(), getattr(time, name)()
        return first, second

    real = getattr(time, name)
    first, second = run_virtual(main)
    scale = 1_000_000_000 if name.endswith("_ns") else 1
    epoch = WALL_CLOCK_EPOCH if name.startswith("time") else 0
    assert first == pytest.approx((epoch + 5) * scale)
    # The monotonic clock is the loop's own: two reads at one instant are equal.
    assert second == first if name.startswith("monotonic") else second > first
    assert getattr(time, name) is real  # and outside a run it is the real one again


def test_a_default_built_without_validation_reads_the_virtual_clock():
    class Stamped(BaseModel):
        at: float = Field(default_factory=time.time)

    async def main():
        await asyncio.sleep(3)
        return Stamped.model_construct().at

    assert run_virtual(main) == WALL_CLOCK_EPOCH + 3
    assert Stamped.model_fields["at"].default_factory is time.time  # as it was before the run


def _microseconds(seconds):
    return round(seconds * 1_000_000)


def test_a_log_record_reads_the_clock_without_moving_it():
    factory = logging.getLogRecordFactory()

    async def main():
        time.time()
        before = time.time()  # one step past the loop's time
        made = [logging.makeLogRecord({}).created for _ in range(5)]
        return before, made, time.time()

    before, made, after = run_virtual(main)
    assert [_microseconds(created - before) for created in made] == [0] * 5
    assert _microseconds(after - before) == 1
    assert logging.getLogRecordFactory() is factory


def test_a_logging_handler_that_reads_the_clock_does_not_move_it():
    class Reads(logging.Handler):
        def emit(self, record):
            self.read = time.time(), time.perf_counter()

    logger = logging.getLogger("earful.tests.clock")
    handler = Reads()
    logger.addHandler(handler)
    handle = logging.Logger.handle

    async def main():
        before = time.time()
        for _ in range(5):
            logger.warning("the handler reads the clock")
        return before, time.time()

    try:
        before, after = run_virtual(main)
    finally:
        logger.removeHandler(handler)
    assert _microseconds(handler.read[0] - before) == 0
    assert _microseconds(after - before) == 1
    assert logging.Logger.handle is handle


@pytest.mark.parametrize("virtual", [True, False])
def test_a_run_leaves_the_random_module_as_it_found_it(virtual):
    async def main():
        random.seed(7)  # as a run does with its seed
        return random.random()

    random.seed(123)
    before = random.getstate()
    drawn = run_virtual(main) if virtual else run_real(main, timeout=5)
    assert drawn == random.Random(7).random()
    assert random.getstate() == before


@pytest.mark.parametrize("virtual", [True, False])
def test_a_run_leaves_the_current_event_loop_in_place(virtual):
    mine = asyncio.new_event_loop()
    asyncio.set_event_loop(mine)
    try:
        run_virtual(_nothing) if virtual else run_real(_nothing, timeout=5)
        assert asyncio.get_event_loop() is mine
    finally:
        asyncio.set_event_loop(None)
        mine.close()


def test_conversion_is_exact_for_decimal_seconds():
    assert to_us(0.3) == 300_000 and to_us(5.5) == 5_500_000
    assert to_seconds(500_000) == 0.5
    # the subtraction a float clock would do is not exact; the integer one is
    assert 4.1 - 3.6 < 0.5
    assert to_seconds(to_us(4.1) - to_us(3.6)) == 0.5
