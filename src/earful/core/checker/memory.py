"""Reading a model context: which user turns it holds, and which scripted speech each
assistant message is."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .view import Event, Speech, Spoken, View

Item = dict[str, Any]
Score = tuple[int, int, int]  # (stray words, -turns found, -transcripts found); least is best


def _words(item: Item) -> list[str]:
    return (item.get("text") or "").split()


@dataclass
class UserTurns:
    present: list[Speech] = field(default_factory=list)  # delivered turns found, in order
    began_at: dict[int, int] = field(default_factory=dict)  # speech index -> item of its first word
    ended_at: dict[int, int] = field(default_factory=dict)  # speech index -> item of its last word
    # Turns no other reading could place elsewhere: none before them was read as absent.
    placed: set[int] = field(default_factory=set)
    ambiguous: set[int] = field(default_factory=set)  # identical turns across a handoff
    stray: list[str] = field(default_factory=list)  # user words that match no delivered turn


def user_turns(items: list[Item], delivered: list[Speech]) -> UserTurns:
    """The delivered transcripts a context's user text holds, read as one word stream, in order."""
    stream: list[tuple[str, int]] = [
        (word, index)
        for index, item in enumerate(items)
        if item["type"] == "message" and item.get("role") == "user"
        for word in _words(item)
    ]
    texts = [w for w, _ in stream]

    def fits(speech: Speech, at: int) -> bool:
        return bool(speech.words) and texts[at : at + len(speech.words)] == speech.words

    def may_be_absent(speech: Speech) -> bool:
        return speech.backchannel or not speech.words

    def ends(at: int) -> Score:
        return (len(texts) - at, 0, 0)

    # The places in the stream each speech could start at, given the speeches before it.
    starts: list[set[int]] = [{0}]
    for speech in delivered:
        starts.append(
            {at + len(speech.words) for at in starts[-1] if fits(speech, at)}
            | {at for at in starts[-1] if may_be_absent(speech)}
        )
    # From the last speech back: the best that can follow each place, and whether by skipping.
    best: dict[int, Score] = {at: ends(at) for at in starts[-1]}
    skips: list[dict[int, bool]] = []
    for speech, places in zip(reversed(delivered), reversed(starts[:-1])):
        here: dict[int, Score] = {}
        skip: dict[int, bool] = {}
        for at in places:
            options: list[tuple[Score, bool]] = []
            if fits(speech, at):
                stray, turns, kept = best[at + len(speech.words)]
                options.append(((stray, turns - (not speech.backchannel), kept - 1), False))
            if may_be_absent(speech):
                options.append((best[at], True))
            if options:
                here[at], skip[at] = min(options)  # of two equal readings, the one that takes it
            else:
                here[at] = ends(at)  # a turn that is not there ends the reading
        best = here
        skips.append(skip)

    found = UserTurns()
    at, certain = 0, True
    for speech, skip in zip(delivered, reversed(skips)):
        if at not in skip:
            break
        if skip[at]:
            certain = certain and not speech.words  # its words may be those read as a later turn
            continue
        found.began_at[speech.index] = stream[at][1]
        at += len(speech.words)
        found.present.append(speech)
        found.ended_at[speech.index] = stream[at - 1][1]
        if certain:
            found.placed.add(speech.index)
    found.stray = texts[at:]
    return found


def _retained_user_message(
    view: View, boundary: Event, seen: UserTurns, items: list[Item], earlier: list[Speech]
) -> bool | None:
    """Whether a message matched as a pre-handoff turn carries its earlier witnessed ID."""
    for speech in earlier:
        current = items[seen.began_at[speech.index]].get("id")
        if not current:
            continue
        for call in reversed(list(view.calls.values())):
            seq = call.request["seq"]
            if not speech.delivery["seq"] < seq < boundary["seq"]:
                continue
            preceding = [s for s in view.delivered if s.delivery["seq"] < seq]
            turns = user_turns(call.items, preceding)
            if speech.index not in turns.placed:
                continue
            old = call.items[turns.began_at[speech.index]].get("id")
            if old:
                return old == current
    return None


def context_turns(
    view: View, event: Event, items: list[Item]
) -> tuple[UserTurns, list[Speech], bool]:
    """A new agent can start its own context. Only change the attribution when a
    post-handoff transcript actually explains user words the full history cannot."""
    delivered = [s for s in view.delivered if s.delivery["seq"] < event["seq"]]
    original = user_turns(items, delivered)
    if not view.handoffs or (not original.stray and len(original.present) == len(delivered)):
        return original, delivered, False
    best, kept = original, delivered
    for boundary in view.handoffs:
        if boundary["seq"] >= event["seq"]:
            break
        later = [s for s in delivered if s.delivery["seq"] > boundary["seq"]]
        suffix = user_turns(items, later)
        if not suffix.present or len(suffix.stray) > len(best.stray):
            continue
        if len(suffix.stray) < len(best.stray):
            best, kept = suffix, later
            continue
        # Equal words can belong to two speakers' turns even when the new agent
        # answered. A repeated transcript alone cannot identify its message.
        old_turns = [s for s in best.present if s.delivery["seq"] < boundary["seq"]]
        alternatives = {s.index for s in suffix.present} ^ {s.index for s in best.present}
        if not old_turns or not alternatives:
            continue
        retained = _retained_user_message(view, boundary, best, items, old_turns)
        if retained is False:
            best, kept = suffix, later
        elif retained is None:
            best.ambiguous.update(alternatives)
            best.placed.difference_update(alternatives)
    return best, kept, best is not original


@dataclass
class Alignment:
    """Which scripted speech each assistant message is: a prefix of its speech, in speech order."""

    possible: bool  # false: some assistant message is no scripted speech's prefix, in any order
    matched: dict[str, int] = field(default_factory=dict)  # spoken key -> item index
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
        if total in ways:
            out.matched[item.key] = messages[ways.index(total)][0]
        elif any(ways):
            out.ambiguous.add(item.key)
    return out


Cost = tuple[int, int]


def favourable(
    items: list[Item], spoken: list[Spoken], cost: Callable[[Spoken, Item | None], Cost]
) -> dict[str, int | None] | None:
    """The cheapest reading, spoken key -> item index or None; None if nothing lines up."""
    messages = [
        (index, _words(item))
        for index, item in enumerate(items)
        if item["type"] == "message" and item.get("role") == "assistant" and _words(item)
    ]
    a, b = len(messages), len(spoken)
    absent = [cost(speech, None) for speech in spoken]
    # best[i][j]: (least cost of placing messages i.. on speech j.., whether i goes to j) or None.
    best: list[list[tuple[Cost, bool] | None]] = [[None] * (b + 1) for _ in range(a + 1)]
    rest: Cost = (0, 0)
    best[a][b] = (rest, False)
    for j in range(b - 1, -1, -1):
        rest = (rest[0] + absent[j][0], rest[1] + absent[j][1])
        best[a][j] = (rest, False)
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
                options.append(
                    ((absent[j][0] + skipped[0][0], absent[j][1] + skipped[0][1]), False)
                )
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
