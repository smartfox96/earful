"""Append-only event ledger, its normalizer and its hash."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

SCHEMA = "earful.ledger/1"
SCHEMA_MINOR = 0

# Who observed the event. The checker treats these as independent witnesses.
SOURCES = ("world", "llm", "sink", "backend", "framework", "harness")

# Random identifiers such as livekit's shortuuid: a prefix ending in "_", then 12 hex digits.
DEFAULT_ID_PATTERN = re.compile(r"\b([A-Za-z][A-Za-z_]*_)[0-9a-f]{12}\b")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def json_value(value: Any) -> Any:
    """``value`` as JSON can hold it. Anything else becomes ``{"unrepresentable": <type>}``,
    since a default ``repr`` would put a memory address in the ledger."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return json_value(dataclasses.asdict(value))
    dump = getattr(value, "model_dump", None)  # pydantic models
    if callable(dump):
        return json_value(dump(mode="json"))
    return {"unrepresentable": type(value).__name__}


def run_id(header: dict[str, Any]) -> str:
    """The identity of a run: a digest of everything ``run.start`` declares."""
    return "run-" + hashlib.sha256(_canonical(header).encode("ascii")).hexdigest()[:16]


class Ledger:
    """Ordered list of events. ``seq`` is assigned on append and is the total order.
    ``append`` snapshots its data as JSON and raises ``TypeError`` for data JSON cannot hold."""

    def __init__(self, clock_us: Callable[[], int], *, run: str | None = None) -> None:
        self._clock_us = clock_us
        self.run = run  # set by run.start unless given
        self.events: list[dict[str, Any]] = []

    def now_us(self) -> int:
        """The time an event recorded now would carry."""
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
            "schema_minor": SCHEMA_MINOR,
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


def write_jsonl(
    events: Iterable[dict[str, Any]], path: str | os.PathLike[str], *, normalized: bool = False
) -> None:
    """Write a ledger file, one event per line. ``normalized`` writes the form that is hashed."""
    events = list(events)
    lines = normalize(events) if normalized else [_canonical(e) for e in events]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def read_jsonl(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """The events of a ledger file, in order."""
    text = Path(path).read_text(encoding="ascii")
    return [json.loads(line) for line in text.splitlines() if line]


def compare_view(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The ledger without the fields that differ between framework releases whatever the
    behaviour: the release, the run id derived from it and the framework's ``created_us``."""
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
