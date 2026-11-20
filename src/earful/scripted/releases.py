"""The framework releases each adapter is tested on, and the range its extra accepts."""

from __future__ import annotations

import contextlib
import re
import warnings
from collections.abc import Iterator
from dataclasses import dataclass


class UntestedRelease(UserWarning):
    """The installed framework release is accepted and has no tests behind it."""


@dataclass(frozen=True)
class Support:
    adapter: str
    extra: str
    tested: tuple[str, ...]
    lowest: str
    below: str

    @property
    def requirement(self) -> str:
        return f">={self.lowest},<{self.below}"


SUPPORT = {
    "livekit-agents": Support(
        adapter="earful.livekit",
        extra="livekit",
        tested=("1.5.6", "1.5.7", "1.6.5", "1.6.6", "1.8.12"),
        lowest="1.5.6",
        below="1.9",
    ),
    "pipecat-ai": Support(
        adapter="earful.pipecat",
        extra="pipecat",
        tested=("1.16.0",),
        lowest="1.16",
        below="1.17",
    ),
}


def _numbers(version: str) -> tuple[int, ...] | None:
    leading = re.match(r"\d+(?:\.\d+)*", version)
    return tuple(int(part) for part in leading.group().split(".")) if leading else None


def admit(package: str, installed: str) -> None:
    """Refuse a release outside the range, and warn about one inside it that is not tested."""
    support = SUPPORT[package]
    numbers = _numbers(installed)
    lowest, below = _numbers(support.lowest), _numbers(support.below)
    assert lowest is not None and below is not None
    if numbers is None or not lowest <= numbers < below:
        raise ImportError(
            f"{support.adapter} supports {package} from {support.lowest} up to, and not "
            f"including, {support.below}, and {installed} is installed. "
            f'Install a supported release with: pip install "earful[{support.extra}]"'
        )
    if installed not in support.tested:
        warnings.warn(
            f"Earful is not tested on {package} {installed}. "
            f"The tested releases are: {', '.join(support.tested)}",
            UntestedRelease,
            stacklevel=2,
        )


@contextlib.contextmanager
def importing(package: str, installed: str) -> Iterator[None]:
    """Around an adapter's imports: ``admit``, and one line if an untested release lacks a name."""
    admit(package, installed)
    support = SUPPORT[package]
    try:
        yield
    except (ImportError, AttributeError) as exc:
        if installed in support.tested:
            raise
        raise ImportError(
            f"{support.adapter} cannot be imported on {package} {installed}, which is not a "
            f"tested release ({exc}). The tested releases are: {', '.join(support.tested)}"
        ) from exc


def quiet_worker() -> None:
    """A worker process imports the adapter again: the parent has already warned."""
    warnings.simplefilter("ignore", UntestedRelease)
