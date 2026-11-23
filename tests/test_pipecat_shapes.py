"""Every Pipecat shape on the installed release, as it is and varied (tests/shapes/pipecat)."""

from __future__ import annotations

import dataclasses
import importlib
from typing import Any

import pytest

pytest.importorskip("pipecat")

import invariants
import pipecat
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.llm_service import LLMService
from shapes import corpus

from earful.pipecat import run_case

ADAPTER = "earful.pipecat:run_case"
SHAPES = corpus.load("pipecat")


def _with_history(case: Any, original: corpus.Seen) -> Any:
    user, reply = invariants.earlier_exchange(case, original)
    make = case.bot

    def bot(llm: LLMService) -> LLMContext:
        context = make(llm)
        earlier = [{"role": "user", "content": user}, {"role": "assistant", "content": reply}]
        context.set_messages([*earlier, *context.messages])
        return context

    return dataclasses.replace(case, bot=bot)


VARIATIONS = [
    invariants.SHIFT_EVERY_TIME,
    invariants.LONGER_DELAYS,
    invariants.Variation(invariants.HISTORY, _with_history, invariants.same_verdicts),
    invariants.Variation(
        invariants.TWO_PARTS,
        None,
        invariants.same_verdicts,
        why="the voice gets every reply one sentence at a time: a shape's reply of two "
        "sentences is voiced in two parts in each run",
    ),
]


@pytest.mark.parametrize("shape", corpus.params(SHAPES, pipecat.__version__))
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
