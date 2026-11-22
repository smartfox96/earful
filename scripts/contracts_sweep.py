"""Sweep ordinary scenario shapes, moving one user speech across each, and check every run.

python scripts/contracts_sweep.py [family ...] [--step 0.05] [--dump DIR]
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

import livekit.agents as lk

from earful.core.backend import effect
from earful.core.checker import check
from earful.core.ledger import write_jsonl
from earful.livekit import scenario
from earful.livekit.cases import BOOKING_ACK, CASES, LONG_REPLY
from earful.livekit.harness import Case, Intent, InvalidRun, run_case
from earful.livekit.providers import Claim, Reply, ToolCall, UserSpeech

INTENT = "book-table"
LOOKUP = "check-availability"
BOOK = '{"party_size": 6}'
ASK_MENU = UserSpeech(start=0.5, end=2.5, transcript="Tell me about the menu.")
ASK_BOOK = UserSpeech(start=0.5, end=2.5, transcript="Book a table for six tonight.")
INTENTS = [
    Intent(INTENT, tools=("create_reservation",)),
    Intent(LOOKUP, tools=("check_availability",)),
]


def last_user(items):
    for item in reversed(items):
        if item["type"] == "message" and item.get("role") == "user":
            return (item.get("text") or "").lower()
    return ""


def outputs(items):
    return [i for i in items if i["type"] == "function_call_output"]


def after_a_tool(items):
    return bool(items) and items[-1]["type"] == "function_call_output"


def host(items):
    text = last_user(items)
    if "stop" in text or "wait" in text:
        return Reply(text="Okay, stopping.")
    if "dessert" in text:
        return Reply(text="For dessert we have a dark chocolate tart.")
    if "uh huh" in text:
        return Reply(text="Shall I go on?")
    if "menu" in text:
        return Reply(text=LONG_REPLY, ttft=0.1, duration=0.4)
    return Reply(text="Sorry, could you repeat that?")


def plain_host():
    return lk.Agent(instructions="You are a restaurant host.")


class Booker(lk.Agent):
    def __init__(self, *, latency=0.0, fail=False, returns="text"):
        super().__init__(instructions="You take restaurant reservations.")
        self._latency, self._fail, self._returns = latency, fail, returns

    @lk.function_tool
    async def create_reservation(self, party_size: int):
        """Create a reservation.

        Args:
            party_size: Number of guests
        """
        receipt = await effect(INTENT, {"party_size": party_size}, latency=self._latency)
        if self._fail:
            raise ValueError("the reservation system is down")
        if self._returns == "dict":
            return {"receipt": receipt, "party_size": party_size}
        return f"Reservation {receipt} confirmed."

    @lk.function_tool
    async def check_availability(self, party_size: int) -> str:
        """Check availability.

        Args:
            party_size: Number of guests
        """
        await effect(LOOKUP, {"party_size": party_size}, latency=self._latency)
        return "A table is free at eight."


class Greeter(lk.Agent):
    def __init__(self):
        super().__init__(instructions="You are a restaurant host.")

    async def on_enter(self):
        self.session.say("Welcome to the restaurant we are glad to have you with us this evening.")


def booking(ack=BOOKING_ACK, call=None):
    """Books when asked, and confirms once the tool's result is last in its context."""

    def policy(items):
        text = last_user(items)
        if after_a_tool(items):
            if outputs(items)[-1]["is_error"]:
                return Reply(text="I am sorry something went wrong.")
            return Reply(text="Your table is booked.", claims=(Claim(INTENT),))
        if "stop" in text:
            return Reply(text="Okay, stopping.")
        if "window" in text:
            return Reply(text="Yes it is right by the window.")
        if "table" in text and not outputs(items):
            asked = call or ToolCall("create_reservation", BOOK)
            return Reply(text=ack, tool_calls=(asked,))
        return Reply(text="How can I help?")

    return policy


def two_rounds(items):
    """Checks availability, then books, then confirms."""
    text = last_user(items)
    names = [i["name"] for i in outputs(items)]
    if (
        after_a_tool(items)
        and names[-1] == "check_availability"
        and "create_reservation" not in names
    ):
        return Reply(
            text="There is a table at eight so I will book it now.",
            tool_calls=(ToolCall("create_reservation", BOOK),),
        )
    if after_a_tool(items):
        return Reply(text="Your table is booked.", claims=(Claim(INTENT),))
    if "window" in text:
        return Reply(text="Yes it is right by the window.")
    if "table" in text and not names:
        return Reply(text="Let me check.", tool_calls=(ToolCall("check_availability", BOOK),))
    return Reply(text="How can I help?")


def parallel_tools(items):
    text = last_user(items)
    if after_a_tool(items):
        return Reply(text="Your table is booked.", claims=(Claim(INTENT),))
    if "window" in text:
        return Reply(text="Yes it is right by the window.")
    if "table" in text and not outputs(items):
        return Reply(
            text="One moment please.",
            tool_calls=(
                ToolCall("check_availability", BOOK),
                ToolCall("create_reservation", BOOK),
            ),
        )
    return Reply(text="How can I help?")


