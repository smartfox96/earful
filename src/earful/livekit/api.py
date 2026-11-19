"""What a test calls: run a scenario, search it, keep a failure, replay one."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from typing import Any

from ..core import case as cases
from ..scripted.api import AdapterAPI, WorkersUnavailable
from . import scenario
from .harness import Case, RunResult, run_case


@contextlib.contextmanager
def _quiet_framework() -> Iterator[None]:
    # A search makes hundreds of runs, and the framework logs a traceback per tool error.
    logger = logging.getLogger("livekit")
    before = logger.propagate
    logger.propagate = False
    try:
        yield
    finally:
        logger.propagate = before


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
