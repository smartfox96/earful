"""Time in Earful is an integer count of microseconds; seconds are converted at the edge."""

from __future__ import annotations

US_PER_SECOND = 1_000_000


def to_us(seconds: float) -> int:
    """Seconds, as an author wrote them, to the nearest microsecond."""
    return round(seconds * US_PER_SECOND)


def to_seconds(us: int) -> float:
    """Microseconds to the float nearest that many seconds, for a framework API."""
    return us / US_PER_SECOND


__all__ = ["US_PER_SECOND", "to_seconds", "to_us"]