def speech(at, length, text, **kwargs):
    return UserSpeech(start=at, end=round(at + length, 3), transcript=text, **kwargs)


def window(at):
    return speech(at, 1.0, "Is the table by the window?")


def reference(name):
    """A reference case with its second speech moved to ``at``."""

    def build(at):
        case = CASES[name]()
        second = case.speeches[1]
        length = round(second.end - second.start, 3)
        case.speeches = [case.speeches[0], speech(at, length, second.transcript)]
        return case

    return build


def booker(policy, at, **kwargs):
    agent_kwargs = {k: kwargs.pop(k) for k in ("latency", "fail", "returns") if k in kwargs}
    kwargs.setdefault("speeches", [ASK_BOOK, window(at)])
    kwargs.setdefault("drain_delay", 14.0)
    return Case(
        name="sweep",
        agent=lambda: Booker(**agent_kwargs),
        policy=policy,
        intents=INTENTS,
        **kwargs,
    )


def hosted(at, second, **kwargs):
    kwargs.setdefault("drain_delay", 14.0)
    return Case(name="sweep", agent=plain_host, policy=host, speeches=[ASK_MENU, second], **kwargs)


# family -> (first and last start of the moving speech, builder)
FAMILIES = {
    "barge_in_mid_word": (2.5, 12.0, reference("barge_in_mid_word")),
    "book_then_interrupt": (2.5, 12.0, reference("book_then_interrupt")),
    "speak_over_new_turn": (2.5, 6.0, reference("speak_over_new_turn")),
    "slow_tool": (2.5, 11.0, lambda at: booker(booking(), at, latency=3.0)),
    "silent_tool_call": (2.5, 8.0, lambda at: booker(booking(ack=""), at, latency=2.0)),
    "tool_raises": (
        2.5,
        8.0,
        lambda at: booker(booking(ack="One moment please."), at, latency=1.0, fail=True),
    ),
    "tool_returns_dict": (2.5, 8.0, lambda at: booker(booking(), at, latency=0.5, returns="dict")),
    # The model asks for a tool with an argument the tool cannot take.
    "refused_call": (
        2.5,
        8.0,
        lambda at: booker(
            booking(
                ack="One moment please.",
                call=ToolCall("create_reservation", '{"party_size": "six"}'),
            ),
            at,
        ),
    ),
    "two_rounds": (2.5, 12.0, lambda at: booker(two_rounds, at, latency=1.0)),
    "parallel_tools": (2.5, 8.0, lambda at: booker(parallel_tools, at, latency=1.5)),
    "retire": (
        2.5,
        11.0,
        lambda at: booker(
            booking(),
            at,
            latency=2.0,
            speeches=[ASK_BOOK, speech(at, 1.0, "Actually stop that.", retires=(INTENT,))],
        ),
    ),
    # The endpointing delay is long, so the next speech arrives before the turn is over.
    "long_endpointing": (
        2.5,
        8.0,
        lambda at: booker(booking(ack=""), at, latency=2.0, min_endpointing_delay=2.0),
    ),
    "backchannel": (2.5, 12.0, lambda at: hosted(at, speech(at, 0.6, "Uh huh.", backchannel=True))),
    # Noise: the VAD fires and nothing is transcribed.
    "noise": (2.5, 12.0, lambda at: hosted(at, speech(at, 0.8, ""), drain_delay=16.0)),
    # A speech shorter than the interruption threshold.
    "short_speech": (2.5, 12.0, lambda at: hosted(at, speech(at, 0.3, "Wait."))),
    "no_pause": (2.5, 12.0, lambda at: hosted(at, speech(at, 1.0, "Stop."), sink_can_pause=False)),
    "slow_stt": (
        2.5,
        12.0,
        lambda at: Case(
            name="sweep",
            agent=plain_host,
            policy=host,
            drain_delay=10.0,
            speeches=[
                speech(0.5, 2.0, "Tell me about the menu.", stt_delay=1.2),
                speech(at, 1.0, "Stop.", stt_delay=1.2),
            ],
        ),
    ),
    "quick_interrupt": (
        2.5,
        12.0,
        lambda at: hosted(
            at, speech(at, 1.0, "Stop."), min_interruption_duration=0.2, min_endpointing_delay=1.0
        ),
    ),
    "slow_interrupt": (
        2.5,
        12.0,
        lambda at: hosted(at, speech(at, 1.5, "Stop."), min_interruption_duration=1.0),
    ),
    "greeting": (
        0.1,
        7.0,
        lambda at: Case(
            name="sweep",
            agent=Greeter,
            policy=host,
            drain_delay=12.0,
            speeches=[speech(at, 1.5, "Tell me about the menu.")],
        ),
    ),
    # Three turns: the third moves across the reply to the second.
    "three_turns": (
        6.0,
        14.0,
        lambda at: Case(
            name="sweep",
            agent=plain_host,
            policy=host,
            drain_delay=8.0,
            speeches=[
                ASK_MENU,
                speech(5.0, 1.0, "Stop."),
                speech(at, 1.2, "Actually what about dessert?"),
            ],
        ),
    ),
    # Under half a second apart, a VAD hears the two halves as one.
    "pauses_mid_sentence": (
        1.5,
        5.0,
        lambda at: Case(
            name="sweep",
            agent=plain_host,
            policy=host,
            drain_delay=14.0,
            speeches=[speech(0.5, 1.0, "Tell me about"), speech(at, 1.0, "the menu please.")],
        ),
    ),
    "goes_on_speaking": (
        5.0,
        9.0,
        lambda at: booker(
            booking(ack=""),
            at,
            latency=4.0,
            speeches=[
                ASK_BOOK,
                window(at),
                speech(round(at + 1.6, 3), 0.8, "And is it quiet?"),
                speech(round(at + 3.0, 3), 0.8, "We have a baby with us."),
            ],
        ),
    ),
}


