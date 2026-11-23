"""The patch that makes pydantic models read the virtual clock. Both frameworks install
pydantic, and Earful alone does not: scripts/ran_every_test.sh --without pydantic."""

import asyncio
import time

import pytest

pytest.importorskip("pydantic")

from pydantic import BaseModel, Field

from earful.scripted.clock import WALL_CLOCK_EPOCH, patch_report, run_virtual


async def _nothing():
    return None


def test_a_model_defined_after_the_first_run_reads_the_virtual_clock():
    """A ``default_factory=time.time`` field holds the real function, so every run patches
    the model classes that arrived since the last."""

    class Early(BaseModel):  # a model the first run looks at, whatever else is imported
        at: float = 0.0

    run_virtual(_nothing)

    class Late(BaseModel):
        at: float = Field(default_factory=time.time)

    async def main():
        await asyncio.sleep(5)
        return Late().at

    assert run_virtual(main) == WALL_CLOCK_EPOCH + 5
    assert any(name.endswith("Late.at") for name in patch_report().model_fields)
    assert abs(Late().at - time.time()) < 60  # outside a run it reads the real clock


def test_a_default_built_without_validation_reads_the_virtual_clock():
    class Stamped(BaseModel):
        at: float = Field(default_factory=time.time)

    async def main():
        await asyncio.sleep(3)
        return Stamped.model_construct().at

    assert run_virtual(main) == WALL_CLOCK_EPOCH + 3
    assert Stamped.model_fields["at"].default_factory is time.time  # as it was before the run
