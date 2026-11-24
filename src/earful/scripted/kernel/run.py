"""Drive one case on its chosen clock and record the adapter's observations."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

from ...core.backend import EffectBackend
from ...core.ledger import Ledger, ledger_hash
from ...core.schema import validity
from ...core.timebase import to_us
from ..case import Case, DrainOptions, InvalidRun
from ..clock import (
    VirtualTimeLoop,
    left_the_clock,
    now_us,
    patch_report,
    run_real,
    run_virtual,
)
from ..result import RunEvidence, RunResult
from ..timing import REAL_CLOCK_ORIGIN_TOLERANCE_US, WorldOrigin
from .lifecycle import drive
from .ports import AdapterPorts

REAL_CLOCK_TIMEOUT = 120.0

CaseT = TypeVar("CaseT", bound=Case)
ResultT = TypeVar("ResultT", bound=RunEvidence)


@dataclass
class RunContext:
    ledger: Ledger
    virtual: bool
    errors: list[str]
    backend: EffectBackend
    declared_us: Callable[[str], dict[int, int]]
    origin: WorldOrigin
    disconnected: asyncio.Event
    warnings: list[str] = field(default_factory=list)
    stamp_errors: int = 0
    underruns: int = 0
    executor_calls: int | None = None


def run(
    case: CaseT,
    seed: int,
    *,
    drain: DrainOptions,
    virtual: bool,
    require_valid: bool,
    framework: dict[str, str],
    validate: Callable[[CaseT, int], None],
    build: AdapterPorts[CaseT],
    result_of: Callable[[RunResult, RunContext], ResultT],
) -> ResultT:
    async def execute() -> ResultT:
        validate(case, seed)
        random.seed(seed)
        loop = asyncio.get_running_loop()
        on_virtual_clock = isinstance(loop, VirtualTimeLoop)
        if on_virtual_clock:
            ledger = Ledger(now_us)
        else:
            began_us = now_us()
            ledger = Ledger(lambda: now_us() - began_us)
        errors = [
            f"{name} reads the wall clock and could not be given the virtual one"
            for name in (patch_report().unpatched if on_virtual_clock else ())
        ]

        def declared_us(of: str) -> dict[int, int]:
            return {delay.nth: to_us(delay.seconds) for delay in case.delays if delay.of == of}

        backend = EffectBackend(
            ledger,
            mode=case.backend_mode,
            latencies_us=declared_us("backend.latency"),
            faults={
                f.nth: (f.of, None if f.seconds is None else to_us(f.seconds)) for f in case.faults
            },
        )
        origin = WorldOrigin(
            ledger, errors, tolerance_us=0 if on_virtual_clock else REAL_CLOCK_ORIGIN_TOLERANCE_US
        )
        context = RunContext(
            ledger, on_virtual_clock, errors, backend, declared_us, origin, asyncio.Event()
        )
        async with build(case, context) as ports:
            ledger.append(
                "harness",
                "run.start",
                case=case.name,
                seed=seed,
                framework=framework,
                clock="virtual" if on_virtual_clock else "real",
                scenario=ports.config(),
                contracts=case.contracts,
            )
            completion = await ports.start()
            await drive(case, drain, context, ports, completion)
            ledger.append("harness", "history.final", **ports.history())
            await ports.close()

        context.executor_calls = loop.executor_calls if isinstance(loop, VirtualTimeLoop) else None
        errors += left_the_clock(loop)
        ledger.append(
            "harness",
            "run.end",
            stamp_errors=context.stamp_errors,
            executor_calls=context.executor_calls,
            underruns=context.underruns,
            harness_errors=errors,
            framework_warnings=len(context.warnings),
        )
        result = RunResult(
            case.name, seed, ledger, ledger_hash(ledger.events), validity(ledger.events), errors
        )
        return result_of(result, context)

    result = run_virtual(execute) if virtual else run_real(execute, timeout=REAL_CLOCK_TIMEOUT)
    if result.invalid and require_valid:
        raise InvalidRun(result.invalid, result)
    return result
