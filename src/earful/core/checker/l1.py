"""L1. Progress: a turn that is owed a response gets one within its budget, and nothing
blocks the turns after it."""

from __future__ import annotations

from . import policy as policies
from .memory import user_turns
from .report import ContractResult, Finding, result, verdict
from .view import Event, ModelCall, Speech, View

MET, MISSED, SUPERSEDED, UNOBSERVABLE = "met", "missed", "superseded", "unobservable"
LEFT = "the user left"
AUDIO = "the first word of a reply starts playing"

Step = tuple[str, str, int | None, int]  # (milestone, status, reached, deadline)


def _milestone(step: str) -> str:
    if step == AUDIO:
        return "milestone 3"
    return "milestone 2" if step.startswith("round ") else "milestone 1"


class _Budget:
    def __init__(self, view: View, policy: policies.L1, speech: Speech) -> None:
        self.clock = max(speech.end, speech.delivery["t_us"])
        self._base = self.clock
        self._end_of_turn = view.end_of_turn_us
        self._slack = policy.slack_us
        self._resume = view.session["false_interruption_timeout_us"] or 0
        self._later = sorted(
            (s for s in view.speeches if s.start > speech.start), key=lambda s: s.start
        )
        self._used = 0
        # The scenario stops waiting when the harness reads the final history.
        self._ended = (view.history or view.end)["t_us"]
        # A user who hangs up is owed nothing that was not yet due.
        self._left = view.hangup["t_us"] if view.hangup else None

    def settle(self, reached: int | None, chain_us: int) -> tuple[str, int]:
        """Status and deadline of a milestone; ``chain_us`` sums the scripted delays before it."""
        while True:
            deadline = self._base + self._end_of_turn + chain_us + self._slack
            if reached is not None and reached <= deadline:
                return MET, deadline
            nxt = self._later[self._used] if self._used < len(self._later) else None
            if (
                nxt is not None
                and nxt.start < deadline
                and (reached is None or nxt.start < reached)
            ):
                if nxt.expects_reply:
                    return SUPERSEDED, deadline  # the later speech carries the obligation
                # Noise or a backchannel restarts the clock once it is known not to interrupt.
                self._used += 1
                self._base = max(self._base, nxt.end + self._resume)
                continue
            if (
                self._left is not None
                and self._left < deadline
                and (reached is None or reached > self._left)
            ):
                return LEFT, deadline
            if reached is None and self._ended < deadline:
                return UNOBSERVABLE, deadline
            return MISSED, deadline


