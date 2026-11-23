"""Every LiveKit shape on the installed release, as it is and varied (tests/shapes/livekit)."""

from __future__ import annotations

import dataclasses
import importlib
from collections.abc import AsyncIterable
from typing import Any

import pytest

pytest.importorskip("livekit.agents")

import invariants
import livekit.agents as lk
from livekit.agents import llm as lk_llm
from livekit.agents.types import FlushSentinel
from shapes import corpus

from earful.livekit import run_case

ADAPTER = "earful.livekit:run_case"
SHAPES = corpus.load("livekit")


def _with_history(case: Any, original: corpus.Seen) -> Any:
    user, reply = invariants.earlier_exchange(case, original)
    make = case.agent

    def agent() -> lk.Agent:
        made = make()
        earlier = lk_llm.ChatContext()
        earlier.add_message(role="user", content=user)
        earlier.add_message(role="assistant", content=reply)
        made._chat_ctx.items[:0] = earlier.items  # what Agent(chat_ctx=...) would hold
        return made

    return dataclasses.replace(case, agent=agent)


def _text(chunk: Any) -> str:
    if isinstance(chunk, str):
        return chunk
    return getattr(getattr(chunk, "delta", None), "content", None) or ""


def _in_two_parts(case: Any, original: corpus.Seen) -> Any:
    """Each reply goes to the voice in two parts, split after its first word."""
    make = case.agent

    def agent() -> lk.Agent:
        made = make()
        node = made.llm_node

        async def llm_node(*args: Any) -> AsyncIterable[Any]:
            said, flushed = "", False
            async for chunk in node(*args):
                yield chunk
                said += _text(chunk)
                if not flushed and said.strip() and said[-1] in " .,!?":
                    flushed = True
                    yield FlushSentinel()

        made.llm_node = llm_node
        return made

    return dataclasses.replace(case, agent=agent)


VARIATIONS = [
    invariants.SHIFT_EVERY_TIME,
    invariants.LONGER_DELAYS,
    invariants.Variation(invariants.HISTORY, _with_history, invariants.same_verdicts),
    invariants.Variation(invariants.TWO_PARTS, _in_two_parts, invariants.same_verdicts),
]


@pytest.mark.parametrize("shape", corpus.params(SHAPES, lk.__version__))
def test_shape(shape: corpus.Shape) -> None:
    corpus.judge(shape.should, shape.defect, corpus.run(shape, ADAPTER, run_case))


@pytest.mark.parametrize("shape, variation", invariants.params(SHAPES, VARIATIONS))
def test_variation(shape: corpus.Shape, variation: invariants.Variation) -> None:
    module = importlib.import_module(shape.module)
    original = corpus.observe(run_case, module.case())
    assert variation.vary is not None
    varied = corpus.observe(run_case, variation.vary(module.case(), original))
    invariants.judge(shape, variation, original, varied)


def test_every_shape_is_declared_whole():
    corpus.check_declared(SHAPES, invariants.NAMES)


def test_every_variation_applies_or_says_why_not():
    assert [variation.name for variation in VARIATIONS] == list(invariants.NAMES)
    assert all(variation.vary or variation.why for variation in VARIATIONS)
