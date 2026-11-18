"""The virtual clock, and the patch that makes pydantic models read it."""

import asyncio
import time

import pytest
from pydantic import BaseModel, Field

from earful.livekit.cases import CASES
from earful.livekit.clock import WALL_CLOCK_EPOCH, patch_report, run_virtual
from earful.livekit.harness import InvalidRun, run_case


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
    from earful.livekit import clock

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
