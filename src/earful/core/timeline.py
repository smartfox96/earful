"""A ledger as text, for a person: what was run, the verdicts, and every event in order."""

from __future__ import annotations

import json
from typing import Any

from .checker import Report, check
from .ledger import ledger_hash

Event = dict[str, Any]


def heading(events: list[Event]) -> list[str]:
    """What the ledger is a run of. Empty for a ledger that does not begin with a header."""
    first = events[0] if events else {}
    if first.get("kind") != "run.start":
        return []
    header = first["data"]
    framework = header.get("framework", {})
    return [
        f"case       {header.get('case')}  (seed {header.get('seed')})",
        f"framework  {framework.get('name')} {framework.get('version')}",
        f"clock      {header.get('clock')}",
        f"run        {first.get('run')}",
        f"hash       {ledger_hash(events)}",
    ]


def rows(events: list[Event], *, width: int = 150) -> list[str]:
    """One line per event, its data cut to ``width`` characters."""
    out = []
    for event in events:
        data = json.dumps(event.get("data"), sort_keys=True)
        if len(data) > width:
            data = data[: width - 4] + " ..."
        out.append(
            f"{event.get('seq', '?'):>4} {event.get('t_us', 0) / 1_000_000:>10.6f} "
            f"{event.get('source', '?'):<9} {event.get('kind', '?'):<27} {data}"
        )
    return out


def text(events: list[Event], report: Report | None = None, *, width: int = 150) -> str:
    """The run as text: heading, verdicts, then every event."""
    report = check(events) if report is None else report
    parts = [heading(events), report.lines(), rows(events, width=width)]
    return "\n\n".join("\n".join(part) for part in parts if part) + "\n"


__all__ = ["heading", "rows", "text"]
