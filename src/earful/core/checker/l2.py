"""L2. Interruption: while the user holds the floor the agent is silent, within a declared bound."""

from __future__ import annotations

from . import policy as policies
from .report import ContractResult, Finding, result, verdict
from .view import View


def check(view: View, policy: policies.L2) -> ContractResult:
    verdicts, notes = [], []
    runs = view.runs()
    minimum = view.session["min_interruption_duration_us"]
    stops = [e for e in view.commands if e["kind"] in ("sink.pause", "sink.clear")]
    starts = [s.started for s in view.segments.values() if s.started] + view.resumes()
    starts.sort(key=lambda e: e["seq"])
    ordered = sorted(view.speeches, key=lambda s: s.start)

    def in_flight(opened: int, closed: int) -> bool:
        """Whether a reply's synthesis began before the floor closed and it had not finished
        playing when the floor opened: playing or waiting, rule b had it to hold back."""
        return any(
            p.utterance is not None
            and p.utterance.event["data"]["started_us"] < closed
            and (p.settled is None or p.settled["t_us"] > opened)
            for p in view.playouts.values()
        )

    for position, speech in enumerate(ordered):
        if speech.noise or speech.backchannel:
            continue
        instance = {"speech": speech.index}
        if view.origin is None:
            verdicts.append(
                verdict(
                    "L2",
                    instance,
                    "cannot place the speech in time",
                    missing=[("no-origin", "the ledger has no true boundaries for this speech")],
                )
            )
            continue
        if speech.vad_start is None:
            # The framework was never told of this speech, so nobody held the floor.
            notes.append(f"speech {speech.index}: the VAD never reported it, so it held no floor")
            continue

        # The floor opens when the VAD has reported the speech, and no earlier than its start.
        opened, closed = max(speech.vad_start["t_us"], speech.start), speech.end
        # The framework counts speech from the start of the VAD's segment, not of this speech.
        reached = max(speech.segment_start + minimum, speech.start)
        point = reached if reached <= speech.end else None
        following = (
            ordered[position + 1].start if position + 1 < len(ordered) else view.end["t_us"] + 1
        )
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

        # Rule a: under way at the interruption point -> silent by that point plus the bound.
        # A reply that starts at the point itself may start there, and is held to the bound.
        run = None
        if point is not None:
            run = next((r for r in runs if r[0][0][0] <= point <= r[0][-1][1]), None)
        if policy.interruptions_allowed and point is not None:
            # What was heard of it from the point on: a gap that outlasts the floor ends it.
            heard = [b for a, b in run[0] if a < closed and b >= point] if run else []
            if run is not None and heard:
                stopped, segment = heard[-1], run[1]
                used += [
                    segment.started,
                    segment.finished,
                    *(p for pair in segment.pauses for p in pair),
                ]
                if command is not None:
                    measures["stop_latency_us"] = max(stopped - command["t_us"], 0)
                measures["audible_after_point_us"] = stopped - point
                if stopped > point + policy.stop_bound_us:
                    findings.append(
                        Finding(
                            "rule a",
                            f"the agent was audible for {stopped - point} us after the "
                            f"interruption point; the bound is {policy.stop_bound_us} us",
                            tuple(e["seq"] for e in (segment.started, command) if e),
                        )
                    )
                    if command is not None and command["t_us"] > point + policy.stop_bound_us:
                        # The framework did act, but later than Earful's model of its threshold.
                        flags.append("reference_in_doubt")

        # Rule b: nothing starts, or is resumed by command, while the user holds the floor.
        for e in starts:
            if opened + policy.start_grace_us < e["t_us"] < closed:
                used.append(e)
                verb = "started" if e["kind"] == "playout.started" else "resumed"
                findings.append(
                    Finding(
                        "rule b",
                        f"segment {e['data']['segment']} {verb} {e['t_us'] - opened} us "
                        "after the user took the floor",
                        (e["seq"],),
                    )
                )

        # With no reply to stop or to hold back there is nothing to judge, unless one started.
        missing = []
        if run is None and not in_flight(opened, closed):
            if point is None:
                short = "the speech ended before it could interrupt, and no reply was in flight"
                missing = [("no-interruption-point", short)]
            else:
                idle = "no reply was under way, or waiting to play, while the user held the floor"
                missing = [("no-reply-under-way", idle)]
        verdicts.append(
            verdict(
                "L2",
                instance,
                "the agent was audible while the user held the floor"
                if findings
                else "nothing was under way to interrupt"
                if missing
                else "the agent was silent while the user held the floor",
                events=used,
                findings=findings,
                missing=missing,
                measures=measures,
                flags=flags,
            )
        )
    return result("L2", policy, verdicts, notes)