class _AtTheEnd:
    """``at_the_end.<family>`` is the family with its moving speech at its last position."""

    def __getattr__(self, family):
        if family not in FAMILIES:
            raise AttributeError(family)
        _first, last, build = FAMILIES[family]
        return lambda: build(last)


at_the_end = _AtTheEnd()


def run_one(job):
    family, at, dump = job
    try:
        result = run_case(FAMILIES[family][2](at))
    except InvalidRun as exc:
        return family, at, {"invalid": exc.reasons}
    events = result.ledger.events
    report = check(events)
    rows = [
        {
            "contract": name,
            "outcome": v.outcome,
            "instance": v.instance,
            "rules": sorted({f.rule for f in v.findings}),
            "codes": sorted({f.code for f in v.findings if f.code}),
            "detail": v.findings[0].detail if v.findings else "",
        }
        for name, res in report.results.items()
        for v in res.verdicts
        if v.outcome != "pass"
    ]
    if dump and rows:
        write_jsonl(events, Path(dump) / f"{family}.{at:.3f}.jsonl")
    drift = [
        abs(c["drift_us"])
        for v in report.results["M1"].verdicts
        for c in v.measures.get("contexts", [])
    ]
    return family, at, {"rows": rows, "drift_us": max(drift, default=0)}


def spans(points, step):
    points = sorted(points)
    out, first, last = [], points[0], points[0]
    for p in points[1:]:
        if p - last > step * 1.5:
            out.append((first, last))
            first = p
        last = p
    out.append((first, last))
    return ", ".join(f"{a:.2f}" if a == b else f"{a:.2f}-{b:.2f}" for a, b in out)


if __name__ == "__main__":
    args = sys.argv[1:]
    step, dump = 0.05, None
    for flag in ("--step", "--dump"):
        if flag in args:
            at = args.index(flag)
            value = args[at + 1]
            del args[at : at + 2]
            if flag == "--step":
                step = float(value)
            else:
                dump = value
    families = args or list(FAMILIES)
    jobs = []
    for family in families:
        first, last, _build = FAMILIES[family]
        jobs += [
            (family, round(first + k * step, 3), dump)
            for k in range(round((last - first) / step) + 1)
        ]
    results = defaultdict(list)
    with scenario.pool() as pool:
        for family, at, res in pool.map(run_one, jobs, chunksize=4):
            results[family].append((at, res))

    runs = sum(len(r) for r in results.values())
    judged = ("fail", "unobservable")  # a verdict with nothing to judge is no finding
    clean = sum(
        1
        for r in results.values()
        for _at, res in r
        if "invalid" not in res and not any(row["outcome"] in judged for row in res["rows"])
    )
    print(
        f"livekit-agents {lk.__version__}: {runs} runs in {len(families)} families, step {step} s; "
        f"{clean} with no verdict failed or lost"
    )
    kinds = defaultdict(set)
    for family, family_runs in results.items():
        for at, res in family_runs:
            for row in res.get("rows", []):
                what = "/".join(row["rules"] if row["outcome"] == "fail" else row["codes"])
                kinds[(row["outcome"], row["contract"], what)].add((family, at))
    for (outcome, contract, what), where in sorted(kinds.items()):
        families_hit = len({f for f, _at in where})
        print(
            f"   {outcome:12} {contract} {what:10} {len(where):5} runs in {families_hit} families"
        )
    for family in families:
        groups = defaultdict(list)
        example: dict[tuple[str, ...], str] = {}
        drift = 0
        for at, res in results[family]:
            if "invalid" in res:
                key: tuple[str, ...] = ("invalid run", "; ".join(res["invalid"])[:120])
                groups[key].append(at)
                continue
            drift = max(drift, res["drift_us"])
            for row in res["rows"]:
                if row["outcome"] not in judged:
                    continue  # counted above, and listed only where it is a finding
                key = (
                    row["contract"],
                    row["outcome"],
                    "/".join(row["rules"]),
                    json.dumps(row["instance"], sort_keys=True),
                )
                groups[key].append(at)
                example.setdefault(key, row["detail"])
        print(f"\n{family}: {len(results[family])} runs, largest M1 drift {drift} us")
        for key, ats in sorted(groups.items()):
            print(f"   {'  '.join(key)}  x{len(ats)}  at {spans(ats, step)}")
            if example.get(key):
                print(f"       {example[key]}")
