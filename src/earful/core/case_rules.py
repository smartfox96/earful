"""Semantic checks on the timeline and occurrences declared by a case."""

from __future__ import annotations

from collections.abc import Hashable
from typing import TypeVar

Key = TypeVar("Key", bound=Hashable)


def repeated(key: Key, seen: set[Key]) -> bool:
    if key in seen:
        return True
    seen.add(key)
    return False


def empty_speech(start: int, end: int) -> bool:
    return end <= start


def overlaps(previous_end: float, start: float) -> bool:
    return start < previous_end


def after_hangup(end: int, hangup: int) -> bool:
    return end > hangup


def wrong_wait(kind: str, wait: float | None) -> bool:
    return (wait is None) != (kind == "backend.error")
