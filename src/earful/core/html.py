"""A ledger as one HTML page: a timeline of the run, the verdicts and every event."""

from __future__ import annotations

import html
import json
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .checker import FAIL, NOT_EXERCISED, PASS, UNOBSERVABLE, PolicyError, Report, Verdict, check
from .checker.view import Segment
from .ledger import ledger_hash

Event = dict[str, Any]

PX_PER_S = 120.0  # at this scale a word of the scripted voice is wide enough for its text
MAX_WIDTH = 9600.0  # a longer run is drawn at a smaller scale
LABEL_W = 172
PAD_L, PAD_R = 18.0, 28.0
AXIS_H = 26.0
BAND_H = 20.0
CH = 6.1  # width reserved for one character of the 10px monospace labels
ELLIPSIS = "…"

CONTRACT_TITLES = {
    "A1": (
        "Retired intent",
        "After the user corrects or cancels an intent, no new write for it starts.",
    ),
    "A2": (
        "One logical effect",
        "Durable effects for an intent do not exceed the count the scenario authorized.",
    ),
    "A3": (
        "Truthful confirmation",
        "A claim of success finishes playing only after the backend has recorded the effect.",
    ),
    "M1": (
        "Played vs. remembered",
        "What the agent remembers having said matches what was played.",
    ),
    "M2": (
        "Turn integrity",
        "Each model call is shown the conversation that happened, once each, in order.",
    ),
    "L1": ("Progress", "A turn that is owed a response gets one within its budget."),
    "L2": ("Interruption", "While the user holds the floor the agent is silent."),
}

FLAGS = {
    "reading_split": (
        "Another reading of the ledger gives this instance a worse outcome. "
        "The result depends on the reading taken."
    ),
    "most_favourable_reading": (
        "Which copy of identical words was voiced or remembered is a reading. "
        "The checker reports the reading most favourable to the system."
    ),
    "judged_on_final_history": (
        "No model call followed the playout, so the final chat history is the judged record."
    ),
    "never_reached_the_output": (
        "No audio of it reached the sink, so only the final history is judged."
    ),
    "stopped_as_it_ended": (
        "Every word played and a stop reached the sink as the audio ended. "
        "Either interrupted mark is accepted."
    ),
    "played_on_after_the_hangup": "The sink consumed audio of it after the user had left.",
    "reference_in_doubt": (
        "A pause or clear reached the sink later than the bound. It stays a failure until "
        "the computed interruption point is confirmed against the framework's threshold."
    ),
    "silenced_by_a_tool": "The turn got no spoken reply because its tools asked for none.",
}

OUTCOME_WORDS = {
    PASS: "pass",
    FAIL: "fail",
    UNOBSERVABLE: "unobservable",
    NOT_EXERCISED: "not exercised",
    "invalid": "invalid",
    "unchecked": "not checked",
}


def _e(value: Any) -> str:
    """``value`` as text safe in HTML content and quoted attributes; all ledger text passes here."""
    text = value if isinstance(value, str) else str(value)
    text = text.encode("utf-8", "replace").decode("utf-8")  # a lone surrogate cannot be written
    text = "".join(ch if ch >= " " or ch in "\n\t" else "�" for ch in text)
    return html.escape(text, quote=True)


def _one_line(value: Any) -> str:
    return " ".join(str(value).split())


def _cells(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _w(text: str) -> float:
    return _cells(text) * CH


def _clip(value: Any, cells: int) -> str:
    text = _one_line(value)
    if _cells(text) <= cells:
        return text
    out, used = [], 0
    for ch in text:
        used += _cells(ch)
        if used > cells - 1:
            break
        out.append(ch)
    return "".join(out).rstrip() + ELLIPSIS


def _json(value: Any, *, indent: int | None = None) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, indent=indent)
    except (TypeError, ValueError):
        return repr(value)


def _n(value: float) -> str:
    text = f"{value:.1f}"
    return text.removesuffix(".0")


def _sec(us: int) -> str:
    sign, us = ("-" if us < 0 else ""), abs(us)
    whole, part = divmod(us, 1_000_000)
    if part % 1000 == 0:
        return f"{sign}{whole}.{part // 1000:03d} s"
    return f"{sign}{whole}.{part:06d} s"


def _int(data: Any, key: str) -> int | None:
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str(data: Any, key: str, default: str = "") -> str:
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, str) else default


def _list(data: Any, key: str) -> list[Any]:
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, list) else []


def _dict(data: Any, key: str) -> dict[str, Any]:
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, dict) else {}


def _order(key: Any) -> tuple[bool, int, str]:
    """Numbers in numeric order, then anything else by its text."""
    return (not isinstance(key, int), key if isinstance(key, int) else 0, str(key))


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


@dataclass
class _Ev:
    i: int  # its position in the ledger: what anchors and links use
    seq: str
    t: int | None  # virtual time; None if the line has none
    source: str
    kind: str
    data: dict[str, Any]
    raw: Any


@dataclass
class _Pair:
    number: Any
    start: _Ev | None = None
    outcome: _Ev | None = None
    timed_out: _Ev | None = None  # a write only: its caller stopped waiting


@dataclass
class _Playout:
    number: Any
    started: _Ev | None = None
    pauses: list[tuple[_Ev, _Ev | None]] = field(default_factory=list)
    finished: _Ev | None = None


class _Run:
    """A ledger indexed for drawing. Nothing here raises on a malformed line."""

    def __init__(self, events: list[Event]) -> None:
        self.evs: list[_Ev] = []
        for i, raw in enumerate(events):
            line = raw if isinstance(raw, dict) else {}
            t = _int(line, "t_us")
            seq = line.get("seq")
            self.evs.append(
                _Ev(
                    i=i,
                    seq=str(seq) if isinstance(seq, int) else "?",
                    t=t,
                    source=_str(line, "source", "?"),
                    kind=_str(line, "kind", "?"),
                    data=_dict(line, "data"),
                    raw=raw,
                )
            )
        first = self.evs[0] if self.evs else None
        self.header = first.data if first is not None and first.kind == "run.start" else {}
        self.scenario = _dict(self.header, "scenario")
        self.origin: _Ev | None = None
        self.hangup: _Ev | None = None
        self.speech_events: dict[Any, dict[str, _Ev]] = {}
        self.calls: dict[Any, _Pair] = {}
        self.tools: dict[Any, _Pair] = {}
        self.writes: dict[Any, _Pair] = {}
        self.utterances: dict[Any, _Ev] = {}
        self.segments: dict[Any, _Playout] = {}
        self.commands: list[_Ev] = []
        self.framework: list[_Ev] = []

        def pair(table: dict[Any, _Pair], number: Any) -> _Pair | None:
            if not isinstance(number, (int, str)):
                return None
            return table.setdefault(number, _Pair(number))

        def segment(number: Any) -> _Playout | None:
            if not isinstance(number, (int, str)):
                return None
            return self.segments.setdefault(number, _Playout(number))

        for ev in self.evs:
            if ev.t is None:
                continue
            kind, d = ev.kind, ev.data
            if kind == "user.origin":
                self.origin = self.origin or ev
            elif kind == "user.hangup":
                self.hangup = self.hangup or ev
            elif kind in ("user.vad_start", "user.vad_end", "user.transcript_final"):
                if isinstance(d.get("speech"), int):
                    self.speech_events.setdefault(d["speech"], {}).setdefault(kind, ev)
            elif kind in ("llm.request", "llm.response"):
                found = pair(self.calls, d.get("call"))
                if found is not None and kind == "llm.request" and found.start is None:
                    found.start = ev
                elif found is not None and kind == "llm.response" and found.outcome is None:
                    found.outcome = ev
            elif kind == "tool.called":
                found = pair(self.tools, d.get("tool"))
                if found is not None and found.start is None:
                    found.start = ev
            elif kind in ("tool.returned", "tool.raised", "tool.cancelled"):
                found = pair(self.tools, d.get("tool"))
                if found is not None and found.outcome is None:
                    found.outcome = ev
            elif kind == "effect.requested":
                found = pair(self.writes, d.get("request"))
                if found is not None and found.start is None:
                    found.start = ev
            elif kind in ("effect.committed", "effect.deduplicated", "effect.failed"):
                found = pair(self.writes, d.get("request"))
                if found is not None and found.outcome is None:
                    found.outcome = ev
            elif kind == "effect.timed_out":
                found = pair(self.writes, d.get("request"))
                if found is not None and found.timed_out is None:
                    found.timed_out = ev
            elif kind == "tts.synthesized":
                if isinstance(d.get("utterance"), (int, str)):
                    self.utterances.setdefault(d["utterance"], ev)
            elif kind == "playout.started":
                seg = segment(d.get("segment"))
                if seg is not None and seg.started is None:
                    seg.started = ev
            elif kind == "playout.paused":
                seg = segment(d.get("segment"))
                if seg is not None:
                    seg.pauses.append((ev, None))
            elif kind == "playout.resumed":
                seg = segment(d.get("segment"))
                if seg is not None and seg.pauses and seg.pauses[-1][1] is None:
                    seg.pauses[-1] = (seg.pauses[-1][0], ev)
            elif kind == "playout.finished":
                seg = segment(d.get("segment"))
                if seg is not None and seg.finished is None:
                    seg.finished = ev
            elif kind.startswith("sink."):
                self.commands.append(ev)
            elif kind.startswith("fw."):
                self.framework.append(ev)

        times = [ev.t for ev in self.evs if ev.t is not None]
        self.base = self.origin.t if self.origin and self.origin.t is not None else 0
        self.t_lo = min([*times, self.base]) if times else 0
        self.t_hi = max(times) if times else 0

    @property
    def speeches(self) -> list[dict[str, Any]]:
        return [
            s
            for s in _list(self.scenario, "speeches")
            if _int(s, "speech") is not None
            and _int(s, "start_us") is not None
            and _int(s, "end_us") is not None
        ]

    @property
    def effect_tools(self) -> dict[str, str]:
        """Tools whose body the run replaced by one write: tool name -> intent."""
        return {
            _str(e, "tool"): _str(e, "intent")
            for e in _list(self.scenario, "effects")
            if _str(e, "tool")
        }


@dataclass
class _Group:
    anchor: str
    short: str  # HTML: what it is, in a few words
    title: str  # HTML: the same, with what it says
    events: list[int]
    detail: str


class _Selections:
    def __init__(self, run: _Run, report: Report | None) -> None:
        self.groups: list[_Group] = []
        self.of_verdict: dict[int, _Group] = {}  # id(verdict) -> group
        self.of_finding: dict[int, _Group] = {}
        self.number: dict[int, int] = {}
        self.member: dict[int, list[_Group]] = {}  # event index -> its groups
        if report is None:
            return
        last = len(run.evs) - 1
        count = 0
        for result in report.results.values():
            for v in result.verdicts:
                count += 1
                self.number[id(v)] = count
                short = f"<b>{_e(v.contract)} {_e(OUTCOME_WORDS.get(v.outcome, v.outcome))}</b>"
                title = f"{short}, {_e(_instance(v.instance))}: {_e(v.summary)}"
                seqs = sorted({s for s in v.evidence if 0 <= s <= last})
                group = _Group(f"v{count}", short, title, seqs, f"d{count}")
                self._add(group)
                self.of_verdict[id(v)] = group
                for at, f in enumerate(v.findings, start=1):
                    seqs = sorted({s for s in f.seqs if 0 <= s <= last})
                    if not seqs:
                        continue
                    short = f"<b>{_e(v.contract)} {_e(f.rule)}</b>"
                    group = _Group(
                        f"f{count}-{at}", short, f"{short}: {_e(f.detail)}", seqs, f"d{count}"
                    )
                    self._add(group)
                    self.of_finding[id(f)] = group

    def _add(self, group: _Group) -> None:
        self.groups.append(group)
        for index in group.events:
            self.member.setdefault(index, []).append(group)

    def classes(self, index: int) -> str:
        return " ".join([f"s{index}", *(g.anchor for g in self.member.get(index, []))])


def _instance(instance: dict[str, Any]) -> str:
    parts = []
    for key, value in instance.items():
        shown = value if isinstance(value, (str, int)) else _json(value)
        parts.append(f"{key} {shown}")
    return ", ".join(parts) or "the run"


