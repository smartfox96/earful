"""A ledger, read once into the records the contracts reason about.

Nothing here judges anything. It indexes a valid ledger: scripted speeches with their true
times, model calls, synthesized utterances, playout segments, tool invocations and backend
writes.

Framework-independent: this module must never import livekit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from itertools import islice
from typing import Any

Event = dict[str, Any]
Reading = dict[int, int]  # utterance -> the model call it is read as being the reply of

# A ledger with more ways than this to read its ambiguous audio is not read every way.
MAX_READINGS = 512


def readings(events: list[Event]) -> list[Reading]:
    """Every way to say which reply each ambiguously attributed text was.

    The scripted voice is given a text, not the model call it came from. When more than
    one reply not yet spoken has that text, the ledger names them all (``candidates``).
    A reading gives each such text one of its candidates, no reply twice. Which reading
    is true cannot be observed, and makes no difference to what was heard.

    Always holds at least one reading. With nothing ambiguous that is the empty one. If
    there are more than ``MAX_READINGS``, or none fits, it is the empty one too, and the
    ambiguous texts stay unattributed.
    """
    taken = {e["data"]["call"] for e in events if e["kind"] == "tts.synthesized" and e["data"]["call"] is not None}
    open_ = [
        (e["data"]["utterance"], [c for c in e["data"]["candidates"] if c not in taken])
        for e in events
        if e["kind"] == "tts.synthesized" and e["data"]["attribution"] == "ambiguous"
    ]

    def assign(at: int, used: frozenset[int]):
        if at == len(open_):
            yield {}
            return
        utterance, candidates = open_[at]
        for call in candidates:
            if call not in used:
                for rest in assign(at + 1, used | {call}):
                    yield {utterance: call, **rest}

    found = list(islice(assign(0, frozenset()), MAX_READINGS + 1))
    return found if 0 < len(found) <= MAX_READINGS else [{}]


@dataclass
class Speech:
    index: int
    start: int  # true start, absolute microseconds
    end: int  # true end
    transcript: str
    expects_reply: bool
    backchannel: bool
    retires: tuple[str, ...]
    vad_start: Event | None = None  # the scripted VAD told the framework
    vad_end: Event | None = None
    final: Event | None = None  # the final transcript was delivered

    @property
    def noise(self) -> bool:
        return not self.transcript

    @property
    def words(self) -> list[str]:
        return self.transcript.split()


@dataclass
class ModelCall:
    call: int
    request: Event
    response: Event | None = None

    @property
    def items(self) -> list[dict[str, Any]]:
        return self.request["data"]["items"]

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
    """Something the agent was scripted to say: a reply the model gave, or text the
    application spoke itself (a greeting, a fixed phrase). Both are played by the sink and
    both are remembered, so the memory contracts judge both."""

    key: str  # "reply 3" or "utterance 5"
    instance: dict[str, int]  # how verdicts name it
    words: list[str]
    born: Event  # llm.response, or tts.synthesized for text the application spoke
    call: int | None  # the model call, for a reply


@dataclass
class Segment:
    number: int
    started: Event | None = None
    pauses: list[tuple[Event, Event | None]] = field(default_factory=list)  # (paused, resumed)
    finished: Event | None = None

    def audible(self, until: int) -> list[tuple[int, int]]:
        """Intervals during which the segment was audible. One that was still audible when
        the run ended is closed at ``until``."""
        if self.started is None:
            return []
        out, opened = [], self.started["t_us"]
        for paused, resumed in self.pauses:
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
    outcome: Event | None = None  # tool.returned, tool.raised or tool.cancelled


@dataclass
class Effect:
    request: int
    intent: str
    requested: Event
    outcome: Event | None = None  # effect.committed or effect.deduplicated

    @property
    def committed(self) -> Event | None:
        return self.outcome if self.outcome and self.outcome["kind"] == "effect.committed" else None


@dataclass
class Playout:
    """What the sink did with one thing the agent was scripted to say."""

    key: str
    utterance: Utterance | None  # None: no synthesized text was attributed to it
    unattributable: str | None  # why its audio cannot be told apart, if it cannot
    interrupted: bool = False  # a segment holding it was cleared before its audio ended
    segments: list[Segment] = field(default_factory=list)  # segments holding its audio
    settled: Event | None = None  # the event after which nothing more of it can play
    pending: bool = False  # the run ended while its audio may still have been in the sink
    played: int = 0  # words whose last sample was consumed
    cut: int = 0  # 1 if playout stopped inside one of its words
    completed_us: dict[int, int] = field(default_factory=dict)  # word index -> completion time
    first_audible_us: int | None = None  # when it began to play; None if it never did


class View:
    def __init__(self, events: list[Event], reading: Reading | None = None) -> None:
        self.events = events
        self.reading = reading or {}
        # Replies that some ambiguous text could be: what is said of them rests on a reading.
        self.read_calls: set[int] = set()
        self.header = events[0]["data"]
        self.scenario = self.header["scenario"]
        self.session = self.scenario["session"]
        self.providers = self.scenario["providers"]
        self.end = events[-1]
        self.origin: Event | None = None
        self.calls: dict[int, ModelCall] = {}
        self.utterances: dict[int, Utterance] = {}
        self.segments: dict[int, Segment] = {}
        self.tools: dict[int, ToolInvocation] = {}
        self.effects: dict[int, Effect] = {}
        self.commands: list[Event] = []  # sink.pause, sink.resume, sink.clear, sink.flush
        self.history: Event | None = None
        self.intents: dict[str, dict[str, Any]] = {it["intent"]: it for it in self.scenario["intents"]}
        self.request_of: dict[str, ModelCall] = {}  # tool call id -> the model call that asked

        speech_events: dict[int, dict[str, Event]] = {}
        for e in events:
            kind, d = e["kind"], e["data"]
            if kind == "user.origin":
                self.origin = e
            elif kind in ("user.vad_start", "user.vad_end", "user.transcript_final"):
                speech_events.setdefault(d["speech"], {})[kind] = e
            elif kind == "llm.request":
                self.calls[d["call"]] = ModelCall(d["call"], e)
            elif kind == "llm.response":
                self.calls[d["call"]].response = e
                for tool_call in d["tool_calls"]:
                    self.request_of[tool_call["call_id"]] = self.calls[d["call"]]
            elif kind == "tts.synthesized":
                call = d["call"]
                if d["attribution"] == "ambiguous":
                    call = self.reading.get(d["utterance"])
                    self.read_calls |= set(d["candidates"]) if call is not None else set()
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
            elif kind in ("effect.committed", "effect.deduplicated"):
                self.effects[d["request"]].outcome = e
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

    def _segment(self, number: int) -> Segment:
        return self.segments.setdefault(number, Segment(number))

    # ------------------------------------------------------------------ derived records

    @cached_property
    def replies(self) -> list[ModelCall]:
        """Model calls that answered with text, in the order the model gave them."""
        return [c for c in self.calls.values() if c.words]

    @cached_property
    def spoken(self) -> list[Spoken]:
        """Everything the agent was scripted to say, in the order it came to be: the
        model's replies, and synthesized texts that no reply accounts for."""
        out = [
            Spoken(f"reply {c.call}", {"reply": c.call}, c.words, c.response, c.call)
            for c in self.replies
        ]
        out += [
            Spoken(f"utterance {u.utterance}", {"utterance": u.utterance}, u.words, u.event, None)
            for u in self.utterances.values()
            if u.attribution == "none" and u.words
        ]
        return sorted(out, key=lambda item: item.born["seq"])

    def end_us(self, item: Spoken, utterance: Utterance | None, words: int) -> int:
        """How much audio the first ``words`` words of something said take: as
        synthesized, and as the scripted voice would make any words that never were."""
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
        """How long a reply's audio is: as synthesized, or as the scripted voice would
        make it if it never was."""
        utterance = next((u for u in self.utterances.values() if u.call == call.call), None)
        if utterance is not None:
            return utterance.audio_us
        per_letter = self.providers["tts"]["seconds_per_letter_us"]
        return sum((sum(ch.isalnum() for ch in word) or 1) * per_letter for word in call.words)

    @cached_property
    def witnessed_tools(self) -> set[str]:
        """Tools whose every call the ledger records with the call id the model gave: those
        of the agent the run started with, and any other seen being called with one."""
        seen = {t.name for t in self.tools.values() if t.call_id is not None}
        unlinked = {t.name for t in self.tools.values() if t.call_id is None}
        return (set(self.scenario["witnessed_tools"]) | seen) - unlinked

    def refusal(self, call_id: str) -> Event | None:
        """The first model context that shows an error result for a tool call no witnessed
        tool received: the framework answered the call itself, as it does for arguments the
        tool cannot take or a tool that does not exist."""
        if any(t.call_id == call_id for t in self.tools.values()):
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
        """The longest the framework can take, once the user stops, to decide the turn is
        over: the scripted VAD's silence plus the session's endpointing delay."""
        return self.providers["vad"]["min_silence_us"] + self.session["min_endpointing_delay_us"]

    def turn_over_us(self, speech: Speech) -> int:
        """The latest instant at which the framework can decide that the user's turn
        holding ``speech`` is over. Speech that begins before then extends the turn."""

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

    def stopped_as_it_ended(self, playout: Playout) -> bool:
        """Whether a pause or clear reached the sink at the very instant the audio of
        ``playout`` ended. Which came first is then decided by nothing the scenario
        controls, so neither reading of that instant is held against the framework."""
        ended = playout.settled
        if ended is None:
            return False
        if ended["data"]["interrupted"]:
            # Stopped with nothing left to play: the stop landed on the last sample.
            return ended["data"]["position_us"] == ended["data"]["pushed_us"]
        return any(
            e["kind"] in ("sink.pause", "sink.clear") and e["t_us"] == ended["t_us"]
            for e in self.commands
        )

    @cached_property
    def delivered(self) -> list[Speech]:
        """Speeches whose final transcript reached the framework, in order of delivery."""
        return sorted((s for s in self.speeches if s.final), key=lambda s: s.final["seq"])

    @cached_property
    def contexts(self) -> list[tuple[str, Event, list[dict[str, Any]]]]:
        """Every record of what the agent remembers: each model context, then the final
        chat history. ``(label, event, items)``."""
        out = [(f"call {c.call}", c.request, c.items) for c in self.calls.values()]
        if self.history is not None:
            out.append(("final history", self.history, self.history["data"]["items"]))
        return out

    @cached_property
    def stamp_owner(self) -> dict[int, tuple[int, int]]:
        """stamp -> (utterance, word index)."""
        return {
            stamp: (u.utterance, index)
            for u in self.utterances.values()
            for index, stamp in enumerate(u.stamps)
        }

    def audible(self) -> list[tuple[int, int, Segment]]:
        """Every interval in which the sink was audible, in time order."""
        until = self.end["t_us"]
        spans = [(a, b, seg) for seg in self.segments.values() for a, b in seg.audible(until)]
        return sorted(spans, key=lambda span: span[0])

    @cached_property
    def playouts(self) -> dict[str, Playout]:
        """For each thing the agent was scripted to say (by key), what the sink did with it."""
        by_call = {u.call: u for u in self.utterances.values() if u.call is not None}
        loose = [u for u in self.utterances.values() if u.call is None]
        unsynthesized = [r for r in self.replies if r.call not in by_call]
        out: dict[str, Playout] = {}
        for item in self.spoken:
            unattributable = None
            if item.call is not None:
                utterance = by_call.get(item.call)
                if utterance is None and any(u.event["seq"] > item.born["seq"] for u in loose):
                    # Its text may have been altered on the way to the voice. Earful does
                    # not guess whether a later unattributed text was this reply.
                    unattributable = "a synthesized text that was not attributed could be this reply"
            else:
                utterance = self.utterances[item.instance["utterance"]]
                if any(r.response["seq"] < item.born["seq"] for r in unsynthesized):
                    unattributable = "this text could be a model reply altered before it was synthesized"
            playout = Playout(item.key, utterance, unattributable)
            if utterance is not None:
                self._account(playout, utterance)
                # Synthesized, in no finished segment, and some segment is still open: its
                # audio may be in that segment, so what was played is not yet known.
                playout.pending = not playout.segments and bool(self.open_segments())
            out[item.key] = playout
        return out

    def playout(self, call: int) -> Playout:
        return self.playouts[f"reply {call}"]

    def _account(self, playout: Playout, utterance: Utterance) -> None:
        index_of = {stamp: i for i, stamp in enumerate(utterance.stamps)}
        first_stamp = utterance.stamps[0] if utterance.stamps else None
        for segment in self.segments.values():
            # A segment says which utterance it began with as soon as it is audible, even
            # if the run ends before it finishes.
            if segment.started is not None and segment.started["data"]["utterance"] == utterance.utterance:
                playout.first_audible_us = segment.started["t_us"]
            done = segment.finished
            # What a segment played is known only once it has finished.
            if done is None or utterance.utterance not in done["data"]["utterances"]:
                continue
            playout.segments.append(segment)
            d = done["data"]
            playout.interrupted = playout.interrupted or d["interrupted"]
            for stamp, at in zip(d["words_played"], d["words_completed_us"]):
                if stamp in index_of:
                    playout.played += 1
                    playout.completed_us[index_of[stamp]] = at
            if d["word_cut"] in index_of:
                playout.cut = 1
            if segment.started is not None and playout.first_audible_us is None:
                if d["utterances"][0] == utterance.utterance:
                    # First in a segment that became audible: it began when the segment did,
                    # even if less than one whole sample of it then played.
                    playout.first_audible_us = segment.started["t_us"]
                elif first_stamp in d["words_played"]:
                    # Not first in its segment: it began when the word before it completed.
                    position = d["words_played"].index(first_stamp)
                    if position > 0:
                        playout.first_audible_us = d["words_completed_us"][position - 1]
                elif d["word_cut"] == first_stamp and d["words_completed_us"]:
                    playout.first_audible_us = d["words_completed_us"][-1]
            if playout.settled is None or done["seq"] > playout.settled["seq"]:
                playout.settled = done

    def open_segments(self) -> list[int]:
        """Segments the sink had begun that had not finished when the run ended."""
        seen = {e["data"]["segment"] for e in self.commands if e["data"]["segment"] is not None}
        seen |= set(self.segments)
        return sorted(n for n in seen if n not in self.segments or self.segments[n].finished is None)

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
