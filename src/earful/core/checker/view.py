"""A ledger, read once into the records the contracts reason about."""

from __future__ import annotations

from functools import cached_property
from typing import Any

from .attribution import Reading, candidates, possible_readings
from .records import (
    Effect,
    Event,
    ModelCall,
    Playout,
    Segment,
    Speech,
    Spoken,
    ToolInvocation,
    Utterance,
    could_be,
    unseeded,
)

MAX_READINGS = 512


def readings(events: list[Event]) -> list[Reading]:
    return possible_readings(events, MAX_READINGS)


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


__all__ = [
    "MAX_READINGS",
    "Effect",
    "Event",
    "ModelCall",
    "Playout",
    "Reading",
    "Segment",
    "Speech",
    "Spoken",
    "ToolInvocation",
    "Utterance",
    "View",
    "candidates",
    "could_be",
    "readings",
    "unseeded",
]