def _judge(
    view: View, policy: policies.L1, speech: Speech, chain: list[ModelCall], calls: list[ModelCall]
) -> tuple[_Budget, list[Step], list[Event | None], bool]:
    """Returns the budget, the steps, the events used and whether a tool asked for silence."""
    budget = _Budget(view, policy, speech)
    steps: list[Step] = []
    used: list[Event | None] = [speech.delivery]
    by_call_id = {t.call_id: t for t in view.tools.values() if t.call_id is not None}

    def ttfb_of(call: ModelCall | None) -> int:
        said = next(
            (u for u in view.utterances.values() if call is not None and u.call == call.call), None
        )
        return (
            said.event["data"]["ttfb_us"] if said is not None else view.providers["tts"]["ttfb_us"]
        )

    def record(name: str, reached: int | None, chain_us: int) -> bool:
        status, deadline = budget.settle(reached, chain_us)
        steps.append((name, status, reached, deadline))
        return status == MET

    first = chain[0] if chain else None
    if not record("a model call sees the turn", first.request["t_us"] if first else None, 0):
        return budget, steps, used, False

    assert first is not None
    used.append(first.request)
    spoke: list[tuple[ModelCall, int]] = []  # replies with text, and the delays before each
    elapsed = 0
    walked = {first.call}
    progress = [(first.request["seq"], 0)]  # (seq, scripted delays on the chain by then)
    silenced = False
    on_track = True
    current: ModelCall | None = first
    rounds = 0
    while current is not None and current.response is not None:
        data = current.response["data"]
        used.append(current.response)
        if current.words:
            spoke.append((current, elapsed))
        if not data["tool_calls"] or rounds >= policy.max_steps:
            elapsed += data["duration_us"]
            progress.append((current.response["seq"], elapsed))
            break
        rounds += 1
        # A refused call has no event of its own: it is timed when the model finished asking.
        asked = current.response["t_us"] + data["duration_us"]
        ran = [
            by_call_id[tc["call_id"]] for tc in data["tool_calls"] if tc["call_id"] in by_call_id
        ]
        rest = [tc for tc in data["tool_calls"] if tc["call_id"] not in by_call_id]
        refused = [r for r in (view.refusal(tc["call_id"]) for tc in rest) if r is not None]
        never = [tc["name"] for tc in rest if view.refusal(tc["call_id"]) is None]
        if any(name not in view.witnessed_tools for name in never):
            # A tool Earful does not witness: whether it was called cannot be known.
            steps.append((f"round {rounds}: every tool call is made", UNOBSERVABLE, None, 0))
            on_track = False
            break
        made = (
            max([t.called["t_us"] for t in ran] + [asked for _ in refused]) if not never else None
        )
        if not record(
            f"round {rounds}: every tool call is made", made, elapsed + data["duration_us"]
        ):
            on_track = False
            break
        used += [t.called for t in ran] + refused
        ids = {t.tool for t in ran}
        # A write costs its caller its latency, or the wait before it was told of a timeout.
        latency = sum(
            e.timed_out["data"]["after_us"] if e.timed_out else e.requested["data"]["latency_us"]
            for e in view.effects.values()
            if e.requested["data"]["tool"] in ids
        )
        # The framework finishes speaking a reply before it takes the next step.
        audio = ttfb_of(current) + view.audio_us(current) if current.words else 0
        elapsed += data["duration_us"] + audio + latency
        outcomes = [t.outcome for t in ran if t.outcome is not None]
        done = len(outcomes) == len(ran) and all(o["kind"] != "tool.cancelled" for o in outcomes)
        finished = max([o["t_us"] for o in outcomes] + [asked for _ in refused]) if done else None
        if not record(f"round {rounds}: every tool call finishes", finished, elapsed):
            on_track = False
            break
        used += outcomes
        last = max([o["seq"] for o in outcomes] + [r["seq"] - 1 for r in refused])
        progress.append((last, elapsed))
        if not refused and not any(o["data"]["expects_reply"] for o in outcomes):
            silenced = True  # every tool asked for silence: no further call is owed
            break
        follow = next((c for c in calls if c.request["seq"] > last), None)
        if not record(
            f"round {rounds}: a further model call follows",
            follow.request["t_us"] if follow else None,
            elapsed,
        ):
            on_track = False
            break
        # Stay on this turn's chain; a call made for a later turn is that turn's.
        current = follow if follow in chain else None
        if current is not None:
            walked.add(current.call)

    # A reply from a repeat call for the same turn is budgeted from the chain as it stood then.
    for call in chain:
        if call.words and call.call not in walked:
            before = max((spent for seq, spent in progress if seq < call.request["seq"]), default=0)
            spoke.append((call, before))

    # Milestone 3: of the chain's replies, the one whose audio started first counts.
    def budget_of(call: ModelCall, before: int) -> int:
        return before + call.answer["data"]["duration_us"] + ttfb_of(call)

    unknown = False
    heard: list[tuple[int, ModelCall, int]] = []
    for call, before in spoke:
        playout = view.playout(call.call)
        if playout.unattributable:
            unknown = True
        elif playout.first_audible_us is not None:
            heard.append((playout.first_audible_us, call, before))
    if heard:
        at, call, before = min(heard, key=lambda h: (h[0], h[1].call))
        playout = view.playout(call.call)
        used += [playout.utterance.event if playout.utterance else None]
        used += [s.started for s in playout.segments]
        status, deadline = budget.settle(at, budget_of(call, before))
        if status == MISSED and unknown:
            status = UNOBSERVABLE  # a reply whose audio cannot be attributed may have come first
        steps.append((AUDIO, status, at, deadline))
    elif unknown:
        steps.append((AUDIO, UNOBSERVABLE, None, 0))
    elif spoke:
        record(AUDIO, None, max(budget_of(call, before) for call, before in spoke))
    elif on_track and not silenced:
        # Nothing was said and nothing excuses it: the turn is still owed its first word.
        record(AUDIO, None, elapsed + ttfb_of(None))
    return budget, steps, used, silenced


