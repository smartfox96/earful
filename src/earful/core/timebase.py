"""Time in Earful is an integer count of microseconds; seconds are converted at the edge."""

from __future__ import annotations

import sys

US_PER_SECOND = 1_000_000


def seconds(name: str, value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 <= value <= sys.float_info.max / US_PER_SECOND
    ):
        raise ValueError(
            f"{name} must be finite nonnegative seconds within microsecond range, got {value!r}"
        )
    return value


def to_us(seconds: float) -> int:
    """Seconds, as an author wrote them, to the nearest microsecond."""
    return round(seconds * US_PER_SECOND)


def to_seconds(us: int) -> float:
    """Microseconds to the float nearest that many seconds, for a framework API."""
    try:
        return us / US_PER_SECOND
    except OverflowError as exc:
        raise ValueError("time in microseconds exceeds the supported range") from exc


__all__ = ["US_PER_SECOND", "to_seconds", "to_us"]
