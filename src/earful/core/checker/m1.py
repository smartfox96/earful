"""M1. Played vs. remembered: what the agent remembers having said matches what was played."""

from __future__ import annotations

from typing import Any

from . import policy as policies
from .memory import align, favourable
from .report import ContractResult, Finding, result, verdict
from .view import Event, Playout, Spoken, View

Context = tuple[str, Event, list[dict[str, Any]]]
READ = "most_favourable_reading"


def _judged_in(view: View, item: Spoken, playout: Playout) -> tuple[list[Context], list[str]]:
    """The contexts that must agree with what was played of it, and the flags that say which."""
    calls = [c for c in view.contexts if c[1]["kind"] == "llm.request"]
    history = [c for c in view.contexts if c[1]["kind"] == "history.final"]
    if playout.settled is None:
        # No audio of it reached the sink, so it is owed absence from the moment it came to be.
        after = item.born["seq"]
        flags = ["never_reached_the_output", "judged_on_final_history"]
        return [c for c in calls if c[1]["seq"] > after] + history, flags
    after = playout.settled["seq"]
    following = [c for c in calls if c[1]["seq"] > after]
    if following:
        return following, []
    # The final history counts only if it was read after the playout finished.
    return [c for c in history if c[1]["seq"] > after], ["judged_on_final_history"]


def _compare(
    view: View,
    policy: policies.M1,
    item: Spoken,
    playout: Playout,
    message: dict[str, Any] | None,
    context: Context,
) -> tuple[dict[str, Any], list[Finding]]:
    label, event, _items = context
    words, played, cut = item.words, playout.played, playout.cut
    remembered = len((message.get("text") or "").split()) if message else 0
    # Positive drift: memory is ahead. The cut word may be remembered or not at no cost.
    if remembered > played + cut:
        drift = view.end_us(item, playout.utterance, remembered) - view.end_us(
            item, playout.utterance, played + cut
        )
    elif remembered < played:
        drift = view.end_us(item, playout.utterance, remembered) - view.end_us(
            item, playout.utterance, played
        )
    else:
        drift = 0
    row = {
        "context": label,
        "remembered": remembered,
        "drift_us": drift,
        "remembered_minus_played": remembered - played,
        "remembered_minus_played_and_cut": remembered - played - cut,
        "marked_interrupted": message["interrupted"] if message else None,
    }
    findings: list[Finding] = []
    seqs = (event["seq"],)
    if playout.first_audible_us is None:
        if message is not None:
            findings.append(
                Finding(
                    "rule 4",
                    f"{label}: it never became audible, yet {remembered} "
                    "word(s) of it are remembered",
                    seqs,
                )
            )
        return row, findings
    if -drift > policy.behind_us:
        findings.append(
            Finding(
                "rule 2",
                f"{label}: {played} word(s) were played but only {remembered} "
                f"are remembered: {-drift} us of played audio is forgotten",
                seqs,
            )
        )
    if drift > policy.ahead_us:
        findings.append(
            Finding(
                "rule 3",
                f"{label}: {remembered} word(s) are remembered but only {played} "
                f"were played{' and one cut' if cut else ''}: memory is {drift} us of "
                "audio ahead",
                seqs,
            )
        )
    # A stop landing as the last word ended leaves the audio whole, so either mark is right.
    cut_short = played < len(words) or playout.interrupted
    either_mark = view.stopped_as_it_ended(playout, len(words))
    if (
        message is not None
        and policy.check_interrupted_flag
        and not either_mark
        and message["interrupted"] != cut_short
    ):
        findings.append(
            Finding(
                "rule 5",
                f"{label}: the message is marked "
                f"{'interrupted' if message['interrupted'] else 'complete'}, but "
                f"{played} of {len(words)} words were played and the playout was "
                f"{'cut short' if cut_short else 'not cut short'}",
                seqs,
            )
        )
    return row, findings


def check(view: View, policy: policies.M1) -> ContractResult:
    playouts = view.playouts
    judged: dict[str, tuple[list[Context], list[str]]] = {
        item.key: _judged_in(view, item, playouts[item.key])
        for item in view.spoken
        if playouts[item.key].unknown is None
    }

    # Among identical texts, the match that agrees best with what was played is taken.
    read: dict[str, tuple[dict[str, int | None] | None, set[str]]] = {}

    def reading_of(context: Context) -> tuple[dict[str, int | None] | None, set[str]]:
        label, event, items = context
        if label not in read:
            said = [s for s in view.spoken if s.born["seq"] < event["seq"]]

            def cost(item: Spoken, message: dict[str, Any] | None) -> tuple[int, int]:
                contexts = judged.get(item.key, ([], []))[0]
                if not any(c[0] == label for c in contexts):
                    return (0, 0)  # this context does not judge it
                row, findings = _compare(view, policy, item, playouts[item.key], message, context)
                return (len(findings), abs(row["drift_us"]))

            alignment = align(items, said)
            read[label] = (
                favourable(items, said, cost),
                alignment.ambiguous if alignment.possible else set(),
            )
        return read[label]

    verdicts = []
    for item in view.spoken:
        playout = playouts[item.key]
        instance = item.instance
        base = [item.born, playout.utterance.event if playout.utterance else None]
        base += [s.finished for s in playout.segments]
        missing = playout.unknown
        if missing:
            verdicts.append(
                verdict(
                    "M1",
                    instance,
                    "cannot tell how much of it was played",
                    events=base,
                    missing=[missing],
                )
            )
            continue

        contexts, flags = judged[item.key]
        flags = list(flags)
        played, cut = playout.played, playout.cut
        if view.stopped_as_it_ended(playout, len(item.words)):
            flags.append("stopped_as_it_ended")
        if playout.outlasted_hangup:
            flags.append("played_on_after_the_hangup")  # judged on what the user heard
            base.append(view.hangup)
        chosen = item.call in view.read_calls  # its audio rests on a reading of the ledger
        findings, rows = [], []
        unknown: list[tuple[str, str]] = []
        for context in contexts:
            label, _event, items = context
            matched, ambiguous = reading_of(context)
            if matched is None:
                unknown.append(
                    (
                        "memory-unaligned",
                        f"{label}: its assistant messages do not line up with what was said",
                    )
                )
                continue
            chosen = chosen or item.key in ambiguous
            at = matched.get(item.key)
            row, found = _compare(
                view, policy, item, playout, items[at] if at is not None else None, context
            )
            rows.append(row)
            findings += found
        if chosen:
            flags.append(READ)

        measures = {"words": len(item.words), "played": played, "cut": cut, "contexts": rows}
        events = [*base, *(event for _label, event, _items in contexts)]
        if not rows and not findings:
            if playout.settled is not None and view.history is not None:
                late = "the final history was read before its playout finished"
                lacking = unknown or [("history-before-playout", late)]
            else:
                lacking = unknown or [
                    ("no-memory-after", "no model context or final history follows it")
                ]
            verdicts.append(
                verdict(
                    "M1",
                    instance,
                    "cannot tell what is remembered of it",
                    events=events,
                    measures=measures,
                    flags=flags,
                    missing=lacking,
                )
            )
            continue
        verdicts.append(
            verdict(
                "M1",
                instance,
                f"{played} of {len(item.words)} words played"
                + (", one cut" if cut else "")
                + (f"; remembered {rows[0]['remembered']}" if rows else ""),
                events=events,
                findings=findings,
                measures=measures,
                flags=flags,
            )
        )
    return result("M1", policy, verdicts)
