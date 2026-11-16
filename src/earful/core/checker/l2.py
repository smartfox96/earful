"""L2. Interruption: while the user holds the floor the agent is silent, within a declared bound."""

from __future__ import annotations

from . import policy as policies
from .report import ContractResult, Finding, result, verdict
from .view import View


def check(view: View, policy: policies.L2) -> ContractResult:
    verdicts, notes = [], []
    spans = view.audible()
    minimum = view.session["min_interruption_duration_us"]
    stops = [e for e in view.commands if e["kind"] in ("sink.pause", "sink.clear")]
    starts = [
        e for e in view.events if e["kind"] in ("playout.started", "playout.resumed")
    ]
    ordered = sorted(view.speeches, key=lambda s: s.start)

    for position, speech in enumerate(ordered):
        if speech.noise or speech.backchannel:
            continue
        instance = {"speech": speech.index}
        if view.origin is None:
            verdicts.append(
                verdict("L2", instance, "cannot place the speech in time",
                        unobservable="the ledger has no true boundaries for this speech")
            )
            continue
        if speech.vad_start is None:
            # The framework was never told of this speech, so nobody held the floor.
            notes.append(f"speech {speech.index}: the run ended before the VAD reported it")
            continue

        # The floor opens when the VAD has told the framework, and not before the user is
        # in fact speaking: a speech that resumes inside a segment the VAD already reported
        # holds the floor from its own start.
        opened, closed = max(speech.vad_start["t_us"], speech.start), speech.end
        # The framework counts speech from the start of the VAD's segment. This speech is
        # the one that interrupts if the count reaches the minimum while it is under way.
        reached = max(speech.segment_start + minimum, speech.start)
        point = reached if reached <= speech.end and speech.segment_end - speech.segment_start >= minimum else None
        following = ordered[position + 1].start if position + 1 < len(ordered) else view.end["t_us"] + 1
        command = next((e for e in stops if speech.start <= e["t_us"] < following), None)
        findings, flags, used = [], [], [speech.vad_start, command]
        measures = {
            "floor_opened_us": opened,
            "floor_closed_us": closed,
            "interruption_point_us": point,
            "recognition_delay_us": command["t_us"] - speech.start if command else None,
            "decision_lag_us": command["t_us"] - point if command and point is not None else None,
            "stop_latency_us": None,
        }

        # Rule a: audible at the interruption point -> silent by that point plus the bound.
        if policy.interruptions_allowed and point is not None:
            span = next(((a, b, seg) for a, b, seg in spans if a < point <= b), None)
            if span is not None:
                _a, stopped, segment = span
                used += [segment.started, segment.finished, *(p for pair in segment.pauses for p in pair)]
                if command is not None:
                    measures["stop_latency_us"] = max(stopped - command["t_us"], 0)
                measures["audible_after_point_us"] = stopped - point
                if stopped > point + policy.stop_bound_us:
                    findings.append(Finding(
                        "rule a", f"the agent was audible for {stopped - point} us after the "
                        f"interruption point; the bound is {policy.stop_bound_us} us",
                        tuple(e["seq"] for e in (segment.started, command) if e)))
                    if command is not None and command["t_us"] > point + policy.stop_bound_us:
                        # The framework did act, but later than Earful's model of its threshold.
                        flags.append("reference_in_doubt")

        # Rule b: nothing starts or resumes while the user holds the floor.
        for e in starts:
            if opened + policy.start_grace_us < e["t_us"] < closed:
                used.append(e)
                verb = "started" if e["kind"] == "playout.started" else "resumed"
                findings.append(Finding(
                    "rule b", f"segment {e['data']['segment']} {verb} {e['t_us'] - opened} us "
                    "after the user took the floor", (e["seq"],)))

        verdicts.append(
            verdict(
                "L2",
                instance,
                "the agent was silent while the user held the floor"
                if not findings
                else "the agent was audible while the user held the floor",
                events=used,
                findings=findings,
                measures=measures,
                flags=flags,
            )
        )
    return result("L2", policy, verdicts, notes)
