"""A ledger, read once into the records the contracts reason about."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

Event = dict[str, Any]
Reading = dict[int, int]  # utterance -> model call

MAX_READINGS = 512


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


def readings(events: list[Event]) -> list[Reading]:
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
    while at >= 0 and len(found) <= MAX_READINGS:
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
    return found if len(found) <= MAX_READINGS else [{}]


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


def unseeded(items: list[dict[str, Any]], seeded: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``items`` without the history the agent started with: each item of ``seeded`` in turn
    takes out the first item after the last one taken that is the same. One that is no longer
    there takes nothing out."""

    def key(item: dict[str, Any]) -> tuple[Any, ...]:
        return (item["type"], *(item.get(k) for k in _ITEM_KEYS.get(item["type"], ())))

    out, at = list(items), 0
    for item in seeded:
        found = next((i for i in range(at, len(out)) if key(out[i]) == key(item)), None)
        if found is not None:
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


class View:
    def __init__(self, events: list[Event], reading: Reading | None = None) -> None:
        self.events = events
        self.reading = reading or {}
        # Replies that some ambiguous text could be: what is said of them rests on a reading.
        self.read_calls: set[int] = set()
        could_be = candidates(events)
        # Ambiguous texts and no reading of them: there were more readings than the cap.
        self.capped: set[int] = set(could_be) if could_be and not self.reading else set()
        self.header = events[0]["data"]
        self.scenario = self.header["scenario"]
        self.session = self.scenario["session"]
        self.providers = self.scenario["providers"]
        self.end = events[-1]
        self.origin: Event | None = None
        self.hangup: Event | None = None
        self.calls: dict[int, ModelCall] = {}
        self.utterances: dict[int, Utterance] = {}
        self.segments: dict[int, Segment] = {}
        self.tools: dict[int, ToolInvocation] = {}
        self.effects: dict[int, Effect] = {}
        self.commands: list[Event] = []
        self.history: Event | None = None
        self.seeded: list[dict[str, Any]] = self.scenario.get("history", [])
        self.intents: dict[str, dict[str, Any]] = {
            it["intent"]: it for it in self.scenario["intents"]
        }
        self.request_of: dict[str, ModelCall] = {}  # tool call id -> the model call that asked
        self.asked: dict[str, dict[str, Any]] = {}  # tool call id -> the call as the model made it

        speech_events: dict[int, dict[str, Event]] = {}
        for e in events:
            kind, d = e["kind"], e["data"]
            if kind == "user.origin":
                self.origin = e
            elif kind == "user.hangup":
                self.hangup = e
            elif kind in ("user.vad_start", "user.vad_end", "user.transcript_final"):
                speech_events.setdefault(d["speech"], {})[kind] = e
            elif kind == "llm.request":
                self.calls[d["call"]] = ModelCall(d["call"], e, seeded=self.seeded)
            elif kind == "llm.response":
                self.calls[d["call"]].response = e
                for tool_call in d["tool_calls"]:
                    self.request_of[tool_call["call_id"]] = self.calls[d["call"]]
                    self.asked[tool_call["call_id"]] = tool_call
            elif kind == "tts.synthesized":
                call = d["call"]
                if d["attribution"] == "ambiguous":
                    call = self.reading.get(d["utterance"])
                    self.read_calls |= set(could_be[d["utterance"]]) if call is not None else set()
                self.utterances[d["utterance"]] = Utterance(
                    d["utterance"], e, call, d["attribution"], d["text"].split(), d["stamps"]
                )
            elif kind == "playout.started":
                self._segment(d["segment"]).started = e
            elif kind == "playout.paused":
                self._segment(d["segment"]).pauses.append((e, None))
            elif kind == "playout.resumed":
                pauses = self._segment(d["segment"]).pauses
                pauses[-1] = (pauses[-1][0], e)
            elif kind == "playout.finished":
                self._segment(d["segment"]).finished = e
            elif kind.startswith("sink."):
                self.commands.append(e)
            elif kind == "tool.called":
                self.tools[d["tool"]] = ToolInvocation(d["tool"], d["name"], d["call_id"], e)
            elif kind in ("tool.returned", "tool.raised", "tool.cancelled"):
                self.tools[d["tool"]].outcome = e
            elif kind == "effect.requested":
                self.effects[d["request"]] = Effect(d["request"], d["intent"], e)
            elif kind in ("effect.committed", "effect.deduplicated", "effect.failed"):
                self.effects[d["request"]].outcome = e
            elif kind == "effect.timed_out":
                self.effects[d["request"]].timed_out = e
            elif kind == "history.final":
                self.history = e

        t0 = self.origin["t_us"] if self.origin else None
        self.speeches: list[Speech] = []
        for s in self.scenario["speeches"]:
            seen = speech_events.get(s["speech"], {})
            self.speeches.append(
                Speech(
                    index=s["speech"],
                    # Without an origin the true times are unknown; such a speech has no events.
                    start=(t0 or 0) + s["start_us"],
                    end=(t0 or 0) + s["end_us"],
                    transcript=s["transcript"],
                    expects_reply=s["expects_reply"],
                    backchannel=s["backchannel"],
                    retires=tuple(s["retires"]),
                    vad_start=seen.get("user.vad_start"),
                    vad_end=seen.get("user.vad_end"),
                    final=seen.get("user.transcript_final"),
                )
            )

        # Speeches of one VAD segment share its start and end reports.
        min_silence = self.providers["vad"]["min_silence_us"]
        heard: list[list[Speech]] = []
        for speech in sorted(self.speeches, key=lambda sp: sp.start):
            if heard and speech.start - heard[-1][-1].end < min_silence:
                heard[-1].append(speech)
            else:
                heard.append([speech])
        for group in heard:
            for speech in group:
                speech.vad_start, speech.vad_end = group[0].vad_start, group[-1].vad_end
                speech.segment_start, speech.segment_end = group[0].start, group[-1].end

    def _segment(self, number: int) -> Segment:
        return self.segments.setdefault(number, Segment(number))

    @cached_property
    def replies(self) -> list[ModelCall]:
        return [c for c in self.calls.values() if c.words]

    @cached_property
    def spoken(self) -> list[Spoken]:
        out = [
            Spoken(f"reply {c.call}", {"reply": c.call}, c.words, c.answer, c.call)
            for c in self.replies
        ]
        out += [
            Spoken(f"utterance {u.utterance}", {"utterance": u.utterance}, u.words, u.event, None)
            for u in self.utterances.values()
            if u.attribution == "none" and u.words
        ]
        return sorted(out, key=lambda item: item.born["seq"])

    def end_us(self, item: Spoken, utterance: Utterance | None, words: int) -> int:
        """Microseconds of audio in the first ``words`` words; unsynthesized words are estimated."""
        if utterance is not None:
            d = utterance.event["data"]
            samples, rate = d["word_samples"], d["sample_rate"]
            known = -(-sum(samples[:words]) * 1_000_000 // rate)
        else:
            samples, known = [], 0
        per_letter = self.providers["tts"]["seconds_per_letter_us"]
        rest = item.words[len(samples) : words]
        return known + sum((sum(ch.isalnum() for ch in word) or 1) * per_letter for word in rest)

    def audio_us(self, call: ModelCall) -> int:
        """Microseconds of audio in a reply: as synthesized, else estimated."""
        utterance = next((u for u in self.utterances.values() if u.call == call.call), None)
        if utterance is not None:
            return utterance.audio_us
        per_letter = self.providers["tts"]["seconds_per_letter_us"]
        return sum((sum(ch.isalnum() for ch in word) or 1) * per_letter for word in call.words)

    @cached_property
    def witnessed_tools(self) -> set[str]:
        """Tools whose every call the ledger records with the call id the model gave."""
        seen = {t.name for t in self.tools.values() if t.call_id is not None}
        unlinked = {t.name for t in self.tools.values() if t.call_id is None}
        return (set(self.scenario["witnessed_tools"]) | seen) - unlinked

    def unaccounted_calls(self, intent: str) -> list[Event]:
        """Calls of the intent's tools that made no write, if the backend saw none for it."""
        if any(e.intent == intent for e in self.effects.values()):
            return []  # the backend is where this intent is written: its record is complete
        wrote = {e.requested["data"]["tool"] for e in self.effects.values()}
        tools = self.intents[intent]["tools"]
        return [t.called for t in self.tools.values() if t.name in tools and t.tool not in wrote]

    @cached_property
    def invocation_of(self) -> dict[str, ToolInvocation]:
        """Call id -> the invocation the tool witness recorded for it."""
        return {t.call_id: t for t in self.tools.values() if t.call_id is not None}

    def refusal(self, call_id: str) -> Event | None:
        """The first model context showing an error result for a call no witnessed tool received."""
        if call_id in self.invocation_of:
            return None
        for call in self.calls.values():
            if any(
                i["type"] == "function_call_output" and i["call_id"] == call_id and i["is_error"]
                for i in call.items
            ):
                return call.request
        return None

    @cached_property
    def end_of_turn_us(self) -> int:
        """The longest the framework can take, once the user stops, to decide the turn is over."""
        return self.providers["vad"]["min_silence_us"] + self.session["min_endpointing_delay_us"]

    def turn_over_us(self, speech: Speech) -> int:
        """The latest instant the framework can decide the turn holding ``speech`` is over."""

        def stopped(s: Speech) -> int:
            return max(s.end, s.final["t_us"]) if s.final else s.end

        over = stopped(speech) + self.end_of_turn_us
        for later in sorted(self.speeches, key=lambda s: s.start):
            if later.start <= speech.start:
                continue
            if later.start >= over:
                break
            over = max(over, stopped(later) + self.end_of_turn_us)
        return over

    def stopped_as_it_ended(self, playout: Playout, words: int) -> bool:
        """Whether all its ``words`` played and a pause or clear reached the sink as they ended."""
        done = playout.settled
        if done is None or playout.played < words:
            return False
        ended = {playout.completed_us.get(words - 1)}
        if not done["data"]["interrupted"]:
            ended.add(done["t_us"])  # the framework learns of the end when the sink reports it
        return any(
            e["kind"] in ("sink.pause", "sink.clear") and e["t_us"] in ended for e in self.commands
        )

    @cached_property
    def delivered(self) -> list[Speech]:
        return sorted((s for s in self.speeches if s.final), key=lambda s: s.delivery["seq"])

    @cached_property
    def contexts(self) -> list[tuple[str, Event, list[dict[str, Any]]]]:
        """Each model context, then the final chat history, as ``(label, event, items)``, each
        without the history the agent started with."""
        out = [(f"call {c.call}", c.request, c.items) for c in self.calls.values()]
        if self.history is not None:
            items = unseeded(self.history["data"]["items"], self.seeded)
            out.append(("final history", self.history, items))
        return out

    @cached_property
    def stamp_owner(self) -> dict[int, tuple[int, int]]:
        """stamp -> (utterance, word index)."""
        return {
            stamp: (u.utterance, index)
            for u in self.utterances.values()
            for index, stamp in enumerate(u.stamps)
        }

    def asked_to_pause(self, paused: Event, resumed: Event | None) -> bool:
        """Whether the output had been asked to pause at some time during this silence."""
        asked, until = False, resumed["seq"] if resumed else self.end["seq"]
        for e in self.commands:
            if e["seq"] > until:
                break
            if e["kind"] == "sink.pause":
                asked = True
            elif e["kind"] == "sink.resume" and e["seq"] < paused["seq"]:
                asked = False
        return asked

    def runs(self) -> list[tuple[list[tuple[int, int]], Segment]]:
        """Each stretch a segment was under way, as its audible spans. A gap left by an empty
        buffer does not end a stretch; a pause the output was asked for does."""
        until = self.end["t_us"]
        out = []
        for segment in self.segments.values():
            spans = segment.audible(until)
            if not spans:
                continue
            run = [spans[0]]
            for (paused, resumed), span in zip(segment.gaps(), spans[1:]):
                if self.asked_to_pause(paused, resumed):
                    out.append((run, segment))
                    run = []
                run.append(span)
            out.append((run, segment))
        return sorted(out, key=lambda run: run[0][0][0])

    def resumes(self) -> list[Event]:
        """Each time a segment became audible again after a pause the output was asked for."""
        return [
            resumed
            for segment in self.segments.values()
            for paused, resumed in segment.gaps()
            if resumed is not None and self.asked_to_pause(paused, resumed)
        ]

    @cached_property
    def playouts(self) -> dict[str, Playout]:
        by_call = {u.call: u for u in self.utterances.values() if u.call is not None}
        loose = [u for u in self.utterances.values() if u.call is None]
        unsynthesized = [r for r in self.replies if r.call not in by_call]
        out: dict[str, Playout] = {}

        def code(texts: list[Utterance]) -> str:
            capped = any(u.utterance in self.capped for u in texts)
            return "readings-over-cap" if capped else "audio-unattributed"

        for item in self.spoken:
            unattributable = None
            if item.call is not None:
                utterance = by_call.get(item.call)
                later = [
                    u
                    for u in loose
                    if u.event["data"]["started_us"] >= item.born["t_us"]
                    and could_be(u.words, item.words)
                ]
                if utterance is None and later:
                    # Its text may have been altered on the way to the voice; Earful does not guess.
                    unattributable = (
                        code(later),
                        "a synthesized text that was not attributed could be this reply",
                    )
            else:
                utterance = self.utterances[item.instance["utterance"]]
                if any(
                    r.answer["t_us"] <= utterance.event["data"]["started_us"]
                    and could_be(utterance.words, r.words)
                    for r in unsynthesized
                ):
                    unattributable = (
                        code([utterance]),
                        "this text could be a model reply altered before it was synthesized",
                    )
            playout = Playout(item.key, utterance, unattributable)
            if utterance is not None:
                self._account(playout, utterance)
                # With none of it in a finished segment, any segment still open may hold it.
                playout.pending |= not playout.segments and bool(self.open_segments())
            out[item.key] = playout
        return out

    def playout(self, call: int) -> Playout:
        return self.playouts[f"reply {call}"]

    def _account(self, playout: Playout, utterance: Utterance) -> None:
        index_of = {stamp: i for i, stamp in enumerate(utterance.stamps)}
        first_stamp = utterance.stamps[0] if utterance.stamps else None
        # What the user heard ends when they leave.
        left = self.hangup["t_us"] if self.hangup else None

        def heard(at: int | None) -> int | None:
            return None if at is None or (left is not None and at >= left) else at

        began: list[int] = []  # each instant a segment began to play audio of it
        for segment in self.segments.values():
            # A started segment names its first utterance even if the run ends before it finishes.
            if (
                segment.started is not None
                and segment.started["data"]["utterance"] == utterance.utterance
            ):
                began.append(segment.started["t_us"])
                playout.pending |= segment.finished is None
            done = segment.finished
            # What a segment played is known only once it has finished.
            if done is None or utterance.utterance not in done["data"]["utterances"]:
                continue
            playout.segments.append(segment)
            d = done["data"]
            playout.interrupted = playout.interrupted or d["interrupted"]
            for stamp, at in zip(d["words_played"], d["words_completed_us"]):
                if stamp not in index_of:
                    continue
                if left is not None and at > left:
                    playout.outlasted_hangup = True
                    continue
                playout.played += 1
                playout.completed_us[index_of[stamp]] = at
            if d["word_cut"] in index_of:
                playout.cut = 1
            if playout.outlasted_hangup:
                playout.cut = 1  # the word under way when the user left is the one they heard cut
            if segment.started is not None:
                if d["utterances"][0] == utterance.utterance:
                    # First in its segment: it began when the segment did.
                    began.append(segment.started["t_us"])
                elif first_stamp in d["words_played"]:
                    # Not first in its segment: it began when the word before it completed.
                    position = d["words_played"].index(first_stamp)
                    if position > 0:
                        began.append(d["words_completed_us"][position - 1])
                elif d["word_cut"] == first_stamp and d["words_completed_us"]:
                    began.append(d["words_completed_us"][-1])
            if playout.settled is None or done["seq"] > playout.settled["seq"]:
                playout.settled = done
        # One text can play in several segments; it was first audible at the earliest.
        playout.first_audible_us = heard(min(began, default=None))

    def open_segments(self) -> list[int]:
        seen = {e["data"]["segment"] for e in self.commands if e["data"]["segment"] is not None}
        seen |= set(self.segments)
        return sorted(
            n for n in seen if n not in self.segments or self.segments[n].finished is None
        )

    def word_start_us(self, playout: Playout, index: int) -> int | None:
        """When word ``index`` of a reply started playing, if it completed."""
        if index not in playout.completed_us:
            return None
        if index == 0:
            start = playout.first_audible_us
        else:
            start = playout.completed_us.get(index - 1)
        if start is None:
            return None
        # A pause that began exactly as the previous word completed delays this word's start.
        for segment in playout.segments:
            for paused, resumed in segment.pauses:
                if paused["t_us"] == start and resumed is not None and index > 0:
                    start = resumed["t_us"]
        return start