def check(view: View, policy: policies.L1) -> ContractResult:
    verdicts, notes = [], []
    calls = list(view.calls.values())
    # A model call belongs to the latest turn in its context that is owed a reply.
    latest_turn: dict[int, int | None] = {}
    for call in calls:
        seen = user_turns(
            call.items, [s for s in view.delivered if s.delivery["seq"] < call.request["seq"]]
        )
        owed = [s.index for s in seen.present if s.expects_reply]
        latest_turn[call.call] = owed[-1] if owed else None

    for speech in view.delivered:
        if not speech.expects_reply:
            continue
        chain = [c for c in calls if latest_turn[c.call] == speech.index]
        budget, steps, used, silenced = _judge(view, policy, speech, chain, calls)
        instance = {"speech": speech.index}
        statuses = [status for _name, status, _reached, _deadline in steps]
        audio = next((s for s in steps if s[0] == AUDIO and s[2] is not None), None)
        measures = {
            "clock_start_us": budget.clock,
            "time_to_first_model_call_us": chain[0].request["t_us"] - budget.clock
            if chain
            else None,
            "time_to_first_audio_us": audio[2] - budget.clock if audio else None,
            "milestones": [
                {
                    "milestone": name,
                    "status": status,
                    "reached_us": reached,
                    "deadline_us": deadline,
                }
                for name, status, reached, deadline in steps
            ],
        }
        flags = ["silenced_by_a_tool"] if silenced else []
        if any(c.call in view.read_calls for c in chain):
            flags.append("most_favourable_reading")

        missed = [s for s in steps if s[1] == MISSED]
        if missed:
            findings = [
                Finding(
                    _milestone(name),
                    f"{name}: "
                    + (
                        f"happened {reached - deadline} us after its deadline"
                        if reached is not None
                        else "never happened"
                    )
                    + f" (deadline {deadline} us)",
                )
                for name, _status, reached, deadline in missed
            ]
            verdicts.append(
                verdict(
                    "L1",
                    instance,
                    f"the turn of speech {speech.index} did not progress in time",
                    events=used,
                    findings=findings,
                    measures=measures,
                    flags=flags,
                )
            )
        elif SUPERSEDED in statuses:
            notes.append(
                f"speech {speech.index}: the user spoke again before a response was due; "
                "the later speech carries the obligation"
            )
        elif LEFT in statuses:
            notes.append(f"speech {speech.index}: the user hung up before a response was due")
        elif UNOBSERVABLE in statuses:
            verdicts.append(
                verdict(
                    "L1",
                    instance,
                    f"cannot tell whether the turn of speech {speech.index} progressed",
                    events=used,
                    measures=measures,
                    flags=flags,
                    unobservable="the run ended before a deadline passed, a tool was not "
                    "witnessed, or the reply's audio cannot be attributed",
                )
            )
        else:
            verdicts.append(
                verdict(
                    "L1",
                    instance,
                    f"the turn of speech {speech.index} got its response in time"
                    if not silenced
                    else f"the turn of speech {speech.index} progressed; "
                    "its tool asked for silence",
                    events=used,
                    measures=measures,
                    flags=flags,
                )
            )
    return result("L1", policy, verdicts, notes)
