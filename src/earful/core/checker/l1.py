"""L1. Progress: a turn that is owed a response gets one within its budget, and nothing
blocks the turns after it.

The budget is built from what the scenario declared, never from what the framework did:
the end-of-turn delay, the scripted delays of each step taken so far, and one allowance of
slack. Three milestones are timed against it: a model call that sees the turn; each round
of tool calls being made, finishing and being followed by a further model call; and the
first word of a reply starting to play.
"""

from __future__ import annotations

from . import policy as policies
from .memory import user_turns
from .report import ContractResult, Finding, result, verdict
from .view import ModelCall, Speech, View

MET, MISSED, SUPERSEDED, UNOBSERVABLE = "met", "missed", "superseded", "unobservable"
AUDIO = "the first word of a reply starts playing"

Step = tuple[str, str, int | None, int]  # (milestone, status, reached, deadline)


class _Budget:
    """The deadline for a milestone, and what excuses missing it."""

    def __init__(self, view: View, policy: policies.L1, speech: Speech) -> None:
        assert speech.final is not None
        self.clock = max(speech.end, speech.final["t_us"])  # the later of the two, both world
        self._base = self.clock
        # End of turn can be no later than the VAD's silence plus the endpointing delay.
        self._end_of_turn = view.end_of_turn_us
        self._slack = policy.slack_us
        self._resume = view.session["false_interruption_timeout_us"] or 0
        self._later = sorted((s for s in view.speeches if s.start > speech.start), key=lambda s: s.start)
        self._used = 0
        # The scenario stops waiting when the harness reads the final history.
        self._ended = (view.history or view.end)["t_us"]

    def settle(self, reached: int | None, chain_us: int) -> tuple[str, int]:
        """Was a milestone, reached at ``reached`` (or never), on time? ``chain_us`` is the
        sum of the scripted delays on the way to it."""
        while True:
            deadline = self._base + self._end_of_turn + chain_us + self._slack
            if reached is not None and reached <= deadline:
                return MET, deadline
            nxt = self._later[self._used] if self._used < len(self._later) else None
            if nxt is not None and nxt.start < deadline and (reached is None or nxt.start < reached):
                if nxt.expects_reply:
                    return SUPERSEDED, deadline  # the later speech carries the obligation
                # Noise or a backchannel holds the agent up without taking the turn: the
                # clock restarts once the framework can tell it was not an interruption.
                self._used += 1
                self._base = max(self._base, nxt.end + self._resume)
                continue
            if reached is None and self._ended < deadline:
                return UNOBSERVABLE, deadline
            return MISSED, deadline


