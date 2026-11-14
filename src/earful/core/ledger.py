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

SCHEMA = "earful.ledger/0"

# Who observed the event. The checker treats these as independent witnesses.
SOURCES = ("world", "llm", "sink", "backend", "framework", "harness")

# Random identifiers: an alphabetic prefix ending in "_" followed by 12 hex characters
# (livekit's utils.shortuuid). Replaced by "<prefix>#<n>", n = order of first appearance.
DEFAULT_ID_PATTERN = re.compile(r"\b([A-Za-z][A-Za-z_]*_)[0-9a-f]{12}\b")


class Ledger:
    """Ordered list of events. ``seq`` is assigned on append and is the total order."""

    def __init__(self, clock_us: Callable[[], int]) -> None:
        self._clock_us = clock_us
        self.events: list[dict[str, Any]] = []

    def append(self, source: str, kind: str, /, **data: Any) -> dict[str, Any]:
        if source not in SOURCES:
            raise ValueError(f"unknown ledger source: {source}")
        event = {
            "schema": SCHEMA,
            "seq": len(self.events),
            "t_us": self._clock_us(),
            "source": source,
            "kind": kind,
            "data": data,
        }
        self.events.append(event)
        return event

    def of(self, source: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        return [
            e
            for e in self.events
            if (source is None or e["source"] == source) and (kind is None or e["kind"] == kind)
        ]


def _canonical(event: dict[str, Any]) -> str:
    return json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


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


def first_difference(a: list[str], b: list[str]) -> tuple[int, str | None, str | None] | None:
    """Index and content of the first differing normalized line, or None if identical."""
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i, x, y
    if len(a) != len(b):
        i = min(len(a), len(b))
        return i, (a[i] if i < len(a) else None), (b[i] if i < len(b) else None)
    return None
