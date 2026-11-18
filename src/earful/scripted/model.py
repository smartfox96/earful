"""A scripted model's replies, and which reply a synthesized text belongs to."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolCall:
    """A tool call in a scripted reply. The scripted model assigns ``call_id`` if left out."""

    name: str
    arguments: str  # JSON text
    call_id: str | None = None


@dataclass(frozen=True)
class Claim:
    """A span of a reply that tells the user ``intent`` succeeded.
    Word indices are 0-based and inclusive; ``last_word=None`` means the reply's last word."""

    intent: str
    first_word: int = 0
    last_word: int | None = None


@dataclass(frozen=True)
class Reply:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    ttft: float = 0.1  # time to first token
    duration: float = 0.3  # total generation time
    claims: tuple[Claim, ...] = ()


def _words(text: str) -> list[str]:
    return text.split()


@dataclass
class ReplyRegister:
    """Every reply the scripted model gave, by call. Shared with the TTS for attribution."""

    replies: dict[int, list[str]] = field(default_factory=dict)
    synthesized: set[int] = field(default_factory=set)

    def attribute(
        self, text: str, *, eligible: set[int] | None = None
    ) -> tuple[int | None, str, list[int]]:
        """Which unsynthesized reply a text belongs to, how it matched, and every candidate.
        A ``prefix`` match is a reply whose generation stopped early."""
        words = _words(text)
        open_ = [
            c
            for c in self.replies
            if c not in self.synthesized and (eligible is None or c in eligible)
        ]
        kinds: tuple[tuple[str, Callable[[list[str]], bool]], ...] = (
            ("unique", lambda w: w == words),
            ("prefix", lambda w: len(w) > len(words) and w[: len(words)] == words),
        )
        for kind, fits in kinds:
            hits = sorted(c for c in open_ if words and fits(self.replies[c]))
            if len(hits) == 1:
                self.synthesized.add(hits[0])
                return hits[0], kind, hits
            if len(hits) > 1:
                return None, "ambiguous", hits
        return None, "none", []


# A model policy: a pure function of the context, which it sees as item summaries.
Policy = Callable[[list[dict[str, Any]]], Reply]