def _judge(view: View, policy: policies.L1, speech: Speech, chain: list[ModelCall], calls: list[ModelCall]):
    """Time the milestones of one turn. Returns the budget, the steps, the events used and
    whether the turn's tools asked for silence."""
    budget = _Budget(view, policy, speech)
    steps: list[Step] = []
    used = [speech.final]
    by_call_id = {t.call_id: t for t in view.tools.values() if t.call_id is not None}
    ttfb = view.providers["tts"]["ttfb_us"]

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
    elapsed = 0  # scripted delays on the chain so far
    walked = {first.call}  # calls reached by following the turn's tool rounds
    progress = [(first.request["seq"], 0)]  # (seq, scripted delays on the chain by then)
    silenced = False  # a tool asked for no reply
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
        # Each call the model asked for either reached its tool, or was answered by the
        # framework itself (arguments the tool cannot take, a tool that does not exist).
        # A refusal has no event of its own; it happened when the model finished asking,
        # and the context that first shows it is the call that follows it.
        asked = current.response["t_us"] + data["duration_us"]
        ran = [by_call_id[tc["call_id"]] for tc in data["tool_calls"] if tc["call_id"] in by_call_id]
        rest = [tc for tc in data["tool_calls"] if tc["call_id"] not in by_call_id]
        refused = [r for r in (view.refusal(tc["call_id"]) for tc in rest) if r is not None]
        never = [tc["name"] for tc in rest if view.refusal(tc["call_id"]) is None]
        if any(name not in view.witnessed_tools for name in never):
            # A tool Earful does not witness: whether it was called cannot be known.
            steps.append((f"round {rounds}: every tool call is made", UNOBSERVABLE, None, 0))
            on_track = False
            break
        made = max([t.called["t_us"] for t in ran] + [asked for _ in refused]) if not never else None
        if not record(f"round {rounds}: every tool call is made", made, elapsed + data["duration_us"]):
            on_track = False
            break
        used += [t.called for t in ran] + refused
        ids = {t.tool for t in ran}
        latency = sum(
            e.requested["data"]["latency_us"]
            for e in view.effects.values()
            if e.requested["data"]["tool"] in ids
        )
        # The framework finishes speaking a reply before it takes the next step.
        audio = ttfb + view.audio_us(current) if current.words else 0
        elapsed += data["duration_us"] + audio + latency
        # A tool cancelled when the scenario ended never finished.
        outcomes = [t.outcome for t in ran]
        done = all(o is not None and o["kind"] != "tool.cancelled" for o in outcomes)
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

    # Replies of this turn the walk did not reach: the framework called the model again
    # for the same turn, as it does after a backchannel, or when what it had generated
    # ahead of time was out of date. Each is budgeted from the chain as it stood then.
    for call in chain:
        if call.words and call.call not in walked:
            before = max((spent for seq, spent in progress if seq < call.request["seq"]), default=0)
            spoke.append((call, before))

    # The first word of a reply in the chain starts playing. Any reply of the chain will
    # do: the one whose audio started first is the one that counts.
    def budget_of(call: ModelCall, before: int) -> int:
        return before + call.response["data"]["duration_us"] + ttfb

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
        # Nothing was said, and nothing excuses it: however the chain ended, the turn is
        # still owed its first word.
        record(AUDIO, None, elapsed + ttfb)
    return budget, steps, used, silenced


def check(view: View, policy: policies.L1) -> ContractResult:
    verdicts, notes = [], []
    calls = list(view.calls.values())
    # A model call belongs to the latest turn in its context that is owed a reply. A
    # backchannel or other speech that expects none does not take the call over.
    latest_turn: dict[int, int | None] = {}
    for call in calls:
        seen = user_turns(call.items, [s for s in view.delivered if s.final["seq"] < call.request["seq"]])
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
            "time_to_first_model_call_us": chain[0].request["t_us"] - budget.clock if chain else None,
            "time_to_first_audio_us": audio[2] - budget.clock if audio else None,
            "milestones": [
                {"milestone": name, "status": status, "reached_us": reached, "deadline_us": deadline}
                for name, status, reached, deadline in steps
            ],
        }
        flags = ["silenced_by_a_tool"] if silenced else []
        if any(c.call in view.read_calls for c in chain):
            flags.append("most_favourable_reading")  # which of its replies was voiced is a reading

        missed = [s for s in steps if s[1] == MISSED]
        if missed:
            findings = [
                Finding(
                    "rule",
                    f"{name}: "
                    + (f"happened {reached - deadline} us after its deadline" if reached is not None else "never happened")
                    + f" (deadline {deadline} us)",
                )
                for name, _status, reached, deadline in missed
            ]
            verdicts.append(
                verdict("L1", instance, f"the turn of speech {speech.index} did not progress in time",
                        events=used, findings=findings, measures=measures, flags=flags)
            )
        elif SUPERSEDED in statuses:
            notes.append(
                f"speech {speech.index}: the user spoke again before a response was due; "
                "the later speech carries the obligation"
            )
        elif UNOBSERVABLE in statuses:
            verdicts.append(
                verdict("L1", instance, f"cannot tell whether the turn of speech {speech.index} progressed",
                        events=used, measures=measures, flags=flags,
                        unobservable="the run ended before a deadline passed, a tool was not "
                        "witnessed, or the reply's audio cannot be attributed")
            )
        else:
            verdicts.append(
                verdict("L1", instance, f"the turn of speech {speech.index} got its response in time"
                        if not silenced else f"the turn of speech {speech.index} progressed; its tool asked for silence",
                        events=used, measures=measures, flags=flags)
            )
    return result("L1", policy, verdicts, notes)
