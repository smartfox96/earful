"""Measure how far remembered text drifts from played audio, to set M1's default tolerances.

python scripts/contracts_m1_sweep.py [seconds_per_letter]
"""

import json
import sys
from collections import Counter

import livekit.agents as lk

from earful.core.checker import check
from earful.livekit.cases import BOOKING_ACK, LONG_REPLY
from earful.livekit.harness import Case, run_case
from earful.livekit.providers import Reply, UserSpeech, letters

SENTENCES = {
    "menu": LONG_REPLY,
    "booking": BOOKING_ACK,
    "long words": (
        "Unfortunately the reservation system is temporarily unavailable so I cannot "
        "confirm availability for Saturday evening right now"
    ),
    "short words": (
        "So if you can be at the bar by six or so we can put you at a big table by the door"
    ),
}
seconds_per_letter = float(sys.argv[1]) if len(sys.argv) > 1 else 0.06
STEP = 0.05


def policy_for(text):
    def policy(items):
        last = next(
            i for i in reversed(items) if i["type"] == "message" and i.get("role") == "user"
        )
        return (
            Reply(text="Okay, stopping.")
            if "Stop" in (last.get("text") or "")
            else Reply(text=text, ttft=0.1, duration=0.4)
        )

    return policy


rows = []
for name, text in SENTENCES.items():
    audio = sum(letters(w) for w in text.split()) * seconds_per_letter
    beyond_cut: Counter[int] = Counter()  # remembered - played - cut
    drifts: list[int] = []
    points = 0
    at = 3.0
    while at < 3.3 + audio:
        case = Case(
            name="m1_sweep",
            agent=lambda: lk.Agent(instructions="You are a restaurant host."),
            speeches=[
                UserSpeech(start=0.5, end=2.5, transcript="Tell me more."),
                UserSpeech(start=round(at, 3), end=round(at + 1.0, 3), transcript="Stop."),
            ],
            policy=policy_for(text),
            seconds_per_letter=seconds_per_letter,
        )
        at += STEP
        first = check(run_case(case).ledger.events).results["M1"].verdicts[0]
        measures = first.measures
        if not measures.get("contexts") or measures["played"] == measures["words"]:
            continue  # not judged, or the interruption came after the last word
        points += 1
        row = measures["contexts"][0]
        drifts.append(row["drift_us"])
        beyond_cut[row["remembered_minus_played_and_cut"]] += 1
    rows.append(
        {
            "sentence": name,
            "words": len(text.split()),
            "interruption_points": points,
            "exact": sum(1 for d in drifts if d == 0),
            "most_behind_us": -min(drifts),
            "most_ahead_us": max(drifts),
            "remembered_minus_played_and_cut": dict(sorted(beyond_cut.items())),
        }
    )

print(
    json.dumps(
        {
            "livekit-agents": lk.__version__,
            "seconds_per_letter": seconds_per_letter,
            "sentences": rows,
        },
        indent=2,
    )
)
