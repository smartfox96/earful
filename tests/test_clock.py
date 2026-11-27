"""The virtual clock. The patch that makes pydantic models read it: test_clock_models.py."""

import _thread
import asyncio
import logging
import math
import random
import socket
import subprocess
import sys
import threading
import time

import pytest

from earful.core.timebase import to_seconds, to_us
from earful.scripted.ceilings import SPIN_PASSES, livelocked
from earful.scripted.clock import (
    VIRTUAL_READS,
    WALL_CLOCK_EPOCH,
    left_the_clock,
    run_real,
    run_virtual,
)
from earful.scripted.timers import VirtualTimeStall


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


def test_a_run_reads_no_model_where_the_application_never_imported_pydantic():
    code = (
        "import sys\n"
        "sys.modules['pydantic'] = sys.modules['pydantic_core'] = None\n"  # importing either raises
        "from earful.scripted.clock import run_virtual\n"
        "async def main():\n"
        "    return 7\n"
        "print(run_virtual(main))\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert (done.returncode, done.stdout) == (0, "7\n"), done.stderr


def test_a_deadlocked_run_fails_instead_of_hanging(monkeypatch):
    from earful.scripted import ceilings, clock

    monkeypatch.setattr(ceilings, "STALL_SECONDS", 0.2)

    async def main():
        await asyncio.get_running_loop().create_future()

    with pytest.raises(clock.VirtualTimeStall):
        run_virtual(main)


async def _nothing():
    return None


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


def test_a_thread_started_without_threading_is_counted_once_and_a_thread_once():
    async def main():
        done = threading.Event()
        _thread.start_new_thread(done.set, ())
        ran = done.wait(5)
        worker = threading.Thread(target=lambda: None)
        worker.start()
        worker.join()
        return ran, left_the_clock(asyncio.get_running_loop())

    assert run_virtual(main) == (True, ["work left the virtual clock (threads started: 2)"])


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


async def _no_deadline(how):
    if how == "wait_for":
        sleeping = asyncio.ensure_future(asyncio.sleep(0.1, "ok"))
        try:
            return await asyncio.wait_for(sleeping, timeout=math.inf)
        finally:
            sleeping.cancel()
    asyncio.get_running_loop().call_later(math.inf, print).cancel()
    return "ok"


@pytest.mark.parametrize("how", ["wait_for", "call_later"])
def test_an_infinite_timeout_works_on_the_virtual_clock_as_on_a_real_one(how):
    assert asyncio.run(_no_deadline(how)) == "ok"
    assert run_virtual(lambda: _no_deadline(how)) == "ok"


def test_a_timer_due_at_minus_infinity_runs_at_once_as_on_a_real_one():
    async def main():
        loop = asyncio.get_running_loop()
        ran = loop.create_future()
        loop.call_at(-math.inf, ran.set_result, "ok")
        return await ran

    assert asyncio.run(main()) == "ok"
    assert run_virtual(main) == "ok"


async def _polls_with_sleep_zero():
    loop = asyncio.get_running_loop()
    done: list[float] = []
    loop.call_later(0.2, lambda: done.append(loop.time()))
    while not done:
        await asyncio.sleep(0)
    return done[0]


def test_a_task_that_never_waits_moves_the_clock_to_the_next_timer():
    assert run_virtual(_polls_with_sleep_zero) == 0.2


def test_a_task_that_never_waits_with_no_timer_to_move_to_stops_the_run():
    async def main():
        while True:
            await asyncio.sleep(0)

    with pytest.raises(VirtualTimeStall, match="passes at one instant"):
        run_virtual(main)


def test_a_task_left_behind_that_ignores_cancellation_cannot_hold_the_end_of_a_run():
    async def stubborn():
        while True:
            try:
                await asyncio.sleep(1000)
            except asyncio.CancelledError:
                pass

    async def main():
        asyncio.get_running_loop().create_task(stubborn())
        return "ended"

    assert run_virtual(main) == "ended"


def test_a_short_poll_with_sleep_zero_beside_a_frequent_timer_is_a_loop_that_waits():
    async def main():
        loop = asyncio.get_running_loop()
        for n in range(1, 101):
            loop.call_later(n * 0.05, lambda: None)  # as a microphone's frames
        done: list[int] = []
        loop.call_later(2.0, done.append, 1)  # as a slow backend
        while not done:
            await asyncio.sleep(0)
        return loop.time(), livelocked(loop)

    assert run_virtual(main) == (2.0, False)


def test_a_loop_that_never_waits_moves_to_its_last_timer_and_says_so():
    async def main():
        loop = asyncio.get_running_loop()
        ended = loop.create_future()
        loop.call_later(30.0, ended.set_result, None)  # as a run bounds its wait
        for n in range(1, 301):
            loop.call_later(n / 10, lambda: None)  # as a run that polls every 0.1 s
        passes = 0

        async def spins():
            nonlocal passes
            while True:
                passes += 1
                await asyncio.sleep(0)

        task = asyncio.create_task(spins())
        await ended
        task.cancel()
        return loop.time(), passes, livelocked(loop)

    at, passes, never_waits = run_virtual(main)
    assert at == 30.0 and never_waits
    assert passes < 55 * SPIN_PASSES  # five seconds of moves of the clock, not thirty