@dataclass
class _Item:
    x0: float
    x1: float
    draw: Callable[[float], str]


@dataclass
class _Sub:
    label: str  # HTML
    row_h: float
    items: list[_Item] = field(default_factory=list)
    hint: str = ""
    single_row: bool = False  # never wraps: its items were laid out not to collide
    empty: str = ""
    label_dy: float = 0  # how far below the top of its first row the name sits


@dataclass
class _Lane:
    key: str
    title: str
    note: str
    subs: list[_Sub] = field(default_factory=list)


def _pack(items: list[_Item], gap: float = 10.0) -> list[list[_Item]]:
    rows: list[list[_Item]] = []
    ends: list[float] = []
    for item in sorted(items, key=lambda it: it.x0):
        for at, end in enumerate(ends):
            if item.x0 >= end + gap:
                rows[at].append(item)
                ends[at] = item.x1
                break
        else:
            rows.append([item])
            ends.append(item.x1)
    return rows


def _status_shape(outcome: str) -> str:
    if outcome == PASS:
        return (
            '<circle class="st-pass" cx="6" cy="6" r="6"/>'
            '<path class="st-on" d="M3.2 6.3l2 2L8.9 4.2"/>'
        )
    if outcome == FAIL:
        return (
            '<rect class="st-fail" width="12" height="12" rx="2"/>'
            '<path class="st-on" d="M3.6 3.6l4.8 4.8M8.4 3.6l-4.8 4.8"/>'
        )
    if outcome == UNOBSERVABLE:
        return (
            '<circle class="st-unob" cx="6" cy="6" r="5.2"/>'
            '<path class="st-unob-in" '
            'd="M4.4 4.6a1.7 1.7 0 1 1 2.5 1.5c-.6.4-.9.7-.9 1.4M6 9.3v.4"/>'
        )
    if outcome == NOT_EXERCISED:
        return (
            '<circle class="st-none" cx="6" cy="6" r="5.2"/><path class="st-none" d="M3.6 6h4.8"/>'
        )
    return (
        '<path class="st-inv" d="M6 1.2L11.4 10.8H.6Z"/><path class="st-inv" d="M6 4.6v3M6 9v.3"/>'
    )


def _badge(outcome: str, text: str | None = None) -> str:
    word = OUTCOME_WORDS.get(outcome, outcome) if text is None else text
    return (
        f'<span class="badge b-{_e(outcome if outcome in OUTCOME_WORDS else "other")}">'
        f'<svg class="sg" width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">'
        f"{_status_shape(outcome)}</svg>{_e(word)}</span>"
    )


def _glyph(name: str, x: float, y: float) -> str:
    g = f'<g transform="translate({_n(x)} {_n(y)})">'
    shapes = {
        "play": '<path class="gf" d="M1 0l8 5l-8 5z"/>',
        "pause": '<path class="gf" d="M1 0h3v10h-3zM6 0h3v10h-3z"/>',
        "x": '<path class="gs" d="M1.5 1.5l7 7M8.5 1.5l-7 7"/>',
        "flush": '<path class="gs" d="M0 5h6M3.5 2.5L6 5L3.5 7.5M8.5 0v10"/>',
        "stop": '<rect class="gf" x="1" y="1" width="8" height="8"/>',
        "open": '<rect class="gs" x="1.5" y="1.5" width="7" height="7"/>',
        "rise": '<path class="gs" d="M0 9h3.5v-8H9"/>',
        "fall": '<path class="gs" d="M0 1h3.5v8H9"/>',
        "dot": '<circle class="gf" cx="5" cy="5" r="3.6"/>',
        "clock": (
            '<circle class="gs" cx="5" cy="5" r="4.2"/><path class="gs" d="M5 2.6V5l1.8 1.2"/>'
        ),
        "diamond": '<path class="gf" d="M5 0l5 5l-5 5l-5-5z"/>',
        "diamond-o": '<path class="gs" d="M5 .8l4.2 4.2l-4.2 4.2L.8 5z"/>',
        "torn": '<path class="gs" d="M1 -4l4 3.5l-4 3.5l4 3.5l-4 3.5l4 3.5"/>',
    }
    return g + shapes[name] + "</g>"


