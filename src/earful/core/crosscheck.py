"""Compare a run on the real clock with the run of the same case on the virtual clock.

The virtual clock is what makes a run repeatable, and it is also the one thing about a
run that a production system does not have. A finding made on it is worth cross-checking:
run the same case on the real clock, with nothing patched, and see whether the same
things happen.

A real-clock run is never evidence. It is not repeatable, and ``validity`` says so of
every one. The comparison leaves that rule alone. It asks a narrower question of a
real-clock ledger that is well formed: does it agree with the virtual ledger, within
tolerances the caller declares, on

    order     what happened, and in what order where the virtual run leaves no doubt
    timing    when each thing happened from the user's first audio on, measured from it
    audible   which words each segment played, and whether it was cut short
    memory    what each model call was shown, and the final history
    effects   which writes were requested and what became of each
    verdicts  which contract rules broke

Two events closer together on the virtual clock than the time tolerance may come in
either order on the real one. A segment may differ by the declared number of words where
it was cut short, and an assistant message that was cut short may differ by as many.

Framework-independent: this module must never import livekit.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any

from .checker import _judge
from .schema import validity

Event = dict[str, Any]

_REAL = "the run was on the real clock"
ASPECTS = ("order", "timing", "audible", "memory", "effects", "verdicts")


@dataclass(frozen=True)
class Tolerance:
    """How far a real-clock run may be from the virtual one and still agree with it."""

    time_us: int = 100_000  # the time of an event, from the user's first audio
    words: int = 1  # the words a segment played, where it was cut short

    def __post_init__(self) -> None:
        if self.time_us < 0 or self.words < 0:
            raise ValueError("a tolerance cannot be negative")


@dataclass(frozen=True)
class Difference:
    """One thing on which the real-clock run does not agree with the virtual one."""

    aspect: str  # one of ASPECTS
    detail: str


@dataclass(frozen=True)
class Comparison:
    """What comparing one real-clock run with the virtual run came to."""

    differences: tuple[Difference, ...]
    drift_us: int  # the largest difference in time between an event and its counterpart
    rules: tuple[str, ...]  # the rules the virtual run broke
    real_rules: tuple[str, ...]  # the rules the real-clock run broke
    # Why the real-clock run could not be compared at all, if it could not: a reason
    # other than its clock for which it is not a well-formed, complete run.
    unusable: tuple[str, ...] = ()

    @property
    def agrees(self) -> bool:
        return not self.differences and not self.unusable

    def on(self, aspect: str) -> tuple[Difference, ...]:
        return tuple(d for d in self.differences if d.aspect == aspect)

    def to_json(self) -> dict[str, Any]:
        return {
            "agrees": self.agrees,
            "drift_us": self.drift_us,
            "rules": list(self.rules),
            "real_rules": list(self.real_rules),
            "unusable": list(self.unusable),
            "differences": [{"aspect": d.aspect, "detail": d.detail} for d in self.differences],
        }


def _origin(events: list[Event]) -> int:
    return next((e["t_us"] for e in events if e["kind"] == "user.origin"), 0)


_NAMED_BY = {
    "user": "speech",
    "llm": "call",
    "tts": "utterance",
    "tool": "tool",
    "effect": "request",
    "sink": "segment",
    "playout": "segment",
}


def _named(events: list[Event]) -> dict[tuple[str, Any, int], Event]:
    """Every event Earful's own witnesses recorded, under a name that is the same in two
    runs of one case: its kind, what it is about, and which occurrence of that it is. The
    framework's own events are left out: they explain a run and decide nothing."""
    out: dict[tuple[str, Any, int], Event] = {}
    seen: dict[tuple[str, Any], int] = {}
    for event in events:
        if event["source"] == "framework":
            continue
        about = event["data"].get(_NAMED_BY.get(event["kind"].split(".")[0], ""))
        occurrence = seen[event["kind"], about] = seen.get((event["kind"], about), 0) + 1
        out[event["kind"], about, occurrence] = event
    return out


