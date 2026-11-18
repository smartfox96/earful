"""What a test calls: run a scenario, search it, keep a failure, replay one."""

from __future__ import annotations

from ..scripted.api import AdapterAPI, WorkersUnavailable
from . import scenario
from .harness import Case, RunResult, run_case

_api = AdapterAPI[Case, RunResult](scenario, run_case, scenario.run)
run = _api.run
explore = _api.explore
cross_check = _api.cross_check
keep = _api.keep
replay = _api.replay

__all__ = ["WorkersUnavailable", "cross_check", "explore", "keep", "replay", "run"]