class _Timeline:
    def __init__(
        self,
        run: _Run,
        report: Report | None,
        selections: _Selections,
        manifest: dict[str, Any] | None,
        is_minimal: bool,
    ) -> None:
        self.run = run
        self.report = report
        self.sel = selections
        self.manifest = manifest
        self.is_minimal = is_minimal
        self.placed: dict[int, float] = {}  # event index -> its x
        base = run.base
        lo_s = (run.t_lo - base) // 1_000_000
        # An implied end time widens the axis by at most a minute past the events.
        implied = [base + s["end_us"] for s in run.speeches] if run.origin is not None else []
        for pair in run.calls.values():
            answer = pair.outcome
            if answer is not None and answer.t is not None:
                implied.append(answer.t + (_int(answer.data, "duration_us") or 0))
        latest = max([run.t_hi, *(t for t in implied if t <= run.t_hi + 60_000_000)])
        hi_s = max(-(-(latest - base) // 1_000_000), lo_s + 1)
        self.lo_s, self.hi_s = lo_s, hi_s
        span = hi_s - lo_s
        self.scale = PX_PER_S if span * PX_PER_S <= MAX_WIDTH else MAX_WIDTH / span

    def x(self, t: int) -> float:
        """Where a time is drawn. A time outside the axis is drawn at its nearer end."""
        lo, hi = self.lo_s * 1_000_000, self.hi_s * 1_000_000
        return PAD_L + (min(max(t - self.run.base, lo), hi) - lo) * self.scale / 1_000_000

    def at(self, t: int | None) -> str:
        return "no time" if t is None else _sec(t - self.run.base)

    def a(self, ev: _Ev, inner: str, tip: str = "") -> str:
        """``inner`` as the mark of ``ev``: a link that highlights the event."""
        if ev.t is not None:
            self.placed.setdefault(ev.i, self.x(ev.t))
        title = f"{ev.kind}, seq {ev.seq}, at {self.at(ev.t)}" + (f". {tip}" if tip else "")
        return f'<a class="m" href="#t{ev.i}" data-i="{ev.i}"><title>{_e(title)}</title>{inner}</a>'

    def hit(self, ev: _Ev, x: float, y: float, w: float, h: float) -> str:
        return (
            f'<rect class="hit {self.sel.classes(ev.i)}" x="{_n(x)}" y="{_n(y)}" '
            f'width="{_n(max(w, 6))}" height="{_n(h)}" rx="3"/>'
        )

    @staticmethod
    def text(x: float, y: float, value: str, cls: str = "t", extra: str = "") -> str:
        return f'<text class="{cls}" x="{_n(x)}" y="{_n(y)}"{extra}>{_e(value)}</text>'

    def flag(self, ev: _Ev, label: str, glyph: str = "", cls: str = "", tip: str = "") -> _Item:
        assert ev.t is not None
        x = self.x(ev.t)
        gw = 13.0 if glyph else 0.0
        width = 5 + gw + _w(label) + 3

        def draw(y: float) -> str:
            inner = self.hit(ev, x - 3, y + 1, width + 3, 18)
            inner += (
                f'<line class="pole {cls}" x1="{_n(x)}" x2="{_n(x)}" '
                f'y1="{_n(y + 2)}" y2="{_n(y + 18)}"/>'
            )
            if glyph:
                inner += f'<g class="{cls}">{_glyph(glyph, x + 5, y + 5)}</g>'
            inner += self.text(x + 5 + gw, y + 14, label)
            return self.a(ev, inner, tip)

        return _Item(x - 3, x + width, draw)

    def start(self, ev: _Ev, x: float, y: float, cls: str, tip: str = "") -> str:
        return self.a(
            ev,
            self.hit(ev, x - 11, y + 1, 11, 18)
            + f'<path class="arrow {cls}" d="M{_n(x - 8)} {_n(y + 4)}L{_n(x)} {_n(y + 10)}'
            f'L{_n(x - 8)} {_n(y + 16)}Z"/>',
            tip,
        )

    def user(self) -> _Lane:
        run = self.run
        lane = _Lane(
            "user",
            "User (VAD / STT)",
            "The scripted user. Speech bars are the true times the scenario declares. "
            "The VAD and the recognizer are scripted providers.",
        )
        speech = _Sub("speech, as scripted", 20, empty="the scenario declares no speech")
        reports = _Sub("VAD reports", 20)
        finals = _Sub("final transcript", 20)
        if run.origin is not None:
            speech.items.append(
                self.flag(run.origin, "origin", "", "c-user", "The axis starts here")
            )
            speech.items += [self._speech(s) for s in run.speeches]
        elif run.speeches:
            speech.empty = "no user.origin in this ledger, so the true speech times are unknown"
        for number in sorted(run.speech_events):
            events = run.speech_events[number]
            first, last = events.get("user.vad_start"), events.get("user.vad_end")
            if first is not None:
                reports.items.append(self.flag(first, f"start, speech {number}", "rise", "c-user"))
            if last is not None:
                reports.items.append(self.flag(last, f"end, speech {number}", "fall", "c-user"))
            final = events.get("user.transcript_final")
            if final is not None:
                shown = f'"{_clip(_str(final.data, "text"), 56)}"'
                finals.items.append(self.flag(final, shown, "dot", "c-user"))
        lane.subs = [speech, reports, finals]
        windows = _Sub(
            "failure window",
            20,
            hint="How far a speech can move while the reduced case still breaks the rule.",
        )
        if self.manifest is not None and self.is_minimal and run.origin is not None:
            found = [self._window(w) for w in _list(self.manifest, "windows")]
            windows.items = [item for item in found if item is not None]
        if windows.items:
            lane.subs.append(windows)
        return lane

    def _speech(self, s: dict[str, Any]) -> _Item:
        base = self.run.base
        x0, x1 = self.x(base + s["start_us"]), self.x(base + s["end_us"])
        tags = [f"speech {s['speech']}"]
        if not _str(s, "transcript"):
            tags.append("noise")
        if s.get("backchannel") is True:
            tags.append("backchannel")
        if s.get("expects_reply") is False and _str(s, "transcript"):
            tags.append("no reply expected")
        tags += [f"retires {_clip(r, 24)}" for r in _list(s, "retires")]
        label = ", ".join(tags)
        tip = (
            f"Speech {s['speech']}, declared in run.start: "
            f"{_sec(s['start_us'])} to {_sec(s['end_us'])}. "
            f"Transcript: {_str(s, 'transcript') or '(none, noise)'}"
        )

        def draw(y: float) -> str:
            return (
                f"<g><title>{_e(tip)}</title>"
                f'<rect class="speech" x="{_n(x0)}" y="{_n(y + 5)}" '
                f'width="{_n(max(x1 - x0, 2))}" height="10" rx="2"/>'
                f"{self.text(x1 + 6, y + 14, label)}</g>"
            )

        return _Item(x0, x1 + 6 + _w(label), draw)

    def _window(self, window: Any) -> _Item | None:
        name = _str(window, "variable")
        lo, hi = _int(window, "lo_us"), _int(window, "hi_us")
        if not name.startswith("speech.position ") or lo is None or hi is None:
            return None
        x0, x1 = self.x(self.run.base + lo), self.x(self.run.base + hi)
        label = f"speech {_clip(name.split()[-1], 6)} can start from {_sec(lo)} to {_sec(hi)}"
        tip = (
            f"{name}: the rule breaks at every start from {_sec(lo)} to {_sec(hi)}. "
            f"Below that: {_str(window, 'lo_end', '?')}. Above: {_str(window, 'hi_end', '?')}."
        )

        def draw(y: float) -> str:
            return (
                f"<g><title>{_e(tip)}</title>"
                f'<path class="window" d="M{_n(x0)} {_n(y + 5)}v10M{_n(x1)} {_n(y + 5)}v10'
                f'M{_n(x0)} {_n(y + 10)}H{_n(x1)}"/>'
                f"{self.text(x1 + 6, y + 14, label, 't t2')}</g>"
            )

        return _Item(x0 - 1, x1 + 6 + _w(label), draw)

    def model(self) -> _Lane:
        lane = _Lane(
            "model",
            "Model (LLM)",
            "The scripted model. A bar runs from the request to the end of generation. "
            "The hatched part is the wait for the first token.",
        )
        calls = _Sub("calls", 34, empty="no model call")
        for number in sorted(self.run.calls, key=_order):
            item = self._call(self.run.calls[number])
            if item is not None:
                calls.items.append(item)
        lane.subs = [calls]
        return lane

    def _call(self, pair: _Pair) -> _Item | None:
        asked, answer = pair.start, pair.outcome
        anchor = asked or answer
        if anchor is None or anchor.t is None:
            return None
        x0 = self.x(anchor.t)
        first = end = x0
        line1, line2 = f"call {pair.number}", "no response in the ledger"
        if answer is not None and answer.t is not None:
            d = answer.data
            ttft, duration = _int(d, "ttft_us") or 0, _int(d, "duration_us") or 0
            first = self.x(answer.t + max(ttft, 0))
            end = max(first, self.x(answer.t + max(duration, 0)))
            reply = _str(d, "text")
            line1 += f' "{_clip(reply, 52)}"' if reply.strip() else " (no text)"
            parts = [f"first token +{_sec(ttft)}, end +{_sec(duration)}"]
            for tool_call in _list(d, "tool_calls"):
                name = _clip(_str(tool_call, "name", "?"), 28)
                arguments = tool_call.get("arguments", "") if isinstance(tool_call, dict) else ""
                parts.append(f"calls {name}({_clip(arguments, 32)})")
            for claim in _list(d, "claims"):
                parts.append(
                    f"claims {_clip(_str(claim, 'intent', '?'), 28)} "
                    f"(words {_int(claim, 'first_word')} to {_int(claim, 'last_word')})"
                )
            line2 = "; ".join(parts)
        width = max(_w(line1), _w(line2))

        def draw(y: float) -> str:
            out = ""
            if asked is not None:
                out += self.start(asked, x0, y, "c-model", "The context the model was shown")
            if answer is None:
                return out + self.text(x0 + 6, y + 13, f"{line1}: {line2}", "t t2")
            inner = self.hit(answer, x0, y + 1, end - x0 + 8 + width, 30)
            inner += (
                f'<rect class="wait-model" x="{_n(x0)}" y="{_n(y + 4)}" '
                f'width="{_n(max(first - x0, 0))}" height="12"/>'
                f'<rect class="gen" x="{_n(first)}" y="{_n(y + 4)}" '
                f'width="{_n(max(end - first, 2))}" height="12" rx="2"/>'
                f'<line class="tick-model" x1="{_n(first)}" x2="{_n(first)}" '
                f'y1="{_n(y + 2)}" y2="{_n(y + 18)}"/>'
            )
            inner += self.text(end + 7, y + 13, line1)
            inner += self.text(end + 7, y + 26, line2, "t t2")
            return out + self.a(
                answer, inner, "The tick is the first token. The bar ends when generation ends"
            )

        return _Item(x0 - 11, end + 8 + width, draw)

    def tools(self) -> _Lane:
        run = self.run
        lane = _Lane(
            "tools",
            "Tools / backend",
            "The tool witness and a fake backend. A tool returning and a write committing "
            "are separate events.",
        )
        invocations = _Sub("tool calls", 34, empty="no tool was called")
        writes = _Sub("backend writes", 34, empty="no write was requested")
        effects = run.effect_tools
        for number in sorted(run.tools, key=_order):
            pair = run.tools[number]
            called, done = pair.start, pair.outcome
            if called is None:
                continue
            name = _str(called.data, "name", "?")
            arguments = _clip(_json(called.data.get("arguments")), 40)
            line1 = f"tool {number} {_clip(name, 30)}({arguments})"
            if name in effects:
                line1 += " [body replaced by one write]"
            if done is None:
                glyph, line2 = "", "no outcome in the ledger"
            elif done.kind == "tool.returned":
                glyph = "stop"
                line2 = f"returned {_clip(_json(done.data.get('output')), 60)}"
                if done.data.get("expects_reply") is False:
                    line2 += " (asks for no reply)"
            elif done.kind == "tool.raised":
                glyph = "x"
                line2 = _clip(
                    f"raised {_str(done.data, 'error', '?')}: {_str(done.data, 'message')}", 70
                )
            else:
                glyph, line2 = "open", "cancelled before it returned"
            solid = done is not None and done.kind != "tool.cancelled"
            invocations.items.append(self._interval(pair, glyph, line1, line2, "tool", solid))

        for number in sorted(run.writes, key=_order):
            pair = run.writes[number]
            asked, done = pair.start, pair.outcome
            if asked is None:
                continue
            d = asked.data
            line1 = f"write {number} for {_clip(_str(d, 'intent', '?'), 30)}"
            if d.get("key") is not None:
                line1 += f", key {_clip(d.get('key'), 20)}"
            if d.get("tool") is not None:
                line1 += f", asked by tool {_clip(d.get('tool'), 8)}"
            if done is None:
                glyph, line2 = "", "no outcome in the ledger"
            elif done.kind == "effect.committed":
                glyph = "diamond"
                line2 = (
                    f"committed, receipt {_clip(done.data.get('receipt'), 20)}, "
                    f"effect {_clip(done.data.get('count_for_intent'), 6)} for this intent"
                )
            elif done.kind == "effect.deduplicated":
                glyph = "diamond-o"
                line2 = (
                    "deduplicated, the key had committed: "
                    f"receipt {_clip(done.data.get('receipt'), 20)}"
                )
            else:
                glyph, line2 = "x", "failed: the backend rejected it, nothing committed"
            writes.items.append(
                self._interval(pair, glyph, line1, line2, "write", done is not None)
            )
            gave_up = pair.timed_out
            if gave_up is not None:
                after = _int(gave_up.data, "after_us")
                waited = f" after {_sec(after)}" if after is not None else ""
                item = self.flag(
                    gave_up,
                    f"caller of write {number} timed out{waited}",
                    "clock",
                    "c-tool",
                    "What the caller was told. The outcome of the write is separate",
                )
                writes.items.append(_Item(item.x0, item.x1, _lower(item.draw, 7)))
        lane.subs = [invocations, writes]
        return lane

    def _interval(
        self, pair: _Pair, glyph: str, line1: str, line2: str, kind: str, solid: bool
    ) -> _Item:
        begun, done = pair.start, pair.outcome
        assert begun is not None and begun.t is not None
        x0 = self.x(begun.t)
        x1 = self.x(done.t) if done is not None and done.t is not None else self.x(self.run.t_hi)
        x1 = max(x1, x0)

        def draw(y: float) -> str:
            out = (
                f'<rect class="bar-{kind}-open{f" bar-{kind}" if solid else ""}" '
                f'x="{_n(x0)}" y="{_n(y + 6)}" width="{_n(x1 - x0)}" height="8"/>'
            )
            out += self.start(begun, x0, y, f"c-{kind}")
            out += self.a(
                begun,
                self.hit(begun, x1 + 15, y + 2, _w(line1) + 4, 14)
                + self.text(x1 + 17, y + 13, line1),
            )
            second = self.text(x1 + 17, y + 26, line2, "t t2")
            if done is None:
                return out + second
            inner = self.hit(done, x1, y + 1, 14, 18)
            inner += self.hit(done, x1 + 15, y + 16, _w(line2) + 4, 14)
            inner += f'<g class="c-{kind}">{_glyph(glyph, x1 + 2, y + 5)}</g>' + second
            return out + self.a(done, inner)

        return _Item(x0 - 11, x1 + 19 + max(_w(line1), _w(line2)), draw)

    def voice(self) -> _Lane:
        run = self.run
        lane = _Lane(
            "voice",
            "Voice / sink (playout)",
            "The scripted voice and a simulated sink. Playout is the audio the sink consumed, "
            "sample by sample.",
        )
        synthesis = _Sub(
            "synthesis",
            20,
            empty="nothing was synthesized",
            hint="From the first complete word the voice accepted. The hatched part is the "
            "wait for the first audio.",
        )
        for number in sorted(run.utterances, key=_order):
            synthesis.items.append(self._utterance(number, run.utterances[number]))
        playout = _Sub("playout, word by word", 46, empty="nothing was played", label_dy=11)
        for number in sorted(run.segments, key=_order):
            item = self._segment(run.segments[number])
            if item is not None:
                playout.items.append(item)
        commands = _Sub(
            "sink commands",
            20,
            empty="the sink received no command",
            hint="What the framework told the output to do. Playout above is what the output did.",
        )
        glyphs = {
            "sink.pause": "pause",
            "sink.resume": "play",
            "sink.clear": "x",
            "sink.flush": "flush",
        }
        for ev in run.commands:
            segment = ev.data.get("segment")
            tip = "No segment had audio" if segment is None else f"Segment {_clip(segment, 8)}"
            name = _clip(ev.kind.split(".", 1)[1], 20)
            commands.items.append(self.flag(ev, name, glyphs.get(ev.kind, ""), "c-voice", tip))
        lane.subs = [synthesis, playout, commands]
        return lane

    def _utterance(self, number: Any, ev: _Ev) -> _Item:
        assert ev.t is not None
        d = ev.data
        started, ttfb = _int(d, "started_us"), _int(d, "ttfb_us") or 0
        x_end = self.x(ev.t)
        x0 = self.x(started) if started is not None else x_end
        x_audio = self.x(started + ttfb) if started is not None else x_end
        words = len(_list(d, "stamps")) or len(_str(d, "text").split())
        attribution = _str(d, "attribution", "?")
        call = d.get("call")
        if attribution in ("unique", "prefix") and call is not None:
            whose = f"reply of call {_clip(call, 8)} ({attribution})"
        elif attribution == "ambiguous":
            options = " or ".join(_clip(c, 6) for c in _list(d, "candidates"))
            taken = self.report.reading.get(number) if self.report is not None else None
            whose = f"could be call {options}"
            whose += f", read as call {taken}" if taken is not None else ""
        elif attribution == "none":
            whose = "no model reply has this text"
        else:
            whose = f"attribution {_clip(attribution, 16)}"
        label = f"utterance {number}: {whose}, {_plural(words, 'word')}"
        right = max(x_end, x_audio)

        def draw(y: float) -> str:
            inner = self.hit(ev, x0 - 2, y + 1, right - x0 + 10 + _w(label), 18)
            inner += (
                f'<rect class="wait-voice" x="{_n(x0)}" y="{_n(y + 5)}" '
                f'width="{_n(max(x_audio - x0, 0))}" height="10"/>'
                f'<line class="tick-voice" x1="{_n(x_audio)}" x2="{_n(x_audio)}" '
                f'y1="{_n(y + 2)}" y2="{_n(y + 18)}"/>'
            )
            if x_end > x_audio:
                inner += (
                    f'<line class="line-voice" x1="{_n(x_audio)}" x2="{_n(x_end)}" '
                    f'y1="{_n(y + 10)}" y2="{_n(y + 10)}"/>'
                    f'<line class="pole c-voice" x1="{_n(x_end)}" x2="{_n(x_end)}" '
                    f'y1="{_n(y + 5)}" y2="{_n(y + 15)}"/>'
                )
            inner += self.text(right + 7, y + 14, label)
            return self.a(
                ev,
                inner,
                "The tick is the first audio the voice released. "
                "The event is recorded when the utterance is final",
            )

        return _Item(x0 - 2, right + 8 + _w(label), draw)

    def _segment(self, seg: _Playout) -> _Item | None:
        run = self.run
        done = seg.finished
        begun = seg.started
        if begun is None and done is None:
            return None
        d = done.data if done is not None else {}
        if begun is None:
            assert done is not None and done.t is not None
            x = self.x(done.t)
            label = f"segment {seg.number} was cleared before it became audible"

            def draw_silent(y: float) -> str:
                assert done is not None
                inner = self.hit(done, x - 3, y + 11, 18 + _w(label), 20)
                inner += (
                    f'<rect class="seg-none" x="{_n(x)}" y="{_n(y + 12)}" width="10" height="18"/>'
                )
                inner += self.text(x + 15, y + 25, label, "t t2")
                return self.a(done, inner)

            return _Item(x - 3, x + 16 + _w(label), draw_silent)

        assert begun.t is not None
        t0 = begun.t
        t1 = done.t if done is not None and done.t is not None else run.t_hi
        x0, x1 = self.x(t0), self.x(max(t1, t0))
        # The checker's own record of a segment says when it was audible.
        audible = Segment(
            0,
            {"t_us": t0},
            [({"t_us": p.t}, {"t_us": r.t} if r is not None else None) for p, r in seg.pauses],
            {"t_us": t1},
        ).audible(t1)
        audible = [(a, b) for a, b in audible if b >= a]

        def position(t: int) -> int:
            return sum(max(0, min(b, t) - a) for a, b in audible)

        def instant(pos: int) -> int:
            for a, b in audible:
                if pos <= b - a:
                    return a + pos
                pos -= b - a
            return audible[-1][1] if audible else t0

        word_of: dict[Any, tuple[str, int, int, int]] = {}  # word, microseconds, samples, rate
        order: list[Any] = []
        for number in _list(d, "utterances"):
            source = run.utterances.get(number) if isinstance(number, (int, str)) else None
            if source is None:
                continue
            stamps = _list(source.data, "stamps")
            texts = _str(source.data, "text").split()
            samples = _list(source.data, "word_samples")
            rate = _int(source.data, "sample_rate") or 0
            for at, stamp in enumerate(stamps):
                if not isinstance(stamp, int):
                    continue
                count = samples[at] if at < len(samples) and isinstance(samples[at], int) else 0
                length = -(-count * 1_000_000 // rate) if rate > 0 else 0
                word_of[stamp] = (texts[at] if at < len(texts) else "", length, count, rate)
                order.append(stamp)
        played = [s for s in _list(d, "words_played") if isinstance(s, int)]
        completed = [c for c in _list(d, "words_completed_us") if isinstance(c, int)]
        left = run.hangup.t if run.hangup is not None else None
        boxes: list[tuple[float, float, str, str, str]] = []  # x0, x1, class, word, tip
        previous = t0
        for stamp, end in zip(played, completed):
            word, length, _count, _rate = word_of.get(stamp, ("", 0, 0, 0))
            begin = instant(max(position(end) - length, 0)) if length else previous
            begin = min(max(begin, previous, t0), end)
            late = left is not None and end > left
            tip = f'"{word}" completed at {self.at(end)}' if word else f"word {stamp} completed"
            if late:
                tip += ", after the user had hung up"
            boxes.append((self.x(begin), self.x(end), "w-late" if late else "w", word, tip))
            previous = end
        cut = d.get("word_cut")
        cut_note = ""
        if isinstance(cut, int):
            word, _length, total, rate = word_of.get(cut, ("", 0, 0, 0))
            samples_played = _int(d, "word_cut_samples") or 0
            part = samples_played * 1_000_000 // rate if rate > 0 else 0
            stop = instant(position(previous) + part) if audible else previous
            stop = max(stop, previous)
            of = f" of {total}" if total else ""
            cut_note = f'cut "{_clip(word, 20)}" ({samples_played}{of} samples)'
            tip = f'"{word}" was cut: {samples_played}{of} of its samples played'
            boxes.append(
                (self.x(previous), max(self.x(stop), self.x(previous) + 3), "w-cut", "", tip)
            )
        unplayed = [word_of[s][0] for s in order if s not in set(played) and s != cut]

        interrupted = d.get("interrupted") is True
        if done is None:
            summary = f"segment {seg.number} had not finished when the ledger ends"
        else:
            pushed, reached = _int(d, "pushed_us"), _int(d, "position_us")
            audio = (
                f"{_sec(reached)} of {_sec(pushed)}"
                if reached is not None and pushed is not None
                else "part"
            )
            if interrupted:
                summary = f"segment {seg.number} interrupted after {audio} of audio"
            else:
                summary = f"segment {seg.number} played out, {_sec(pushed or 0)} of audio"
            counts = [_plural(len(played), "word") + " played"]
            if cut_note:
                counts.append(cut_note)
            summary += ": " + ", ".join(counts)
            if left is not None and any(c > left for c in completed):
                summary += "; words after the hangup reached no one"
        ghost = _clip(" ".join(unplayed), 70) if unplayed else ""
        if unplayed:
            summary += f", {len(unplayed)} not played:"
        width = _w(summary) + (_w(ghost) + 6 if ghost else 0)

        def draw(y: float) -> str:
            top = y + 12
            out = (
                f'<rect class="seg {"seg-cut" if interrupted or done is None else ""}" '
                f'x="{_n(x0)}" y="{_n(top)}" width="{_n(max(x1 - x0, 2))}" height="18"/>'
            )
            for paused, resumed in seg.pauses:
                assert paused.t is not None
                until = resumed.t if resumed is not None and resumed.t is not None else t1
                px0, px1 = self.x(paused.t), self.x(max(until, paused.t))
                out += (
                    f'<rect class="paused" x="{_n(px0)}" y="{_n(top)}" '
                    f'width="{_n(px1 - px0)}" height="18"><title>not audible: paused from '
                    f"{_e(self.at(paused.t))} to {_e(self.at(until))}</title></rect>"
                )
            for bx0, bx1, cls, word, tip in boxes:
                out += (
                    f'<g><title>{_e(tip)}</title><rect class="{cls}" x="{_n(bx0 + 1)}" '
                    f'y="{_n(top + 1)}" width="{_n(max(bx1 - bx0 - 1, 1))}" height="16"/>'
                )
                if cls != "w-cut":
                    out += (
                        f'<line class="done {cls}-done" x1="{_n(bx1)}" x2="{_n(bx1)}" '
                        f'y1="{_n(top)}" y2="{_n(top + 18)}"/>'
                    )
                room = bx1 - bx0 - 2.4
                need = _cells(word) * 6.02
                if word and need <= room * 1.3:
                    out += self.text(
                        bx0 + 1.6,
                        top + 12.5,
                        word,
                        "t wt",
                        f' textLength="{_n(min(need, room))}" lengthAdjust="spacingAndGlyphs"',
                    )
                out += "</g>"
            out += self.start(begun, x0, y + 11, "c-voice", "The segment became audible")
            for paused, resumed in seg.pauses:
                assert paused.t is not None
                px = self.x(paused.t)
                out += self.a(
                    paused,
                    self.hit(paused, px - 1, y, 12, 12)
                    + f'<g class="c-voice" transform="translate({_n(px + 1)} {_n(y + 1)}) '
                    f'scale(.9)">{_glyph("pause", 0, 0)}</g>',
                    "The segment stopped being audible",
                )
                if resumed is not None and resumed.t is not None:
                    rx = self.x(resumed.t)
                    out += self.a(
                        resumed,
                        self.hit(resumed, rx - 11, y, 12, 12)
                        + f'<g class="c-voice" transform="translate({_n(rx - 10)} {_n(y + 1)}) '
                        f'scale(.9)">{_glyph("play", 0, 0)}</g>',
                        "The segment became audible again",
                    )
            tail = self.text(x0, y + 42, summary, "t t2")
            if ghost:
                tail += self.text(x0 + _w(summary) + 6, y + 42, ghost, "t ghost")
            if done is None:
                return out + tail
            cap = (
                f'<g class="c-voice">{_glyph("torn", x1, top + 4)}</g>'
                if interrupted
                else f'<rect class="seg-end" x="{_n(x1)}" y="{_n(top)}" width="3" height="18"/>'
            )
            inner = self.hit(done, x1 - 1, top - 1, 10, 20)
            inner += self.hit(done, x0 - 2, y + 31, width + 4, 14)
            return out + self.a(
                done,
                inner + cap + tail,
                "The segment ended. What it played is recorded here",
            )

        return _Item(x0 - 11, max(x1 + 10, x0 + width), draw)

    def framework(self) -> _Lane:
        lane = _Lane(
            "fw",
            "Framework",
            "The framework's own events, and the harness. They explain a run. "
            "They decide no verdict.",
        )
        by_kind: dict[str, list[_Ev]] = {}
        for ev in self.run.framework:
            by_kind.setdefault(ev.kind, []).append(ev)
        for kind, events in by_kind.items():
            sub = _Sub(_e(_clip(kind[3:] or kind, 24)), 14, hint=kind, single_row=True)
            xs: list[float] = []
            for ev in events:
                assert ev.t is not None
                x = self.x(ev.t)
                if xs and x < xs[-1] + 4:
                    x = xs[-1] + 4  # same instant: side by side, in ledger order
                xs.append(x)
            for at, ev in enumerate(events):
                x = xs[at]
                room = (xs[at + 1] if at + 1 < len(xs) else x + 400) - x - 8
                label = _fw_label(ev.data)
                label = label if label and _w(_clip(label, 40)) <= room else ""
                label = _clip(label, 40)
                sub.items.append(self._tick(ev, x, min(room + 6, 8 + _w(label)), label))
            lane.subs.append(sub)
        if not by_kind:
            lane.subs.append(_Sub("", 14, empty="no framework event in this ledger"))
        return lane

    def _tick(self, ev: _Ev, x: float, width: float, label: str) -> _Item:
        def draw(y: float) -> str:
            inner = self.hit(ev, x - 2, y + 1, max(width, 6), 12)
            inner += (
                f'<line class="fw-tick" x1="{_n(x)}" x2="{_n(x)}" '
                f'y1="{_n(y + 2)}" y2="{_n(y + 12)}"/>'
            )
            if label:
                inner += self.text(x + 4, y + 11, label, "t t3")
            return self.a(ev, inner)

        return _Item(x - 2, x + width, draw)

    def rest(self, lane: _Lane) -> None:
        """Every event with a time that no lane has drawn, so that none is left out."""
        harness = _Sub("harness", 20)
        other = _Sub("other events", 20)
        for ev in self.run.evs:
            if ev.t is None or ev.i in self.placed:
                continue
            if ev.kind == "user.hangup":
                continue  # drawn across every lane
            elif ev.kind == "history.final":
                label, sub = "history.final: the chat history, read back", harness
            elif ev.source == "harness":
                label, sub = _clip(ev.kind, 32), harness
            else:
                label, sub = _clip(ev.kind, 32), other
            sub.items.append(self.flag(ev, label, "", "c-fw"))
        lane.subs += [s for s in (harness, other) if s.items]

    def verdicts(self) -> _Lane:
        report = self.report
        lane = _Lane(
            "verdicts",
            "Verdicts",
            "One row per contract. Each mark sits at the latest event its verdict rests on.",
        )
        if report is None:
            lane.subs.append(_Sub("", 20, empty="the contracts were not checked"))
            return lane
        if not report.valid:
            lane.subs.append(
                _Sub("", 20, empty="no verdicts: the run is not valid evidence (see the top)")
            )
            return lane
        for name, result in report.results.items():
            sub = _Sub(
                f'<a href="#c-{_e(name)}"><b>{_e(name)}</b></a> {_badge(result.outcome)}',
                20,
                hint=" ".join(CONTRACT_TITLES.get(name, ("", ""))),
            )
            if result.outcome == NOT_EXERCISED:
                sub.empty = "not exercised: this run has no instance of it"
            for v in result.verdicts:
                group = self.sel.of_verdict[id(v)]
                times = [ev.t for ev in (self.run.evs[s] for s in group.events) if ev.t is not None]
                x = self.x(max(times)) if times else PAD_L
                label = _clip(_instance(v.instance), 40)
                if v.flags:
                    label += f" [{_plural(len(v.flags), 'flag')}]"
                sub.items.append(self._verdict_mark(v, group, x, label))
            lane.subs.append(sub)
        return lane

    def _verdict_mark(self, v: Verdict, group: _Group, x: float, label: str) -> _Item:
        width = 20 + _w(label)
        title = f"{v.contract} {OUTCOME_WORDS.get(v.outcome, v.outcome)}: {v.summary}"

        def draw(y: float) -> str:
            return (
                f'<a class="m" href="#{group.anchor}"><title>{_e(title)}</title>'
                f'<rect class="hit {group.anchor}" x="{_n(x - 3)}" y="{_n(y + 1)}" '
                f'width="{_n(width + 5)}" height="18" rx="3"/>'
                f'<line class="pole" x1="{_n(x)}" x2="{_n(x)}" y1="{_n(y + 2)}" y2="{_n(y + 18)}"/>'
                f'<g transform="translate({_n(x + 4)} {_n(y + 4)})">{_status_shape(v.outcome)}</g>'
                f"{self.text(x + 20, y + 14, label)}</a>"
            )

        return _Item(x - 3, x + width + 2, draw)

    def build(self) -> str:
        fw = self.framework()
        lanes = [self.user(), self.model(), self.tools(), self.voice(), fw]
        verdicts = self.verdicts()
        # Draw every lane once, so that what is left over is known before the rows are laid out.
        for lane in lanes:
            for sub in lane.subs:
                for item in sub.items:
                    item.draw(0)
        self.rest(fw)
        lanes.append(verdicts)

        labels: list[str] = []
        body: list[str] = []
        bands: list[tuple[float, str]] = []
        y = AXIS_H
        right = self.x(self.run.base + self.hi_s * 1_000_000)
        for lane in lanes:
            bands.append((y, lane.key))
            labels.append(
                f'<div class="ll lt" style="top:{_n(y)}px" title="{_e(lane.note)}">'
                f"{_e(lane.title)}</div>"
            )
            body.append(self.text(PAD_L, y + 14, lane.note, "t t3 note"))
            y += BAND_H + 3
            for sub in lane.subs:
                rows = [sub.items] if sub.single_row else _pack(sub.items)
                if sub.label:
                    labels.append(
                        f'<div class="ll" style="top:{_n(y + sub.label_dy)}px" '
                        f'title="{_e(sub.hint)}">{sub.label}</div>'
                    )
                if not rows or not sub.items:
                    if sub.empty:
                        body.append(self.text(PAD_L, y + sub.label_dy + 14, sub.empty, "t t3"))
                    y += sub.row_h
                    continue
                for row in rows:
                    for item in row:
                        body.append(item.draw(y))
                        right = max(right, item.x1)
                    y += sub.row_h
            y += 7
        height = y + AXIS_H - 6
        hangup_line, hangup_label, hangup_right = self._hangup(height)
        width = max(right, hangup_right) + PAD_R

        out = [
            (
                f'<svg class="tl-svg" width="{_n(width)}" height="{_n(height)}" '
                f'viewBox="0 0 {_n(width)} {_n(height)}" role="group" '
                f'aria-label="Timeline of the run, in seconds from the user origin">'
            ),
            _DEFS,
        ]
        for top, key in bands:
            out.append(
                f'<rect class="band band-{key}" x="0" y="{_n(top)}" width="{_n(width)}" '
                f'height="{_n(BAND_H)}"/>'
            )
        out.append(self._axis(width, height))
        for index in sorted(self.placed):
            gx = self.placed[index]
            out.append(
                f'<line class="gl {self.sel.classes(index)}" x1="{_n(gx)}" x2="{_n(gx)}" '
                f'y1="{_n(AXIS_H)}" y2="{_n(height - AXIS_H + 6)}"/>'
            )
        out.append(hangup_line)
        out += body
        out.append(hangup_label)
        out.append("</svg>")
        return (
            f'<div class="tl-scroll" tabindex="0"><div class="tl" style="width:'
            f'{_n(LABEL_W + width)}px;--print-zoom:{min(1.0, 1040 / (LABEL_W + width)):.3f}">'
            f'<div class="tl-labels" style="width:{LABEL_W}px;height:{_n(height)}px">'
            f"{''.join(labels)}</div>{''.join(out)}</div></div>"
        )

    def _axis(self, width: float, height: float) -> str:
        out = ['<g class="axis">']
        step = 1
        while step * self.scale < 44:
            step = step * 5 if str(step).startswith("1") else step * 2
        bottom = height - AXIS_H + 6
        labelled = range(-(-self.lo_s // step) * step, self.hi_s + 1, step)
        between = range(self.lo_s, self.hi_s + 1) if step > 1 and self.scale >= 20 else range(0)
        for s in sorted({*labelled, *between}):
            x = self.x(self.run.base + s * 1_000_000)
            major = s % step == 0
            out.append(
                f'<line class="{"grid" if major else "grid-minor"}" x1="{_n(x)}" x2="{_n(x)}" '
                f'y1="{_n(AXIS_H - (6 if major else 3))}" y2="{_n(bottom + (6 if major else 3))}"/>'
            )
            if major:
                label = f"{s} s" if s == 0 or s == labelled[0] else str(s)
                for ty in (AXIS_H - 10, bottom + 17):
                    out.append(self.text(x, ty, label, "t ax", ' text-anchor="middle"'))
            if self.scale >= 80 and s < self.hi_s:
                for tenth in range(1, 10):
                    tx = x + tenth * self.scale / 10
                    long = 5 if tenth == 5 else 2.5
                    out.append(
                        f'<path class="grid-minor" d="M{_n(tx)} {_n(AXIS_H)}v{_n(-long)}'
                        f'M{_n(tx)} {_n(bottom)}v{_n(long)}"/>'
                    )
        out.append("</g>")
        return "".join(out)

    def _hangup(self, height: float) -> tuple[str, str, float]:
        run = self.run
        declared = _int(run.scenario, "hangup_us")
        ev = run.hangup
        bottom = height - AXIS_H + 6
        if ev is not None and ev.t is not None:
            x = self.x(ev.t)
            label = f"user hangs up, {self.at(ev.t)}"
            line = f'<line class="hangup" x1="{_n(x)}" x2="{_n(x)}" y1="3" y2="{_n(bottom)}"/>'
            inner = self.hit(ev, x - 3, 1, _w(label) + 12, 15)
            inner += (
                f'<rect class="hangup-tab" x="{_n(x)}" y="2" width="{_n(_w(label) + 8)}" '
                f'height="13"/>{self.text(x + 4, 12, label, "t hangup-t")}'
            )
            tab = self.a(ev, inner, "Nothing after this instant reached the user")
            return line, tab, x + _w(label) + 8
        if declared is not None and run.origin is not None:
            x = self.x(run.base + declared)
            label = f"hangup declared at {_sec(declared)}, not recorded"
            line = (
                f'<line class="hangup hangup-open" x1="{_n(x)}" x2="{_n(x)}" y1="3" '
                f'y2="{_n(bottom)}"/>'
            )
            return line, self.text(x + 4, 12, label, "t t2"), x + _w(label) + 4
        return "", "", 0.0


def _lower(draw: Callable[[float], str], by: float) -> Callable[[float], str]:
    return lambda y: draw(y + by)


def _fw_label(data: dict[str, Any]) -> str:
    if isinstance(data.get("new"), str):
        return str(data["new"])
    if isinstance(data.get("text"), str):
        final = " (final)" if data.get("final") is True else ""
        return f'"{data["text"]}"{final}'
    item = data.get("item")
    if isinstance(item, dict):
        role = item.get("role")
        return str(role if isinstance(role, str) else item.get("type", ""))
    for key in ("reason", "source"):
        if isinstance(data.get(key), str):
            return str(data[key])
    return ""


_DEFS = (
    "<defs>"
    '<pattern id="p-wait" width="5" height="5" patternUnits="userSpaceOnUse" '
    'patternTransform="rotate(45)"><line class="p-model" x1="0" y1="0" x2="0" y2="5"/></pattern>'
    '<pattern id="p-voice" width="5" height="5" patternUnits="userSpaceOnUse" '
    'patternTransform="rotate(45)"><line class="p-voice" x1="0" y1="0" x2="0" y2="5"/></pattern>'
    '<pattern id="p-paused" width="6" height="6" patternUnits="userSpaceOnUse" '
    'patternTransform="rotate(-45)"><line class="p-paused" x1="0" y1="0" x2="0" y2="6"/></pattern>'
    '<pattern id="p-cut" width="4" height="4" patternUnits="userSpaceOnUse" '
    'patternTransform="rotate(45)"><line class="p-cut" x1="0" y1="0" x2="0" y2="4"/></pattern>'
    "</defs>"
)


def _overall(report: Report | None, error: str) -> tuple[str, str, str]:
    if report is None:
        return "unchecked", "Not checked", f"The contracts could not be checked: {error}"
    if not report.valid:
        return (
            "invalid",
            "Invalid run",
            "This run is not valid evidence. No contract was checked, so there are no verdicts.",
        )
    by: dict[str, list[str]] = {}
    for name, result in report.results.items():
        by.setdefault(result.outcome, []).append(name)
    parts = []
    for outcome, phrase in (
        (FAIL, "failed"),
        (UNOBSERVABLE, "could not be decided (unobservable)"),
        (PASS, "passed"),
        (NOT_EXERCISED, "had no instance in this run (not exercised)"),
    ):
        if by.get(outcome):
            parts.append(f"{', '.join(by[outcome])} {phrase}.")
    sentence = " ".join(parts)
    if by.get(FAIL):
        return FAIL, "Failed", sentence
    if by.get(UNOBSERVABLE):
        return UNOBSERVABLE, "Unobservable", sentence
    if by.get(PASS):
        return PASS, "Passed", sentence
    return NOT_EXERCISED, "Not exercised", sentence


def _header(run: _Run, report: Report | None, error: str, title: str, digest: str) -> str:
    header = run.header
    framework = _dict(header, "framework")
    outcome, word, sentence = _overall(report, error)
    run_id = next((_str(ev.raw, "run") for ev in run.evs if _str(ev.raw, "run")), "")
    if run.origin is not None and run.origin.t is not None:
        origin = (
            f"user.origin at t_us {run.origin.t} (seq {run.origin.seq}). "
            "Axis time is t_us minus that."
        )
    else:
        origin = "No user.origin in this ledger. Axis time is virtual time."
    facts = [
        ("case", _str(header, "case") or "not stated"),
        ("seed", header.get("seed", "not stated")),
        (
            "framework",
            f"{_str(framework, 'name') or 'not stated'} {_str(framework, 'version')}".strip(),
        ),
        ("clock", _str(header, "clock") or "not stated"),
        ("run id", run_id or "not stated"),
        ("ledger hash", digest),
        ("events", f"{len(run.evs)}, over {_sec(run.t_hi - run.t_lo)}"),
        ("origin", origin),
    ]
    rows = "".join(f"<div><dt>{_e(k)}</dt><dd>{_e(v)}</dd></div>" for k, v in facts)
    chips = ""
    if report is not None and report.valid:
        chips = '<p class="chips">' + "".join(
            f'<a class="chip" href="#c-{_e(name)}"><b>{_e(name)}</b> {_badge(result.outcome)}</a>'
            for name, result in report.results.items()
        )
        chips += "</p>"
    reading = ""
    if report is not None and report.readings > 1:
        taken = ", ".join(f"utterance {u} as call {c}" for u, c in sorted(report.reading.items()))
        reading = (
            f'<p class="reading">The ledger allows {report.readings} readings of which reply '
            f"a text was. The verdicts are those of the reading most favourable to the system"
            f"{': ' + _e(taken) if taken else ''}.</p>"
        )
    return (
        f'<header class="top"><p class="kind">Earful run report</p><h1>{_e(title)}</h1>'
        f'<div class="result r-{outcome}">{_badge(outcome, word)}<p>{_e(sentence)}</p></div>'
        f'{chips}{reading}<dl class="facts">{rows}</dl>'
        '<p class="scope"><b>What this is evidence of.</b> One run of the framework on a '
        "virtual clock, against scripted providers for the user, the model and the voice, "
        "a simulated audio sink and a fake backend. Playout means audio the sink consumed. "
        "Nothing here measures a network, a client buffer or what a caller heard.</p>"
        "</header>"
    )


def _invalid(report: Report | None, error: str) -> str:
    if report is None:
        return (
            f'<section class="invalid" id="invalid"><h2>{_badge("unchecked", "Not checked")}'
            f"</h2><p>The header declares a contract policy the checker refuses, so no "
            f"contract was checked. The timeline below shows what the ledger holds.</p>"
            f"<ul><li>{_e(error)}</li></ul></section>"
        )
    if report.valid:
        return ""
    reasons = "".join(f"<li>{_e(reason)}</li>" for reason in report.invalid)
    return (
        f'<section class="invalid" id="invalid"><h2>{_badge("invalid", "Invalid run")} '
        f"{_plural(len(report.invalid), 'reason')}</h2>"
        "<p>An invalid run yields no verdict for any contract. It did not pass and it did "
        "not fail. The timeline below shows what the ledger holds.</p>"
        f"<ul>{reasons}</ul></section>"
    )


def _bundle(manifest: dict[str, Any], digest: str) -> str:
    predicate = _dict(manifest, "predicate")
    framework = _dict(manifest, "framework")
    which = "neither recorded ledger"
    for name, words in (("minimal", "the reduced case"), ("original", "the case as found")):
        if _str(_dict(manifest, name), "hash") == digest:
            which = f"{words} ({name})"
    known = which != "neither recorded ledger"
    out = [
        '<section class="bundle" id="bundle"><h2>From a failure bundle</h2>',
        (
            f"<p>The bundle froze <b>{_e(_str(predicate, 'contract', '?'))}/"
            f"{_e(_str(predicate, 'rule', '?'))}</b> on "
            f"{_e(_str(framework, 'name', '?'))} {_e(_str(framework, 'version'))}, "
            f"{_e(_str(manifest, 'clock', '?'))} clock. "
        ),
        (
            f"This ledger is {_e(which)}: its hash matches the manifest.</p>"
            if known
            else "This ledger's hash matches neither ledger the manifest records.</p>"
        ),
        (
            '<table class="plain"><thead><tr><th></th><th>speeches</th><th>delays</th>'
            "<th>faults</th><th>what the checker found for the frozen rule</th></tr></thead>"
            "<tbody>"
        ),
    ]
    for name, words in (("original", "as found"), ("minimal", "reduced")):
        part = _dict(manifest, name)
        findings = "<br>".join(_e(f) for f in _list(part, "findings")) or "none recorded"
        out.append(
            f"<tr><th>{words}</th><td>{_e(part.get('speeches', '?'))}</td>"
            f"<td>{_e(part.get('delays', '?'))}</td><td>{_e(part.get('faults', '?'))}</td>"
            f"<td>{findings}</td></tr>"
        )
    out.append("</tbody></table>")
    reduction = _dict(manifest, "reduction")
    if reduction:
        complete = (
            "The reduction finished."
            if reduction.get("complete") is True
            else "The reduction stopped when its run budget was spent."
        )
        steps = "".join(f"<li>{_e(step)}</li>" for step in _list(reduction, "steps"))
        differences = _list(reduction, "differences")
        differs = (
            "".join(f"<li>{_e(_one_line(_json(d)))}</li>" for d in differences)
            or "<li>none: the author's scenario fails as written, or none was given</li>"
        )
        out.append(
            f"<p><b>Reduction.</b> {_e(reduction.get('runs', '?'))} runs. {complete}</p>"
            f"<ul>{steps}</ul><p>Differences from the author's scenario:</p><ul>{differs}</ul>"
        )
    windows = _list(manifest, "windows")
    if windows:
        out.append(
            "<p><b>Windows.</b> How far each time of the reduced case can move while the "
            "rule still breaks.</p>"
            '<table class="plain"><thead><tr><th>variable</th><th>from</th><th>to</th>'
            "<th>width</th><th>below</th><th>above</th><th>located to</th><th></th></tr>"
            "</thead><tbody>"
        )
        for w in windows:
            cells = [
                _e(_str(w, "variable", "?")),
                *(
                    _e(_sec(value)) if value is not None else "?"
                    for value in (_int(w, "lo_us"), _int(w, "hi_us"), _int(w, "width_us"))
                ),
                _e(_str(w, "lo_end", "?")),
                _e(_str(w, "hi_end", "?")),
                _e(_sec(_int(w, "resolution_us") or 0)),
                "an instant: it says nothing about the system"
                if isinstance(w, dict) and w.get("instant") is True
                else "",
            ]
            out.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
        out.append("</tbody></table>")
    replay = _dict(manifest, "replay")
    environment = _dict(manifest, "environment")
    made = ", ".join(f"{_e(k)} {_e(_clip(v, 20))}" for k, v in sorted(environment.items()))
    if replay or made:
        out.append(
            f'<p class="small">Run {_e(replay.get("repeats", "?"))} times when the bundle '
            f"was made, giving {_e(replay.get('distinct_hashes', '?'))} distinct ledger. "
            f"Made with: {made or 'not stated'}.</p>"
        )
    out.append("</section>")
    return "".join(out)


def _seq_links(run: _Run, indexes: list[int]) -> str:
    return " ".join(
        f'<a class="seq" href="#t{i}" title="{_e(run.evs[i].kind)}">{_e(run.evs[i].seq)}</a>'
        for i in indexes
    )


def _selection_bars(run: _Run, selections: _Selections, timeline: _Timeline) -> str:
    out = []
    clear = '<a class="clear" href="#timeline">clear</a>'
    for group in selections.groups:
        times = [run.evs[i].t for i in group.events]
        known = [t for t in times if t is not None]
        when = ""
        if known and min(known) == max(known):
            when = f", at {_e(timeline.at(known[0]))}"
        elif known:
            when = f", {_e(timeline.at(min(known)))} to {_e(timeline.at(max(known)))}"
        out.append(
            f'<div class="sel" id="{group.anchor}" data-k="{group.anchor}"><p>{group.title}</p>'
            f'<p class="small">Rests on {_plural(len(group.events), "event")}{when}: '
            f"{_seq_links(run, group.events)} "
            f'<a href="#{group.detail}">verdict</a> {clear}</p></div>'
        )
    for ev in run.evs:
        used = "".join(
            f' <a href="#{g.anchor}">{g.short}</a>' for g in selections.member.get(ev.i, [])
        )
        out.append(
            f'<div class="sel" id="t{ev.i}" data-k="s{ev.i}"><p><b>{_e(ev.kind)}</b>, '
            f"seq {_e(ev.seq)}, {_e(ev.source)}, at {_e(timeline.at(ev.t))} "
            f'<code>{_e(_clip(_json(ev.data), 260))}</code></p><p class="small">'
            f'<a href="#e{ev.i}">row in the event table</a>'
            f"{' Used by:' + used if used else ' No verdict rests on it.'} {clear}</p></div>"
        )
    out.append(
        '<div class="sel sel-none"><p>Nothing is highlighted.</p><p class="small">Select a '
        "verdict, a finding or a mark to highlight the events it rests on, on the timeline "
        "and in the event table.</p></div>"
    )
    return "".join(out)


def _selection_css(run: _Run, selections: _Selections) -> str:
    targets = [f"#{g.anchor}:target~section .{g.anchor}" for g in selections.groups]
    targets += [f"#t{ev.i}:target~section .s{ev.i}" for ev in run.evs]
    if not targets:
        return ""
    return (
        ",".join(targets)
        + "{fill:var(--hl-bg);stroke:var(--hl);background:var(--hl-bg);visibility:visible}"
    )


def _verdict(run: _Run, v: Verdict, selections: _Selections) -> str:
    group = selections.of_verdict[id(v)]
    number = selections.number[id(v)]
    findings = ""
    for f in v.findings:
        found: _Group | None = selections.of_finding.get(id(f))
        link = (
            f' <a class="show" href="#{found.anchor}">highlight '
            f"{_plural(len(found.events), 'event')}</a> {_seq_links(run, found.events)}"
            if found is not None
            else ""
        )
        findings += f"<li><b>{_e(f.rule)}</b>: {_e(f.detail)}{link}</li>"
    flags = "".join(
        f'<li><code class="flag">{_e(flag)}</code> '
        f"{_e(FLAGS.get(flag, 'A qualification the checker attached to this verdict.'))}</li>"
        for flag in v.flags
    )
    measures = (
        f"<details><summary>measures</summary><pre>{_e(_json(v.measures, indent=1))}</pre>"
        "</details>"
        if v.measures
        else ""
    )
    evidence = (
        f'<p class="small"><a class="show" href="#{group.anchor}">Highlight the '
        f"{_plural(len(group.events), 'event')} this verdict rests on</a> "
        f"{_seq_links(run, group.events)}"
        f"{' Witnesses: ' + _e(', '.join(v.witnesses)) + '.' if v.witnesses else ''}</p>"
        if group.events
        else '<p class="small">This verdict names no event.</p>'
    )
    return (
        f'<div class="verdict o-{_e(v.outcome)} {group.anchor}" id="d{number}">'
        f"<p>{_badge(v.outcome)} <b>{_e(_instance(v.instance))}</b>: {_e(v.summary)}</p>"
        f"{'<ul class=findings>' + findings + '</ul>' if findings else ''}"
        f"{'<ul class=flags>' + flags + '</ul>' if flags else ''}"
        f"{evidence}{measures}</div>"
    )


def _verdicts(run: _Run, report: Report | None, selections: _Selections) -> str:
    if report is None or not report.valid:
        return (
            '<section id="verdicts"><h2>Verdicts</h2><p class="none">There are no verdicts. '
            "An invalid or unchecked run has no result for any contract.</p></section>"
        )
    out = ['<section id="verdicts"><h2>Verdicts</h2>']
    for name, result in report.results.items():
        title, statement = CONTRACT_TITLES.get(name, ("", ""))
        out.append(
            f'<div class="contract" id="c-{_e(name)}"><h3><span class="cid">{_e(name)}</span> '
            f"{_e(title)} {_badge(result.outcome)}</h3>"
            f'<p class="statement">{_e(statement)}</p>'
        )
        if result.outcome == NOT_EXERCISED:
            out.append(
                '<p class="none">Not exercised: this run has no instance of the contract. '
                "That is not a pass.</p>"
            )
        shown = [v for v in result.verdicts if v.outcome != PASS or v.flags]
        plain = [v for v in result.verdicts if v.outcome == PASS and not v.flags]
        out += [_verdict(run, v, selections) for v in shown]
        if plain:
            passes = "1 plain pass" if len(plain) == 1 else f"{len(plain)} plain passes"
            out.append(f'<details class="plain-pass"><summary>{passes}</summary>')
            out += [_verdict(run, v, selections) for v in plain]
            out.append("</details>")
        out += [f'<p class="note"><b>Note.</b> {_e(note)}</p>' for note in result.notes]
        policy = {k: v for k, v in result.policy.items() if v not in ((), [], None)}
        if policy:
            out.append(
                f'<p class="small policy">Policy: <code>{_e(_one_line(_json(policy)))}</code></p>'
            )
        out.append("</div>")
    out.append("</section>")
    return "".join(out)


def _table(run: _Run, selections: _Selections, timeline: _Timeline) -> str:
    rows = []
    for ev in run.evs:
        data = _json(ev.data) if isinstance(ev.raw, dict) else _json(ev.raw)
        if _cells(data) > 150:
            shown = (
                f"<details><summary><code>{_e(_clip(data, 140))}</code></summary>"
                f"<pre>{_e(data)}</pre></details>"
            )
        else:
            shown = f"<code>{_e(data)}</code>"
        raw_t = f"t_us {ev.t}" if ev.t is not None else "no t_us"
        source = ev.source if ev.source in _SOURCES else "other"
        rows.append(
            f'<tr id="e{ev.i}" class="{selections.classes(ev.i)}" data-src="{_e(ev.source)}">'
            f'<td class="num"><a href="#t{ev.i}" title="highlight on the timeline">{_e(ev.seq)}'
            f'</a></td><td class="num" title="{_e(raw_t)}">{_e(timeline.at(ev.t))}</td>'
            f'<td class="nw"><span class="src src-{source}"></span>{_e(ev.source)}</td>'
            f'<td class="nw"><code>{_e(ev.kind)}</code></td><td>{shown}</td></tr>'
        )
    return (
        '<section id="events"><h2>Events</h2>'
        f'<p class="small">Every line of the ledger, in order. '
        f"Time is in seconds from the origin. "
        f"A seq links to its mark on the timeline.</p>"
        '<div class="ev-tools"></div>'
        '<div class="ev-scroll"><table class="events"><thead><tr><th>seq</th><th>time</th>'
        "<th>source</th><th>kind</th><th>data</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div></section>"
    )


_SOURCES = ("world", "llm", "sink", "backend", "framework", "harness")


def _legend() -> str:
    def swatch(inner: str, width: int = 34) -> str:
        return (
            f'<svg class="tl-svg lg" width="{width}" height="20" viewBox="0 0 {width} 20" '
            f'aria-hidden="true">{inner}</svg>'
        )

    entries = [
        (swatch('<rect class="speech" x="2" y="5" width="30" height="10" rx="2"/>'),
         "user speech, true start to true end"),
        (swatch('<path class="arrow c-model" d="M2 4l8 6l-8 6z"/><rect class="wait-model" x="10" '
                'y="4" width="10" height="12"/><rect class="gen" x="20" y="4" width="12" '
                'height="12" rx="2"/>'),
         "model call: request, wait for the first token, generation"),
        (swatch('<rect class="bar-tool-open bar-tool" x="2" y="6" width="18" height="8"/>'
                f'<g class="c-tool">{_glyph("stop", 22, 5)}</g>'),
         "tool call that returned"),
        (swatch('<rect class="bar-tool-open bar-tool" x="2" y="6" width="18" height="8"/>'
                f'<g class="c-tool">{_glyph("x", 22, 5)}</g>'),
         "tool call that raised"),
        (swatch('<rect class="bar-tool-open" x="2" y="6" width="18" height="8"/>'
                f'<g class="c-tool">{_glyph("open", 22, 5)}</g>'),
         "tool call that was cancelled, or has no outcome"),
        (swatch('<rect class="bar-write-open bar-write" x="2" y="6" width="18" height="8"/>'
                f'<g class="c-write">{_glyph("diamond", 22, 5)}</g>'),
         "write that committed"),
        (swatch(f'<g class="c-write">{_glyph("diamond-o", 4, 5)}{_glyph("x", 20, 5)}</g>'),
         "write deduplicated; write failed"),
        (swatch(f'<line class="pole c-tool" x1="6" x2="6" y1="2" y2="18"/>'
                f'<g class="c-tool">{_glyph("clock", 11, 5)}</g>'),
         "the caller of a write stopped waiting"),
        (swatch('<rect class="wait-voice" x="2" y="5" width="22" height="10"/>'
                '<line class="tick-voice" x1="24" x2="24" y1="2" y2="18"/>'),
         "synthesis: wait for the first audio, then its release"),
        (swatch('<rect class="w" x="2" y="2" width="29" height="16"/>'
                '<line class="done w-done" x1="32" x2="32" y1="1" y2="19"/>'),
         "word played; its right edge is when it completed"),
        (swatch('<rect class="w-cut" x="2" y="2" width="14" height="16"/>'
                f'<g class="c-voice">{_glyph("torn", 20, 5)}</g>'),
         "cut word; end of an interrupted segment"),
        (swatch('<rect class="paused" x="2" y="2" width="30" height="16"/>'),
         "segment paused: not audible"),
        (swatch('<text class="t ghost" x="2" y="14">word</text>'),
         "synthesized, never played"),
        (swatch('<line class="hangup" x1="8" x2="8" y1="1" y2="19"/>'),
         "the user hangs up"),
        (swatch(f'<g transform="translate(2 4)">{_status_shape(PASS)}</g>'
                f'<g transform="translate(18 4)">{_status_shape(FAIL)}</g>'),
         "verdict: pass; fail"),
        (swatch(f'<g transform="translate(2 4)">{_status_shape(UNOBSERVABLE)}</g>'
                f'<g transform="translate(18 4)">{_status_shape(NOT_EXERCISED)}</g>'),
         "unobservable; not exercised"),
    ]  # fmt: skip
    items = "".join(f"<li>{mark}<span>{_e(text)}</span></li>" for mark, text in entries)
    return (
        f'<ul class="legend">{items}</ul>'
        '<p class="small">An arrow points at the instant something starts. A pole stands at '
        "the instant of a single event. Framework marks that share an instant are drawn "
        "side by side, in ledger order. Every mark is a link.</p>"
    )


def _page(
    events: list[Event],
    report: Report | None,
    *,
    title: str | None = None,
    manifest: dict[str, Any] | None = None,
    error: str = "",
) -> str:
    """``report`` is None when the checker refused the header's policy; ``error`` says why."""
    run = _Run(events)
    digest = ledger_hash(events)
    selections = _Selections(run, report)
    is_minimal = manifest is not None and _str(_dict(manifest, "minimal"), "hash") == digest
    timeline = _Timeline(run, report, selections, manifest, is_minimal)
    drawn = timeline.build()
    name = title if title is not None else (_str(run.header, "case") or "Ledger")
    _outcome, word, _sentence = _overall(report, error)
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_e(name)}: {_e(word.lower())} | Earful</title>",
        f"<style>{_CSS}{_selection_css(run, selections)}</style>",
        "</head><body><main>",
        _header(run, report, error, name, digest),
        _invalid(report, error),
        _bundle(manifest, digest) if manifest is not None else "",
        _selection_bars(run, selections, timeline),
        (
            '<section id="timeline"><h2>Timeline</h2>'
            f'<p class="small">Seconds from the user origin, {timeline.scale:.3g} px per second. '
            "The timeline scrolls sideways.</p>"
        ),
        drawn,
        _legend(),
        "</section>",
        _verdicts(run, report, selections),
        _table(run, selections, timeline),
        (
            '<footer><p class="small">Rendered from the ledger alone by earful.core.html. '
            "The same ledger gives the same page.</p></footer>"
        ),
        f"</main><script>{_JS}</script></body></html>",
    ]
    return "\n".join(part for part in parts if part) + "\n"


def render(
    events: list[dict[str, Any]],
    *,
    title: str | None = None,
    manifest: dict[str, Any] | None = None,
) -> str:
    """One complete HTML document for the ledger ``events``, valid or not."""
    report: Report | None
    error = ""
    try:
        report = check(events)
    except PolicyError as exc:
        report, error = None, str(exc)
    return _page(events, report, title=title, manifest=manifest, error=error)


def render_bundle(directory: Path, which: str = "minimal") -> str:
    """The page for one ledger of a bundle; ``ValueError`` if the bundle or ledger is missing."""
    from . import bundle

    if which not in bundle.FILES:
        raise ValueError(f"a bundle has no {which!r} ledger; choose from {sorted(bundle.FILES)}")
    loaded = bundle.load(directory)
    return render(
        loaded.ledger(which), title=f"{directory.name} ({which})", manifest=loaded.manifest
    )


_CSS = """
:root{color-scheme:light dark;--bg:#f4f5f7;--panel:#fff;--ink:#14171c;--ink2:#4c535e;
--ink3:#69707b;--rule:#dcdfe5;--band:#eceef2;--user:#2a78d6;--model:#6250d6;--write:#1baf7a;
--audio:#eb6834;--tool:#56606c;--pass:#0b8a0b;--fail:#cf3636;--unob:#a86400;--on:#fff;
--hl:#d9a400;--hl-bg:#ffeb8a;--wash:.2}
@media (prefers-color-scheme:dark){:root{--bg:#121417;--panel:#1a1c20;--ink:#eceef1;
--ink2:#b3b9c2;--ink3:#868d98;--rule:#30343b;--band:#22252a;--user:#3987e5;--model:#9085e9;
--write:#199e70;--audio:#d95926;--tool:#a0a8b3;--pass:#0ca30c;--fail:#e05757;--unob:#fab219;
--on:#101214;--hl:#ffd43a;--hl-bg:#5c4d00;--wash:.3}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 ui-sans-serif,system-ui,
-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
main{max-width:1500px;margin:0 auto;padding:28px 28px 60px}
code,pre,.num,.t,.ll,.seq,.facts dd,.cid{font-family:ui-monospace,"SF Mono",SFMono-Regular,
Menlo,Consolas,"Liberation Mono",monospace}
h1{font:600 30px/1.2 ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace;
margin:2px 0 14px;letter-spacing:-.01em;overflow-wrap:anywhere}
h2{font-size:19px;margin:0 0 6px}
h3{font-size:15px;margin:0;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
p{margin:0 0 8px}
a{color:inherit;text-decoration-color:var(--ink3);text-underline-offset:2px}
a:hover{text-decoration-color:var(--ink)}
section,header.top{background:var(--panel);border:1px solid var(--rule);border-radius:6px;
padding:18px 20px;margin:0 0 14px}
.kind{color:var(--ink2);font-size:13px;margin:0}
.small{font-size:13px;color:var(--ink2)}
.none{color:var(--ink2)}
.result{display:flex;gap:12px;align-items:baseline;flex-wrap:wrap;margin:0 0 10px}
.result p{margin:0;max-width:78ch}
.result .badge{font-size:16px;font-weight:650;padding:3px 10px 3px 8px}
.badge{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--rule);
border-radius:4px;padding:0 7px 0 5px;font-size:12.5px;line-height:20px;white-space:nowrap;
background:var(--panel)}
.badge .sg{flex:none}
.b-fail{border-color:var(--fail)}
.b-pass{border-color:var(--pass)}
.b-unobservable{border-color:var(--unob);border-style:dashed}
.b-not_exercised{border-style:dotted;border-color:var(--ink3);color:var(--ink2)}
.b-invalid,.b-unchecked{border-color:var(--ink);border-style:double;border-width:3px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 12px}
.chip{display:inline-flex;gap:6px;align-items:center;text-decoration:none;font-size:13px}
.chip b{font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
.facts{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:2px 28px;
margin:0 0 12px;font-size:13px}
.facts div{display:flex;gap:10px;border-top:1px solid var(--rule);padding:3px 0}
.facts dt{flex:none;width:86px;color:var(--ink2)}
.facts dd{margin:0;overflow-wrap:anywhere;font-size:12.5px}
.scope{font-size:13.5px;max-width:100ch;margin:0;color:var(--ink2)}
.scope b{color:var(--ink)}
.reading{font-size:13.5px;border-left:3px solid var(--unob);padding-left:10px}
.invalid{border:2px dashed var(--ink);background:repeating-linear-gradient(135deg,transparent
0 14px,color-mix(in srgb,var(--unob) 9%,transparent) 14px 28px),var(--panel)}
.invalid h2{display:flex;gap:10px;align-items:center}
.invalid ul{margin:6px 0 0;padding-left:20px;font:13px/1.5 ui-monospace,"SF Mono",Menlo,
Consolas,monospace;overflow-wrap:anywhere}
.sel{display:none;background:var(--hl-bg);border:1px solid var(--hl);border-radius:6px;
padding:8px 14px;margin:0 0 14px;min-height:60px;font-size:13.5px;
scroll-margin-top:0}
.sel p{margin:0 0 2px}
.sel:target .small{color:var(--ink)}
.sel code{font-size:12px;overflow-wrap:anywhere}
.sel:target{display:block;position:sticky;top:0;z-index:6}
.sel-none{display:block;background:var(--panel);border:1px dashed var(--rule);
color:var(--ink2)}
.sel:target~.sel-none{display:none}
.clear{margin-left:8px}
.seq{display:inline-block;min-width:20px;text-align:center;font-size:12px;padding:0 4px;
border:1px solid var(--ink3);border-radius:3px;text-decoration:none;background:var(--panel)}
.seq:hover{border-color:var(--ink)}
#timeline{padding-bottom:12px}
.tl-scroll{overflow-x:auto;border:1px solid var(--rule);border-radius:4px;margin:10px 0 12px;
background:var(--panel)}
.tl-scroll:focus-visible{outline:2px solid var(--user);outline-offset:2px}
.tl{display:flex;position:relative}
.tl-labels{position:sticky;left:0;z-index:3;flex:none;background:var(--panel);
border-right:1px solid var(--rule)}
.ll{position:absolute;left:0;right:0;height:20px;padding:0 8px 0 12px;font-size:10.5px;
line-height:20px;
color:var(--ink2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:flex;
align-items:center;gap:6px}
.ll.lt{font:650 12.5px/20px ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
color:var(--ink);height:20px;background:var(--band)}
.ll a{text-decoration:none}
.ll .badge{font-size:10.5px;line-height:15px;padding:0 5px 0 3px;gap:4px;
font-family:ui-sans-serif,system-ui,sans-serif}
.tl-svg{display:block;flex:none}
.tl-svg text{font:10px ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,
"Liberation Mono",monospace;fill:var(--ink)}
.tl-svg .t2{fill:var(--ink2)}
.tl-svg .t3,.tl-svg .ax{fill:var(--ink3)}
.tl-svg .note{font-family:ui-sans-serif,system-ui,sans-serif;font-size:10.5px}
.tl-svg .ghost{fill:var(--ink3);text-decoration:line-through}
.tl-svg .wt{fill:var(--ink)}
.band{fill:var(--band)}
.grid{stroke:var(--rule);stroke-width:1}
.grid-minor{stroke:var(--rule);stroke-width:1;fill:none}
.m{text-decoration:none;cursor:pointer}
.hit{fill:transparent;stroke:transparent;stroke-width:1.5}
.m:hover .hit{fill:color-mix(in srgb,var(--hl) 22%,transparent)}
.m:focus-visible{outline:none}
.m:focus-visible .hit{stroke:var(--ink)}
.gl{stroke:var(--hl);stroke-width:1.5;visibility:hidden;fill:none}
.tl-svg{color:var(--ink2)}
.pole{stroke:currentColor;stroke-width:1.5}
.arrow,.gf{fill:currentColor}
.gs{fill:none;stroke:currentColor;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}
.c-user{color:var(--user)}
.c-model{color:var(--model)}
.c-tool{color:var(--tool)}
.c-write{color:var(--write)}
.c-voice{color:var(--audio)}
.c-fw{color:var(--ink3)}
.speech{fill:var(--user)}
.window{stroke:var(--user);stroke-width:1.5;fill:none}
.wait-model{fill:url(#p-wait);stroke:var(--model);stroke-width:1}
.p-model{stroke:var(--model);stroke-width:1.6}
.gen{fill:var(--model)}
.tick-model{stroke:var(--model);stroke-width:2}
.bar-tool-open{fill:none;stroke:var(--tool);stroke-width:1.2;stroke-dasharray:3 2}
.bar-tool{fill:var(--tool);stroke-dasharray:none}
.bar-write-open{fill:none;stroke:var(--write);stroke-width:1.2;stroke-dasharray:3 2}
.bar-write{fill:var(--write);stroke-dasharray:none}
.wait-voice{fill:url(#p-voice);stroke:var(--audio);stroke-width:1}
.p-voice{stroke:var(--audio);stroke-width:1.6}
.tick-voice{stroke:var(--audio);stroke-width:2}
.line-voice{stroke:var(--audio);stroke-width:1.5}
.seg{fill:none;stroke:var(--audio);stroke-width:1}
.seg-cut{stroke-dasharray:4 3}
.seg-none{fill:none;stroke:var(--ink3);stroke-dasharray:3 2}
.seg-end{fill:var(--audio)}
.w{fill:var(--audio);fill-opacity:var(--wash)}
.w-late{fill:none;stroke:var(--ink3);stroke-dasharray:2 2}
.done{stroke:var(--audio);stroke-width:1.6}
.w-late-done{stroke:var(--ink3)}
.w-cut{fill:url(#p-cut);stroke:var(--fail);stroke-width:1}
.p-cut{stroke:var(--fail);stroke-width:1.6}
.paused{fill:url(#p-paused)}
.p-paused{stroke:var(--ink3);stroke-width:1.4}
.fw-tick{stroke:var(--ink3);stroke-width:1.5}
.hangup{stroke:var(--ink);stroke-width:2;stroke-dasharray:7 3}
.hangup-open{stroke:var(--ink3)}
.hangup-tab{fill:var(--ink)}
.tl-svg .hangup-t{fill:var(--panel)}
.st-pass{fill:var(--pass)}
.st-fail{fill:var(--fail)}
.st-on{fill:none;stroke:var(--on);stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}
.st-unob{fill:none;stroke:var(--unob);stroke-width:1.5;stroke-dasharray:2.6 1.6}
.st-unob-in{fill:none;stroke:var(--unob);stroke-width:1.5;stroke-linecap:round}
.st-none{fill:none;stroke:var(--ink3);stroke-width:1.4;stroke-dasharray:1.5 1.8}
path.st-none{stroke-dasharray:none}
.st-inv{fill:none;stroke:var(--ink);stroke-width:1.5;stroke-linejoin:round;stroke-linecap:round}
.legend{list-style:none;margin:0 0 8px;padding:0;display:grid;
grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:0 20px;font-size:12.5px;
color:var(--ink2)}
.legend li{display:flex;align-items:center;gap:8px;min-height:22px}
.lg{flex:none}
.contract{border-top:1px solid var(--rule);padding:12px 0 6px}
.cid{font-weight:700}
.statement{color:var(--ink2);font-size:13.5px;margin:2px 0 8px}
.verdict{border-left:3px solid var(--rule);padding:6px 0 4px 12px;margin:0 0 8px;
scroll-margin-top:90px}
.o-fail{border-left-color:var(--fail)}
.o-unobservable{border-left:3px dashed var(--unob)}
.o-pass{border-left-color:var(--pass)}
.verdict ul{margin:2px 0 6px;padding-left:20px;font-size:13.5px}
.flag{border:1px solid var(--ink3);border-radius:3px;padding:0 4px;font-size:12px}
.show{font-weight:600}
.note{font-size:13.5px;border-left:3px dotted var(--ink3);padding-left:12px}
.policy code{font-size:12px;overflow-wrap:anywhere}
details summary{cursor:pointer;color:var(--ink2);font-size:13px}
.plain-pass>summary{margin:0 0 6px}
pre{margin:6px 0;padding:8px 10px;background:var(--bg);border-radius:4px;font-size:12px;
white-space:pre-wrap;overflow-wrap:anywhere}
table{border-collapse:collapse;width:100%;font-size:13px}
th{text-align:left;font-weight:600;color:var(--ink2);font-size:12px}
th,td{padding:4px 10px 4px 0;border-top:1px solid var(--rule);vertical-align:top}
.plain{margin:6px 0 12px;width:auto}
.plain td,.plain th{padding-right:22px}
.ev-scroll{overflow-x:auto}
.events td{padding:3px 10px;font-size:12.5px}
.events th{padding:3px 10px;position:sticky;top:0;background:var(--panel)}
.events code{font-size:12px;overflow-wrap:anywhere}
.events tr{scroll-margin-top:100px}
.events tr:target{background:var(--hl-bg);outline:1px solid var(--hl)}
.num{text-align:right;white-space:nowrap;font-size:12px}
.nw{white-space:nowrap}
.src{display:inline-block;width:8px;height:8px;margin-right:6px;background:var(--ink3)}
.src-world{background:var(--user);border-radius:50%}
.src-llm{background:var(--model);transform:rotate(45deg)}
.src-sink{background:var(--audio)}
.src-backend{background:var(--write);border-radius:50% 50% 0 0}
.src-framework{background:none;border:1.5px solid var(--ink3)}
.src-harness{background:none;border:1.5px solid var(--ink3);border-radius:50%}
.ev-tools{display:none;gap:14px;align-items:center;flex-wrap:wrap;margin:8px 0;font-size:13px}
.js .ev-tools{display:flex}
.ev-tools input[type=search]{font:inherit;padding:3px 8px;border:1px solid var(--ink3);
border-radius:4px;background:var(--panel);color:var(--ink);width:220px}
.card{position:fixed;z-index:9;max-width:440px;background:var(--panel);color:var(--ink);
border:1px solid var(--ink3);border-radius:5px;padding:8px 10px;font-size:12px;
box-shadow:0 4px 18px rgba(0,0,0,.18);pointer-events:none}
.card b{display:block;font-size:12.5px}
.card code{display:block;margin-top:4px;max-height:150px;overflow:hidden;
overflow-wrap:anywhere;color:var(--ink2)}
footer{color:var(--ink2);padding:0 4px}
@media (max-width:700px){main{padding:12px}section,header.top{padding:14px}}
@media print{:root{--bg:#fff;--panel:#fff;--ink:#000;--ink2:#333;--ink3:#666;--rule:#bbb;
--band:#eee;--user:#2a78d6;--model:#6250d6;--write:#1baf7a;--audio:#eb6834;--tool:#56606c;
--pass:#0b8a0b;--fail:#cf3636;--unob:#a86400;--on:#fff;--hl:#d9a400;--hl-bg:#ffeb8a;--wash:.2}
@page{size:landscape;margin:12mm}
body{font-size:12px}main{max-width:none;padding:0}
section,header.top{border:0;padding:0 0 10px;break-inside:auto}
.sel,.ev-tools,.card{display:none!important}
.tl-scroll{overflow:visible;border:0}
.tl{zoom:var(--print-zoom,.5)}
.verdict,.contract,tr{break-inside:avoid}
details>summary{list-style:none}}
"""

_JS = """
(function(){
var d=document,sc=d.querySelector('.tl-scroll');
if(!sc)return;
d.documentElement.classList.add('js');
var labels=sc.querySelector('.tl-labels');
function reveal(){
 var el=d.getElementById(location.hash.slice(1));
 if(!el||!el.classList.contains('sel'))return;
 var k=el.getAttribute('data-k'),hit=k&&sc.querySelector('.hit.'+CSS.escape(k));
 if(!hit)return;
 var b=hit.getBoundingClientRect(),s=sc.getBoundingClientRect(),left=s.left+labels.offsetWidth;
 if(b.left<left+10||b.right>s.right-10)sc.scrollLeft+=b.left-left-140;
}
window.addEventListener('hashchange',reveal);reveal();
sc.addEventListener('click',function(ev){
 var a=ev.target.closest&&ev.target.closest('a.m');
 if(!a||ev.metaKey||ev.ctrlKey||ev.shiftKey)return;
 ev.preventDefault();
 var x=window.scrollX,y=window.scrollY;
 location.hash=a.getAttribute('href');
 window.scrollTo(x,y);
});
var card=d.createElement('div');card.className='card';card.hidden=true;d.body.appendChild(card);
function show(a,ev){
 var row=d.getElementById('e'+a.getAttribute('data-i'));
 if(!row)return;
 var c=row.cells,h=d.createElement('b'),p=d.createElement('span'),j=d.createElement('code');
 h.textContent=c[3].textContent;
 p.textContent='seq '+c[0].textContent+', '+c[2].textContent+', at '+c[1].textContent;
 var pre=c[4].querySelector('pre');
 j.textContent=(pre||c[4]).textContent.slice(0,700);
 card.textContent='';card.appendChild(h);card.appendChild(p);card.appendChild(j);
 card.hidden=false;place(ev);
}
function place(ev){
 var w=card.offsetWidth,h=card.offsetHeight,x=ev.clientX+14,y=ev.clientY+16;
 if(x+w>innerWidth-8)x=Math.max(8,ev.clientX-w-14);
 if(y+h>innerHeight-8)y=Math.max(8,ev.clientY-h-16);
 card.style.left=x+'px';card.style.top=y+'px';
}
sc.addEventListener('mouseover',function(ev){
 var a=ev.target.closest&&ev.target.closest('a.m[data-i]');
 if(a)show(a,ev);else card.hidden=true;
});
sc.addEventListener('mousemove',function(ev){if(!card.hidden)place(ev);});
sc.addEventListener('mouseleave',function(){card.hidden=true;});
var tools=d.querySelector('.ev-tools'),rows=d.querySelectorAll('.events tbody tr');
if(tools&&rows.length){
 var seen=[],off=Object.create(null),q=d.createElement('input');
 q.type='search';q.placeholder='Filter events';q.setAttribute('aria-label','Filter events');
 tools.appendChild(q);
 function apply(){
  var t=q.value.toLowerCase();
  rows.forEach(function(r){
   r.hidden=!!off[r.getAttribute('data-src')]||(t&&r.textContent.toLowerCase().indexOf(t)<0);
  });
 }
 rows.forEach(function(r){var s=r.getAttribute('data-src');if(seen.indexOf(s)<0)seen.push(s);});
 seen.forEach(function(s){
  var l=d.createElement('label'),b=d.createElement('input');
  b.type='checkbox';b.checked=true;
  b.addEventListener('change',function(){off[s]=!b.checked;apply();});
  l.appendChild(b);l.appendChild(d.createTextNode(' '+s));tools.appendChild(l);
 });
 q.addEventListener('input',apply);
}
})();
"""

__all__ = ["render", "render_bundle"]