def _label(name: tuple[str, Any, int]) -> str:
    kind, about, occurrence = name
    what = kind if about is None else f"{kind} {about}"
    return what if occurrence == 1 else f"{what} (occurrence {occurrence})"


def _order_and_timing(
    virtual: list[Event], real: list[Event], tolerance: Tolerance
) -> tuple[list[Difference], int]:
    ours, theirs = _named(virtual), _named(real)
    out = [
        Difference("order", f"{_label(name)} happened on the virtual clock only")
        for name in ours
        if name not in theirs
    ]
    out += [
        Difference("order", f"{_label(name)} happened on the real clock only")
        for name in theirs
        if name not in ours
    ]
    both = [name for name in ours if name in theirs]
    start, real_start = _origin(virtual), _origin(real)
    at = {name: ours[name]["t_us"] - start for name in both}
    real_at = {name: theirs[name]["t_us"] - real_start for name in both}
    # Time is compared from the user's first audio on. What is recorded before it is the
    # session starting up, which takes no virtual time and as long as the machine takes.
    began = next((e["seq"] for e in virtual if e["kind"] == "user.origin"), 0)
    timed = [name for name in both if ours[name]["seq"] >= began]
    drift = max((abs(real_at[name] - at[name]) for name in timed), default=0)
    late = [name for name in timed if abs(real_at[name] - at[name]) > tolerance.time_us]
    out += [
        Difference(
            "timing",
            f"{_label(name)} at {real_at[name] / 1e6:.3f} s; "
            f"on the virtual clock at {at[name] / 1e6:.3f} s",
        )
        for name in late
    ]
    # Where the virtual run leaves no doubt which of two events came first, the real one
    # has to agree. Each event is reported against the first it wrongly precedes.
    for first, second in itertools.combinations(both, 2):
        if at[second] - at[first] > tolerance.time_us and (
            theirs[second]["seq"] < theirs[first]["seq"]
        ):
            gap = (at[second] - at[first]) / 1e6
            out.append(
                Difference(
                    "order",
                    f"{_label(second)} came before {_label(first)}; on the virtual clock "
                    f"it comes {gap:.3f} s after",
                )
            )
    return out, drift


def _segments(events: list[Event]) -> list[dict[str, Any]]:
    said = {
        e["data"]["utterance"]: e["data"]["text"] for e in events if e["kind"] == "tts.synthesized"
    }
    return [
        {
            "segment": e["data"]["segment"],
            "text": " ".join(said.get(u, "") for u in e["data"]["utterances"]),
            "played": len(e["data"]["words_played"]),
            "interrupted": e["data"]["interrupted"],
            "audible": e["data"]["audible"],
        }
        for e in events
        if e["kind"] == "playout.finished"
    ]


def _audible(virtual: list[Event], real: list[Event], tolerance: Tolerance) -> list[Difference]:
    ours, theirs = _segments(virtual), _segments(real)
    if len(ours) != len(theirs):
        return [Difference("audible", f"{len(theirs)} segments played; {len(ours)} on the virtual")]
    out = []
    for a, b in zip(ours, theirs):
        where = f"segment {a['segment']}"
        if a["text"] != b["text"]:
            out.append(Difference("audible", f"{where} said {b['text']!r}, not {a['text']!r}"))
        elif (a["interrupted"], a["audible"]) != (b["interrupted"], b["audible"]):
            was = "was" if b["interrupted"] else "was not"
            out.append(
                Difference("audible", f"{where} {was} cut short, unlike on the virtual clock")
            )
        elif abs(a["played"] - b["played"]) > (tolerance.words if a["interrupted"] else 0):
            out.append(
                Difference(
                    "audible", f"{where} played {b['played']} words; {a['played']} on the virtual"
                )
            )
    return out


def _item(item: dict[str, Any]) -> tuple[Any, ...]:
    """A chat item without what differs between any two runs: its random id."""
    kind = item["type"]
    if kind == "message":
        return (kind, item.get("role"), item.get("text") or "", item.get("interrupted", False))
    if kind == "function_call":
        return (kind, item.get("name"), item.get("arguments"), item.get("call_id"))
    if kind == "function_call_output":
        return (
            kind,
            item.get("name"),
            item.get("output"),
            item.get("call_id"),
            item.get("is_error"),
        )
    return (kind,)


