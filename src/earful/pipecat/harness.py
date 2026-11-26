"""Run a Pipecat case and return its recorded evidence."""

from __future__ import annotations

import pipecat

from ..scripted.kernel.run import run
from ..scripted.releases import framework_info
from ..scripted.result import RunResult
from .case import Case
from .clock import wait_for_of_python_3_12
from .ports import build


def validate(case: Case, seed: int) -> None:
    if "*" in case.effects:
        raise ValueError(
            "effects must name the function that writes; a catch-all effect '*' is ambiguous"
        )
    if not callable(case.bot):
        raise ValueError(  # noqa: TRY004 - a case that is refused, as check_case refuses one
            "bot is a function that is handed the model service and returns the context; "
            f"got an object of {type(case.bot).__name__}"
        )
    from .scenario import INLINE, describe

    describe(case, INLINE, seed)


def run_case(
    case: Case, seed: int = 0, *, virtual: bool = True, require_valid: bool = True
) -> RunResult:
    """Run a fresh pipeline and raise InvalidRun if its ledger cannot be judged."""
    with wait_for_of_python_3_12():
        return run(
            case,
            seed,
            drain=case,
            virtual=virtual,
            require_valid=require_valid,
            framework=framework_info("pipecat-ai", pipecat.__version__),
            validate=validate,
            build=build,
            result_of=lambda result, _: result,
        )


__all__ = ["Case", "RunResult", "run_case"]
