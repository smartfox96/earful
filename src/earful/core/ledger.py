"""Append-only event ledger, its normalizer and its hash.

Framework-independent: this module must never import livekit.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

SCHEMA = "earful.ledger/5"

# Who observed the event. The checker treats these as independent witnesses.
SOURCES = ("world", "llm", "sink", "backend", "framework", "harness")

# Random identifiers: an alphabetic prefix ending in "_" followed by 12 hex characters
# (livekit's utils.shortuuid). Replaced by "<prefix>#<n>", n = order of first appearance.
DEFAULT_ID_PATTERN = re.compile(r"\b([A-Za-z][A-Za-z_]*_)[0-9a-f]{12}\b")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def run_id(header: dict[str, Any]) -> str:
    """The identity of a run: a digest of everything ``run.start`` declares.

    Two runs share an id exactly when they declare the same case, seed, framework release,
    clock, scenario and contract policy. What then happened is identified by the ledger
    hash, not by this.
    """
    return "run-" + hashlib.sha256(_canonical(header).encode("ascii")).hexdigest()[:16]


class Ledger:
    """Ordered list of events. ``seq`` is assigned on append and is the total order.

    Each event is a snapshot. ``append`` stores a copy of the data as JSON would hold it,
    so nothing a caller does to its own objects afterwards can alter the record, and data
    that cannot be written to a ledger file is refused where it is produced.
    """

    def __init__(self, clock_us: Callable[[], int], *, run: str | None = None) -> None:
        self._clock_us = clock_us
        self.run = run  # set by run.start unless given (a ledger fragment in a unit test)
        self.events: list[dict[str, Any]] = []

    def now_us(self) -> int:
        """The time an event recorded now would carry. Witnesses that compute times of
        their own read this clock, so those times and ``t_us`` share one base."""
        return self._clock_us()

    def append(self, source: str, kind: str, /, **data: Any) -> dict[str, Any]:
        if source not in SOURCES:
            raise ValueError(f"unknown ledger source: {source}")
        try:
            snapshot = json.loads(json.dumps(data, allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise TypeError(f"{kind}: event data must be plain JSON ({exc})") from exc
        if self.events and self.events[-1]["kind"] == "run.end":
            raise ValueError(f"{kind}: the ledger has ended; nothing follows run.end")
        if kind == "run.start" and self.run is None:
            self.run = run_id(snapshot)
        if self.run is None:
            raise ValueError("the first event of a ledger must be run.start")
        event = {
            "schema": SCHEMA,
            "run": self.run,
            "seq": len(self.events),
            "t_us": self._clock_us(),
            "source": source,
            "kind": kind,
            "data": snapshot,
        }
        self.events.append(event)
        return event

    def of(self, source: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        return [
            e
            for e in self.events
            if (source is None or e["source"] == source) and (kind is None or e["kind"] == kind)
        ]


def normalize(
    events: Iterable[dict[str, Any]], id_pattern: re.Pattern[str] = DEFAULT_ID_PATTERN
) -> list[str]:
    """Canonical JSON lines with random identifiers replaced by first-appearance ordinals."""
    ordinals: dict[str, str] = {}
    counters: dict[str, int] = {}

    def _replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        if raw not in ordinals:
            prefix = match.group(1)
            counters[prefix] = counters.get(prefix, 0) + 1
            ordinals[raw] = f"{prefix}#{counters[prefix]}"
        return ordinals[raw]

    return [id_pattern.sub(_replace, _canonical(event)) for event in events]


def ledger_hash(events: Iterable[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for line in normalize(events):
        digest.update(line.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def write_jsonl(events: Iterable[dict[str, Any]], path: Path, *, normalized: bool = False) -> None:
    events = list(events)
    lines = normalize(events) if normalized else [_canonical(e) for e in events]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="ascii").splitlines() if line]


def compare_view(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The ledger without the fields that differ between framework releases for reasons
    unrelated to behaviour, for comparing one case across releases.

    Removed: the release itself (``run.start``'s ``framework``, and the run id derived
    from it), and the framework's own ``created_us``. Each release reads the wall clock a
    different number of times per step, so those values drift by microseconds even when
    behaviour is identical. Every other field is kept, including virtual time ``t_us``.
    """
    view = []
    for event in events:
        event = {k: v for k, v in event.items() if k != "run"}
        if event["kind"] == "run.start":
            event["data"] = {k: v for k, v in event["data"].items() if k != "framework"}
        elif event["source"] == "framework" and "created_us" in event["data"]:
            event["data"] = {k: v for k, v in event["data"].items() if k != "created_us"}
        view.append(event)
    return view


def first_difference(a: list[str], b: list[str]) -> tuple[int, str | None, str | None] | None:
    """Index and content of the first differing normalized line, or None if identical."""
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i, x, y
    if len(a) != len(b):
        i = min(len(a), len(b))
        return i, (a[i] if i < len(a) else None), (b[i] if i < len(b) else None)
    return None
