"""A scripted model's replies, and which reply a synthesized text belongs to."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from ...core.ledger import Ledger
from ...core.timebase import seconds, to_us
from ..clock import now_us
from .user import sleep_until


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

    def check_timing(self) -> None:
        seconds("ttft", self.ttft)
        seconds("duration", self.duration)
        if self.ttft > self.duration:
            raise ValueError(f"reply ttft {self.ttft} exceeds duration {self.duration}")


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
        A ``prefix`` match is a reply whose generation stopped early, inside a word or not."""
        words = _words(text)
        said = " ".join(words)
        open_ = [
            c
            for c in self.replies
            if c not in self.synthesized and (eligible is None or c in eligible)
        ]
        kinds: tuple[tuple[str, Callable[[list[str]], bool]], ...] = (
            ("unique", lambda w: w == words),
            ("prefix", lambda w: " ".join(w).startswith(said)),
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


def misfit(call: ToolCall, takes: Iterable[str], needs: Iterable[str]) -> str | None:
    """Why the arguments of ``call`` do not fit a tool with these parameters, or None."""
    try:
        given = json.loads(call.arguments)
    except ValueError:
        given = None
    if not isinstance(given, dict):
        return (
            f"the scripted model called {call.name!r} with arguments that are not a JSON "
            f"object: {call.arguments!r}"
        )
    takes, needs = sorted(takes), sorted(needs)
    if set(given) <= set(takes) and set(needs) <= set(given):
        return None
    return (
        f"the scripted model called {call.name!r} with the arguments {sorted(given)}; "
        f"the tool takes {takes} and needs {needs}"
    )


def reply_for(policy: Policy, items: list[dict[str, Any]], errors: list[str], call: int) -> Reply:
    try:
        reply = policy(items)
        reply.check_timing()
        return reply
    except Exception as exc:
        errors.append(f"policy raised on call {call}: {type(exc).__name__}: {exc}")
        raise


async def fragments(
    ledger: Ledger,
    call: int,
    reply: Reply,
    tool_calls: list[ToolCall],
    claims: list[dict[str, Any]],
    ttft: dict[int, int],
    durations: dict[int, int],
    register: ReplyRegister,
) -> AsyncIterator[str]:
    ttft_us = ttft.get(call, to_us(reply.ttft))
    duration_us = max(durations.get(call, to_us(reply.duration)), ttft_us)
    ledger.append(
        "llm",
        "llm.response",
        call=call,
        text=reply.text,
        tool_calls=[dataclasses.asdict(t) for t in tool_calls],
        claims=claims,
        ttft_us=ttft_us,
        duration_us=duration_us,
    )
    t0 = now_us()
    await sleep_until(t0, ttft_us)
    words = _words(reply.text)
    if words:
        register.replies[call] = words
    step = 3
    chunks = range(0, len(reply.text), step)
    for n, i in enumerate(chunks):
        at_us = ttft_us + (duration_us - ttft_us) * n // max(len(chunks) - 1, 1)
        await sleep_until(t0, at_us)
        yield reply.text[i : i + step]
    await sleep_until(t0, duration_us)
