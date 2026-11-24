"""The declarations common to every run.start scenario."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..core.backend import Effect
from ..core.timebase import to_us
from .case import Case, declared_intents


def declarations(
    case: Case, witnessed: Iterable[str], effects: dict[str, Effect]
) -> dict[str, Any]:
    intents = declared_intents(case.intents, effects, case.speeches, case.contracts)
    header: dict[str, Any] = {
        "speeches": [
            {
                "speech": i,
                "start_us": sp.start_us,
                "end_us": sp.end_us,
                "transcript": sp.transcript,
                "stt_delay_us": sp.stt_delay_us,
                "expects_reply": sp.owes_reply,
                "backchannel": sp.backchannel,
                "retires": list(sp.retires),
            }
            for i, sp in enumerate(sorted(case.speeches, key=lambda sp: sp.start))
        ],
        "hangup_us": None if case.hangup is None else to_us(case.hangup),
        "intents": [
            {"intent": it.intent, "tools": list(it.tools), "authorized": it.authorized}
            for it in intents
        ],
        "witnessed_tools": sorted(witnessed),
        "delays": [
            {"of": delay.of, "nth": delay.nth, "us": to_us(delay.seconds)}
            for delay in sorted(case.delays, key=lambda d: (d.of, d.nth))
        ],
        "faults": [
            {
                "of": fault.of,
                "nth": fault.nth,
                "us": None if fault.seconds is None else to_us(fault.seconds),
            }
            for fault in sorted(case.faults, key=lambda f: (f.of, f.nth))
        ],
    }
    if effects:
        header["effects"] = [
            {"tool": tool, "intent": declared.intent} for tool, declared in sorted(effects.items())
        ]
    return header
