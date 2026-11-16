"""Time in Earful is an integer count of microseconds.

Scenario authors write seconds, because that is the unit the frameworks under test use.
Earful converts once, at the edge, and does all of its own arithmetic on integers. A
float appears again only where a framework API requires one, produced by a single
division of an exact integer. ``500_000 / 1_000_000`` is exactly the float a framework
wrote as ``0.5``, so a comparison against a framework threshold is decided by the times
themselves and never by accumulated rounding.

Framework-independent: this module must never import livekit.
"""

from __future__ import annotations

US_PER_SECOND = 1_000_000


def to_us(seconds: float) -> int:
    """Seconds, as an author wrote them, to the nearest microsecond."""
    return round(seconds * US_PER_SECOND)


def to_seconds(us: int) -> float:
    """Microseconds to the float nearest that many seconds, for a framework API."""
    return us / US_PER_SECOND


__all__ = ["US_PER_SECOND", "to_seconds", "to_us"]
