"""Resolve the possible replies for ambiguous synthesized text."""

from __future__ import annotations

from .records import Event

Reading = dict[int, int]  # utterance -> model call


def candidates(events: list[Event]) -> dict[int, list[int]]:
    """For each ambiguously attributed text, by utterance, the replies it could be."""
    taken = {
        e["data"]["call"]
        for e in events
        if e["kind"] == "tts.synthesized" and e["data"]["call"] is not None
    }
    first_token = {
        e["data"]["call"]: e["t_us"] + e["data"]["ttft_us"]
        for e in events
        if e["kind"] == "llm.response"
    }
    return {
        e["data"]["utterance"]: [
            c
            for c in e["data"]["candidates"]
            if c not in taken and first_token[c] <= e["data"]["started_us"]
        ]
        for e in events
        if e["kind"] == "tts.synthesized" and e["data"]["attribution"] == "ambiguous"
    }


def possible_readings(events: list[Event], limit: int) -> list[Reading]:
    """Each way to give every ambiguous text a reply of its own; ``[{}]`` if none or too many."""
    texts = list(candidates(events).items())
    reply: dict[int, int] = {}  # position of a text -> the reply it holds
    holder: dict[int, int] = {}  # reply -> position of the text that holds it

    def house(text: int, fixed: int) -> bool:
        """Give ``text``, which holds nothing, a reply; texts before ``fixed`` keep theirs."""
        wanted: dict[int, int] = {}  # reply -> the text that would take it
        asking = [text]
        for other in asking:
            for call in texts[other][1]:
                if call in wanted or (call in holder and holder[call] < fixed):
                    continue
                wanted[call] = other
                if call in holder:
                    asking.append(holder[call])  # its holder must find another
                    continue
                free: int | None = call
                while free is not None:  # hand each reply on the path to the text that wanted it
                    taker = wanted[free]
                    gave_up = reply.get(taker)
                    reply[taker], holder[free] = free, taker
                    free = gave_up
                return True
        return False

    def take(text: int, call: int) -> bool:
        """Give ``text`` this reply if texts before it keep theirs and later ones can be housed."""
        other = holder.get(call)
        if other == text:
            return True
        if other is not None and other < text:
            return False
        mine = reply[text]
        del holder[mine]
        reply[text], holder[call] = call, text
        if other is None:
            return True
        del reply[other]
        if house(other, text + 1):
            return True
        reply[other], holder[call] = call, other
        reply[text], holder[mine] = mine, text
        return False

    if not all(house(text, 0) for text in range(len(texts))):
        return [{}]
    # A candidate is taken only if the rest can then be housed, so no branch is without a reading.
    found: list[Reading] = []
    tried = [0] * len(texts)  # how many candidates of the text at each depth were tried
    at = 0
    while at >= 0 and len(found) <= limit:
        if at == len(texts):
            found.append({utterance: reply[n] for n, (utterance, _) in enumerate(texts)})
            at -= 1
        elif tried[at] == len(texts[at][1]):
            tried[at] = 0
            at -= 1
        else:
            tried[at] += 1
            if take(at, texts[at][1][tried[at] - 1]):
                at += 1
    return found if len(found) <= limit else [{}]
