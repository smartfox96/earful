"""The speech, tools and playout read from a ledger."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

Event = dict[str, Any]


@dataclass
class Speech:
    index: int
    start: int  # true start, absolute microseconds
    end: int
    transcript: str
    expects_reply: bool
    backchannel: bool
    retires: tuple[str, ...]
    # The VAD reports speeches closer than its silence window as one segment.
    vad_start: Event | None = None
    vad_end: Event | None = None
    segment_start: int = 0
    segment_end: int = 0
    final: Event | None = None

    @property
    def delivery(self) -> Event:
        if self.final is None:
            raise ValueError(f"speech {self.index} was not delivered")
        return self.final

    @property
    def noise(self) -> bool:
        return not self.transcript

    @property
    def words(self) -> list[str]:
        return self.transcript.split()


# The fields that tell one context item from another, by type; others compare by type.
_ITEM_KEYS = {
    "message": ("role", "text"),
    "function_call": ("name", "call_id", "arguments"),
    "function_call_output": ("call_id", "output"),
}


def could_be(text: list[str], reply: list[str]) -> bool:
    """Whether a synthesized text could be a reply altered on its way to the voice: they share
    a word, letters and digits compared without case. A text that shares none cannot be it."""

    def plain(words: list[str]) -> set[str]:
        return {"".join(ch for ch in w.lower() if ch.isalnum()) for w in words} - {""}

    return bool(plain(text) & plain(reply))


def unseeded(
    items: list[dict[str, Any]], seeded: list[dict[str, Any]], *, identify: bool = False
) -> list[dict[str, Any]]:
    """``items`` without the history the agent started with: each item of ``seeded`` in turn
    takes out the first item after the last one taken that is the same. One that is no longer
    there takes nothing out."""

    def key(item: dict[str, Any]) -> tuple[Any, ...]:
        return (item["type"], *(item.get(k) for k in _ITEM_KEYS.get(item["type"], ())))

    out, at = list(items), 0
    for item in seeded:
        found = next(
            (
                i
                for i in range(at, len(out))
                if key(out[i]) == key(item)
                and (not item.get("id") or out[i].get("id") == item["id"])
            ),
            None,
        )
        if found is not None:
            if identify and out[found].get("id"):
                item["id"] = out[found]["id"]
            del out[found]
            at = found
    return out


@dataclass
class ModelCall:
    call: int
    request: Event
    response: Event | None = None
    seeded: list[dict[str, Any]] = field(default_factory=list)  # the history it started with

    @property
    def answer(self) -> Event:
        if self.response is None:
            raise ValueError(f"model call {self.call} was not answered")
        return self.response

    @property
    def items(self) -> list[dict[str, Any]]:
        """The context, without the history the agent started with."""
        return unseeded(self.request["data"]["items"], self.seeded)

    @property
    def words(self) -> list[str]:
        return self.response["data"]["text"].split() if self.response else []

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        return self.response["data"]["tool_calls"] if self.response else []


@dataclass
class Utterance:
    utterance: int
    event: Event
    call: int | None
    attribution: str
    words: list[str]
    stamps: list[int]

    @property
    def audio_us(self) -> int:
        d = self.event["data"]
        return -(-sum(d["word_samples"]) * 1_000_000 // d["sample_rate"])


@dataclass
class Spoken:
    """Something the agent was scripted to say: a model reply, or text the application spoke."""

    key: str  # "reply 3" or "utterance 5"
    instance: dict[str, int]
    words: list[str]
    born: Event  # llm.response, or tts.synthesized for text the application spoke
    call: int | None


@dataclass
class Segment:
    number: int
    started: Event | None = None
    pauses: list[tuple[Event, Event | None]] = field(default_factory=list)  # (paused, resumed)
    finished: Event | None = None

    def gaps(self) -> list[tuple[Event, Event | None]]:
        """The pauses that silenced it: one that ended the instant it began did not."""
        return [(p, r) for p, r in self.pauses if r is None or r["t_us"] != p["t_us"]]

    def audible(self, until: int) -> list[tuple[int, int]]:
        """Intervals in which the segment was audible; one still open is closed at ``until``."""
        if self.started is None:
            return []
        out, opened = [], self.started["t_us"]
        for paused, resumed in self.gaps():
            out.append((opened, paused["t_us"]))
            if resumed is None:
                return out
            opened = resumed["t_us"]
        out.append((opened, self.finished["t_us"] if self.finished else until))
        return out


@dataclass
class ToolInvocation:
    tool: int
    name: str
    call_id: str | None
    called: Event
    outcome: Event | None = None


@dataclass
class Effect:
    request: int
    intent: str
    requested: Event
    outcome: Event | None = None
    timed_out: Event | None = None  # the caller stopped waiting; the write may still commit

    @property
    def committed(self) -> Event | None:
        return self.outcome if self.outcome and self.outcome["kind"] == "effect.committed" else None


@dataclass
class Playout:
    """What the sink did with one thing the agent was scripted to say."""

    key: str
    utterance: Utterance | None
    unattributable: tuple[str, str] | None  # the code and why its audio cannot be told apart
    interrupted: bool = False
    segments: list[Segment] = field(default_factory=list)
    settled: Event | None = None  # the event after which nothing more of it can play
    pending: bool = False  # the run ended while its audio may still have been in the sink
    played: int = 0  # words whose last sample was consumed
    cut: int = 0  # 1 if playout stopped inside one of its words
    completed_us: dict[int, int] = field(default_factory=dict)  # word index -> completion time
    first_audible_us: int | None = None
    outlasted_hangup: bool = False

    @property
    def unknown(self) -> tuple[str, str] | None:
        """The code and why what was played of it cannot be told, if it cannot."""
        if self.unattributable:
            return self.unattributable
        return ("run-ended", "the run ended before its playout finished") if self.pending else None
