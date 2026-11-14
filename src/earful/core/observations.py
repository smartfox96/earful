"""Ledger queries used by the Gate 0 spikes.

These are not the contract checker. They are the smallest pure functions over a ledger
that show the evidence each historical case turns on, and they are the seed the checker
grows from in Gate 1. Framework-independent: this module must never import livekit.
"""

from __future__ import annotations

from typing import Any

Event = dict[str, Any]


def _of(events: list[Event], source: str, kind: str) -> list[Event]:
    return [e for e in events if e["source"] == source and e["kind"] == kind]


def effect_counts(events: list[Event]) -> dict[str, int]:
    """Durable effects per intent, from the backend's own record."""
    counts: dict[str, int] = {}
    for e in _of(events, "backend", "effect.committed"):
        intent = e["data"]["intent"]
        counts[intent] = counts.get(intent, 0) + 1
    return counts


def memory_gaps(events: list[Event]) -> list[int]:
    """Model calls made after an effect was committed whose context shows no tool result.

    Returns the call numbers. Evidence comes from the backend (the commit) and from the
    scripted model (the context it was actually shown), never from framework state.
    """
    commits = _of(events, "backend", "effect.committed")
    gaps = []
    for request in _of(events, "llm", "llm.request"):
        committed_before = any(c["seq"] < request["seq"] for c in commits)
        has_result = any(i["type"] == "function_call_output" for i in request["data"]["items"])
        if committed_before and not has_result:
            gaps.append(request["data"]["call"])
    return gaps


def user_speaking_windows(events: list[Event]) -> list[tuple[Event, Event]]:
    windows: list[tuple[Event, Event]] = []
    opened: Event | None = None
    for e in events:
        if e["source"] != "world":
            continue
        if e["kind"] == "user.vad_start":
            opened = e
        elif e["kind"] == "user.vad_end" and opened is not None:
            windows.append((opened, e))
            opened = None
    return windows


def playouts_started_over_user(events: list[Event]) -> list[dict[str, Any]]:
    """Playout segments the sink began while the scripted user was already speaking."""
    finished = {e["data"]["segment"]: e for e in _of(events, "sink", "playout.finished")}
    found = []
    for start in _of(events, "sink", "playout.started"):
        for begin, end in user_speaking_windows(events):
            if begin["seq"] < start["seq"] < end["seq"]:
                done = finished.get(start["data"]["segment"])
                found.append(
                    {
                        "segment": start["data"]["segment"],
                        "started_after_user_us": start["t_us"] - begin["t_us"],
                        "audible_us": done["data"]["position_us"] if done else None,
                    }
                )
    return found


def played_vs_remembered(events: list[Event]) -> list[dict[str, Any]]:
    """For each interrupted playout: words the sink consumed vs. text the framework kept."""
    out = []
    remembered = [
        e["data"]["item"]
        for e in _of(events, "framework", "fw.conversation_item_added")
        if e["data"]["item"].get("role") == "assistant" and e["data"]["item"].get("interrupted")
    ]
    interrupted = [e for e in _of(events, "sink", "playout.finished") if e["data"]["interrupted"]]
    for playout, item in zip(interrupted, remembered):
        played = playout["data"]["text_played"]
        kept = item.get("text") or ""
        out.append(
            {
                "played": played,
                "remembered": kept,
                "word_cut": playout["data"]["word_cut"],
                "extra_words_remembered": len(kept.split()) - len(played.split()),
            }
        )
    return out