def _same_item(a: tuple[Any, ...], b: tuple[Any, ...], tolerance: Tolerance) -> bool:
    if a == b:
        return True
    # A message that was cut short holds what had been played, give or take the words
    # the two clocks may differ by at the cut.
    if a[0] == b[0] == "message" and a[1] == b[1] == "assistant" and a[3] and b[3]:
        short, long = sorted((a[2].split(), b[2].split()), key=len)
        return long[: len(short)] == short and len(long) - len(short) <= tolerance.words
    return False


def _memory(virtual: list[Event], real: list[Event], tolerance: Tolerance) -> list[Difference]:
    def shown(events: list[Event]) -> dict[str, list[tuple[Any, ...]]]:
        out = {}
        for e in events:
            if e["kind"] == "llm.request":
                out[f"model call {e['data']['call']}"] = [_item(i) for i in e["data"]["items"]]
            elif e["kind"] == "history.final":
                out["the final history"] = [_item(i) for i in e["data"]["items"]]
        return out

    ours, theirs = shown(virtual), shown(real)
    out = []
    for where in ours.keys() & theirs.keys():
        a, b = ours[where], theirs[where]
        if len(a) != len(b):
            out.append(
                Difference("memory", f"{where} holds {len(b)} items; {len(a)} on the virtual")
            )
            continue
        for n, (x, y) in enumerate(zip(a, b)):
            if not _same_item(x, y, tolerance):
                out.append(Difference("memory", f"{where}, item {n}: {y!r}, not {x!r}"))
                break
    return sorted(out, key=lambda d: d.detail)


def _effects(virtual: list[Event], real: list[Event]) -> list[Difference]:
    def writes(events: list[Event]) -> list[tuple[Any, ...]]:
        became: dict[int, str] = {}
        gave_up: set[int] = set()
        for e in events:
            if e["kind"] in ("effect.committed", "effect.deduplicated", "effect.failed"):
                became[e["data"]["request"]] = e["kind"]
            elif e["kind"] == "effect.timed_out":
                gave_up.add(e["data"]["request"])
        return [
            (
                e["data"]["request"],
                e["data"]["intent"],
                e["data"]["key"],
                repr(e["data"]["payload"]),
                became.get(e["data"]["request"], "no outcome"),
                e["data"]["request"] in gave_up,
            )
            for e in events
            if e["kind"] == "effect.requested"
        ]

    ours, theirs = writes(virtual), writes(real)
    if ours == theirs:
        return []
    for a, b in itertools.zip_longest(ours, theirs):
        if a != b:
            return [Difference("effects", f"write {(b or a)[0]}: {b!r}, not {a!r}")]
    return []


def _broken(events: list[Event]) -> tuple[str, ...]:
    _reading, _readings, results = _judge(events)
    return tuple(
        sorted(
            {
                f"{verdict.contract}/{finding.rule}"
                for result in results.values()
                for verdict in result.failures
                for finding in verdict.findings
            }
        )
    )


