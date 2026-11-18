"""Compatibility imports for the shared virtual clock."""

from ..scripted.clock import (
    STALL_SECONDS,
    WALL_CLOCK_EPOCH,
    ClockPatchReport,
    VirtualTimeLoop,
    VirtualTimeStall,
    now_us,
    patch_report,
    run_virtual,
    virtual_wall_clock,
)

__all__ = [
    "STALL_SECONDS",
    "WALL_CLOCK_EPOCH",
    "ClockPatchReport",
    "VirtualTimeLoop",
    "VirtualTimeStall",
    "now_us",
    "patch_report",
    "run_virtual",
    "virtual_wall_clock",
]
