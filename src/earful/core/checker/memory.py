"""Reading a model context: which user turns it holds, and which scripted speech each
assistant message is.

Shared by the memory contracts (M1, M2) and by L1, which needs to know when a turn first
appeared in a context.

Framework-independent: this module must never import livekit.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from typing import Any

from .view import Speech, Spoken

Item = dict[str, Any]


def _words(item: Item) -> list[str]:
    return (item.get("text") or "").split()


@dataclass
class UserTurns:
    present: list[Speech] = field(default_factory=list)  # delivered turns found, in order
    ended_at: dict[int, int] = field(default_factory=dict)  # speech index -> item index
    stray: list[str] = field(default_factory=list)  # user words that match no delivered turn


def user_turns(items: list[Item], delivered: list[Speech]) -> UserTurns:
    """Match the user text of a context against the transcripts delivered before it.

    The user text is read as one stream of words, so a framework that merges two
    transcripts into one message, or splits one across two, is not penalized. Each
    delivered transcript must appear whole and in order. A backchannel may be present or
    absent. Reading stops at the first other transcript that is not there: whatever user
    text is left over was invented, duplicated or reordered.

    Where a backchannel's words could be either the backchannel or the start of the next
    turn, the reading that leaves the least text unexplained is taken, and among those
    the one that finds the most turns that are owed a place in memory.
    """
    stream: list[tuple[str, int]] = [
        (word, index)
        for index, item in enumerate(items)
        if item["type"] == "message" and item.get("role") == "user"
        for word in _words(item)
    ]
    texts = [w for w, _ in stream]

    @cache
    def best(i: int, at: int) -> tuple[int, int, int, tuple[int, ...]]:
        """(stray words, -turns found, -all found, speeches taken) from speech ``i`` on."""
        if i == len(delivered):
            return (len(texts) - at, 0, 0, ())
        speech = delivered[i]
        words = speech.words
        options = []
        if words and texts[at : at + len(words)] == words:
            stray, turns, found, taken = best(i + 1, at + len(words))
            options.append((stray, turns - (not speech.backchannel), found - 1, (i, *taken)))
        if speech.backchannel or not words:
            options.append(best(i + 1, at))
        if not options:
            return (len(texts) - at, 0, 0, ())  # a turn that is not there ends the reading
        return min(options)

    _stray, _turns, _found, taken = best(0, 0)
    found = UserTurns()
    at = 0
    for i in taken:
        speech = delivered[i]
        at += len(speech.words)
        found.present.append(speech)
        found.ended_at[speech.index] = stream[at - 1][1]
    found.stray = texts[at:]
    return found


@dataclass
class Alignment:
    """Which scripted speech each assistant message of a context is.

    A message can only be something whose text it is a prefix of, and messages keep the
    order the speech came to be in. Where that leaves exactly one possibility for an item
    it is ``matched`` (to an item index) or ``absent``. Where it leaves more than one, as
    with two replies of identical text of which only one was kept, it is ``ambiguous``:
    Earful does not guess.
    """

    possible: bool  # false: some assistant message is no scripted speech's prefix, in any order
    matched: dict[str, int] = field(default_factory=dict)  # spoken key -> item index
    absent: set[str] = field(default_factory=set)  # keys certainly not in the context
    ambiguous: set[str] = field(default_factory=set)
    unmatched_items: list[int] = field(default_factory=list)  # when not possible


def align(items: list[Item], spoken: list[Spoken]) -> Alignment:
    messages = [
        (index, _words(item))
        for index, item in enumerate(items)
        if item["type"] == "message" and item.get("role") == "assistant" and _words(item)
    ]
    a, b = len(messages), len(spoken)
    fits = [
        [spoken[j].words[: len(words)] == words for j in range(b)] for _index, words in messages
    ]
    # after[i][j]: ways to place messages i.. on speech j.. ; before[i][j]: messages <i on speech <j
    after = [[0] * (b + 1) for _ in range(a + 1)]
    for j in range(b + 1):
        after[a][j] = 1
    for i in range(a - 1, -1, -1):
        for j in range(b - 1, -1, -1):
            after[i][j] = after[i][j + 1] + (after[i + 1][j + 1] if fits[i][j] else 0)
    total = after[0][0]
    if total == 0:
        # Report the messages that fit nothing at all; the rest are out of order.
        loose = [index for (index, _w), row in zip(messages, fits) if not any(row)]
        return Alignment(False, unmatched_items=loose or [index for index, _w in messages])

    before = [[0] * (b + 1) for _ in range(a + 1)]
    for j in range(b + 1):
        before[0][j] = 1
    for i in range(1, a + 1):
        for j in range(1, b + 1):
            before[i][j] = before[i][j - 1] + (before[i - 1][j - 1] if fits[i - 1][j - 1] else 0)

    out = Alignment(True)
    for j, item in enumerate(spoken):
        ways = [before[i][j] * after[i + 1][j + 1] if fits[i][j] else 0 for i in range(a)]
        if sum(ways) == 0:
            out.absent.add(item.key)
        elif total in ways:
            out.matched[item.key] = messages[ways.index(total)][0]
        else:
            out.ambiguous.add(item.key)
    return out


Cost = tuple[int, int]


def favourable(
    items: list[Item], spoken: list[Spoken], cost: Callable[[Spoken, Item | None], Cost]
) -> dict[str, int | None] | None:
    """Of every way to say which scripted speech each assistant message is, the one that
    costs least: for each thing said (by key), the index of its message, or None if it is
    not in the context. None if the messages do not line up with what was said at all.

    ``cost(speech, message)`` is what it costs for ``message`` (or no message) to be the
    memory of ``speech``; costs add. As in ``align``, a message can only be something
    whose text it is a prefix of, and messages keep the order the speech came to be in.
    Among equal readings the one that gives each message to the earliest speech is taken.

    A remembered message carries its words and its place and nothing else, so when two
    things said have the same words there is no fact about which one a message is. Every
    such reading describes the same memory, and the checker reports the kindest.
    """
    messages = [
        (index, _words(item))
        for index, item in enumerate(items)
        if item["type"] == "message" and item.get("role") == "assistant" and _words(item)
    ]
    a, b = len(messages), len(spoken)
    absent = [cost(speech, None) for speech in spoken]
    # best[i][j]: the least cost of placing messages i.. on speech j.., and whether message
    # i is given to speech j there. None: it cannot be done.
    best: list[list[tuple[Cost, bool] | None]] = [[None] * (b + 1) for _ in range(a + 1)]
    best[a][b] = ((0, 0), False)
    for j in range(b - 1, -1, -1):
        rest = best[a][j + 1][0]  # type: ignore[index]
        best[a][j] = ((rest[0] + absent[j][0], rest[1] + absent[j][1]), False)
    for i in range(a - 1, -1, -1):
        index, words = messages[i]
        for j in range(b - 1, -1, -1):
            options: list[tuple[Cost, bool]] = []
            after = best[i + 1][j + 1]
            if after is not None and spoken[j].words[: len(words)] == words:
                here = cost(spoken[j], items[index])
                options.append(((here[0] + after[0][0], here[1] + after[0][1]), True))
            skipped = best[i][j + 1]
            if skipped is not None:
                options.append(((absent[j][0] + skipped[0][0], absent[j][1] + skipped[0][1]), False))
            if options:
                best[i][j] = min(options, key=lambda option: (option[0], not option[1]))
    if best[0][0] is None:
        return None
    out: dict[str, int | None] = {speech.key: None for speech in spoken}
    i = 0
    for j in range(b):
        step = best[i][j]
        assert step is not None
        if step[1]:
            out[spoken[j].key] = messages[i][0]
            i += 1
    return out