def compare(
    virtual: list[Event], real: list[Event], tolerance: Tolerance | None = None
) -> Comparison:
    """Compare the ledger of a real-clock run with the ledger of the virtual run of the
    same case.

    Raises ``ValueError`` if ``virtual`` is not a valid run, if ``real`` is not marked as
    a real-clock run, or if the two do not declare the same scenario. A real-clock run
    that is not well formed, or is not a complete run for a reason other than its clock,
    is returned as ``unusable``: nothing can be concluded from it.
    """
    tolerance = Tolerance() if tolerance is None else tolerance
    reasons = validity(virtual)
    if reasons:
        raise ValueError("the virtual run is not valid: " + "; ".join(reasons))
    reasons = validity(real)
    if _REAL not in reasons:
        raise ValueError("the run to compare is not a real-clock run")
    ours, theirs = virtual[0]["data"], real[0]["data"]
    for name in ("case", "seed", "scenario", "contracts", "framework"):
        if ours[name] != theirs[name]:
            raise ValueError(f"the two runs do not declare the same {name}")
    rules = _broken(virtual)
    other = tuple(reason for reason in reasons if reason != _REAL)
    if other:
        return Comparison((), 0, rules, (), unusable=other)

    real_rules = _broken(real)
    in_order, drift = _order_and_timing(virtual, real, tolerance)
    differences = [
        *in_order,
        *_audible(virtual, real, tolerance),
        *_memory(virtual, real, tolerance),
        *_effects(virtual, real),
    ]
    if rules != real_rules:
        broke = ", ".join(real_rules) or "no rule"
        differences.append(
            Difference(
                "verdicts", f"broke {broke}; the virtual run broke {', '.join(rules) or 'none'}"
            )
        )
    differences.sort(key=lambda d: ASPECTS.index(d.aspect))
    return Comparison(tuple(differences), drift, rules, real_rules)


@dataclass(frozen=True)
class CrossCheck:
    """One case run once on the virtual clock and several times on the real one."""

    virtual: list[Event]
    real: tuple[list[Event], ...]  # the ledger of each real-clock run
    comparisons: tuple[Comparison, ...]  # each real-clock run against the virtual one
    tolerance: Tolerance

    @property
    def agrees(self) -> bool:
        """Every real-clock run agrees with the virtual one."""
        return bool(self.comparisons) and all(c.agrees for c in self.comparisons)

    @property
    def unusable(self) -> bool:
        """Some real-clock run could not be compared at all."""
        return any(c.unusable for c in self.comparisons)

    def lines(self) -> list[str]:
        """The result as text for a person."""
        header = self.virtual[0]["data"]
        framework = header["framework"]
        rules = ", ".join(self.comparisons[0].rules if self.comparisons else ()) or "no rule"
        agree = sum(c.agrees for c in self.comparisons)
        out = [
            f"{header['case']} on {framework['name']} {framework['version']}",
            f"on the virtual clock it breaks {rules}",
            (
                f"{agree} of {len(self.comparisons)} real-clock runs agree with the virtual "
                f"run, within {self.tolerance.time_us / 1000:g} ms and "
                f"{self.tolerance.words} word(s)"
            ),
        ]
        for number, comparison in enumerate(self.comparisons, 1):
            if comparison.unusable:
                out.append(f"run {number}: cannot be compared")
                out += [f"    {reason}" for reason in comparison.unusable]
                continue
            broke = ", ".join(comparison.real_rules) or "no rule"
            verdict = "agrees" if comparison.agrees else "differs"
            out.append(
                f"run {number}: {verdict}; breaks {broke}; "
                f"largest drift {comparison.drift_us / 1000:.1f} ms"
            )
            out += [f"    {d.aspect}: {d.detail}" for d in comparison.differences[:12]]
            if len(comparison.differences) > 12:
                out.append(f"    and {len(comparison.differences) - 12} more")
        return out

    def to_json(self) -> dict[str, Any]:
        header = self.virtual[0]["data"]
        return {
            "case": header["case"],
            "framework": header["framework"],
            "tolerance": {"time_us": self.tolerance.time_us, "words": self.tolerance.words},
            "agrees": self.agrees,
            "runs": [comparison.to_json() for comparison in self.comparisons],
        }


def cross_check(
    virtual: list[Event], real: list[list[Event]], tolerance: Tolerance | None = None
) -> CrossCheck:
    """Compare each real-clock ledger in ``real`` with the virtual ledger of the same
    case. Raises ``ValueError`` as ``compare`` does."""
    tolerance = Tolerance() if tolerance is None else tolerance
    return CrossCheck(
        virtual, tuple(real), tuple(compare(virtual, run, tolerance) for run in real), tolerance
    )


__all__ = [
    "ASPECTS",
    "Comparison",
    "CrossCheck",
    "Difference",
    "Tolerance",
    "compare",
    "cross_check",
]
