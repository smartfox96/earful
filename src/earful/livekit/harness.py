"""Run a LiveKit case and return its recorded evidence."""

from __future__ import annotations

import livekit.agents as lk
from livekit import rtc

from ..scripted.case import (
    DELAYS,
    Delay,
    Fault,
    Intent,
    InvalidRun,
)
from ..scripted.clock import (
    patch_report,
)
from ..scripted.kernel.run import RunContext, run
from ..scripted.result import RunResult as ScriptedResult
from .case import Case, RunResult
from .ports import build


def versions() -> dict[str, str]:
    import platform

    return {
        "livekit-agents": lk.__version__,
        "livekit": rtc.__version__,
        "python": platform.python_version(),
        "platform": f"{platform.system()}-{platform.machine()}",
    }


def run_case(
    case: Case, seed: int = 0, *, virtual: bool = True, require_valid: bool = True
) -> RunResult:
    """Run ``case`` once on a fresh loop. Raises ``InvalidRun``, which carries the result,
    if the run is not valid evidence, unless ``require_valid`` is false. ``virtual=False``
    runs on the real clock as a negative control: such a run is never valid."""

    return run(
        case,
        seed,
        drain=case,
        virtual=virtual,
        require_valid=require_valid,
        framework={"name": "livekit-agents", "version": lk.__version__},
        validate=lambda candidate, run_seed: candidate.check(run_seed),
        build=build,
        result_of=_result,
    )


def _result(result: ScriptedResult, context: RunContext) -> RunResult:
    return RunResult(
        case=result.case,
        seed=result.seed,
        ledger=result.ledger,
        hash=result.hash,
        versions=versions(),
        clock=patch_report(),
        stamp_errors=context.stamp_errors,
        executor_calls=context.executor_calls,
        underruns=context.underruns,
        harness_errors=result.harness_errors,
        framework_warnings=context.warnings,
        invalid=result.invalid,
    )


__all__ = [
    "DELAYS",
    "Case",
    "Delay",
    "Fault",
    "Intent",
    "InvalidRun",
    "RunResult",
    "run_case",
    "versions",
]
