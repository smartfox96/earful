"""What a test calls: run a scenario, search it, keep a failure, replay one."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

from loguru import logger

from ..core import case as cases
from ..scripted.api import AdapterAPI, WorkersUnavailable
from . import scenario
from .harness import Case, RunResult, run_case

_quiet = 0  # searches under way in this process


@contextlib.contextmanager
def _quiet_framework() -> Iterator[None]:
    # A search makes hundreds of runs, and Pipecat logs every frame it handles.
    global _quiet
    _quiet += 1
    logger.disable("pipecat")
    try:
        yield
    finally:
        _quiet -= 1
        if not _quiet:
            logger.enable("pipecat")


# At module level so that it can be sent to a worker process.
def _run_quietly(data: cases.Case) -> list[dict[str, Any]]:
    with _quiet_framework():
        return scenario.run(data)


_api = AdapterAPI[Case, RunResult](scenario, run_case, _run_quietly, _quiet_framework)
run = _api.run
explore = _api.explore
cross_check = _api.cross_check
keep = _api.keep
replay = _api.replay

__all__ = ["WorkersUnavailable", "cross_check", "explore", "keep", "replay", "run"]
