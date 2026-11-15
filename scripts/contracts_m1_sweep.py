"""How far does remembered text run ahead of played audio?

Interrupts one long reply at many points and compares, for each point, the words the sink
fully consumed with the words the framework kept in its chat history. The result sets the
default tolerance of contract M1 in docs/contracts.md.

    python scripts/contracts_m1_sweep.py [seconds_per_word]
"""
import json
import sys
from collections import Counter

import livekit.agents as lk

from earful.livekit.cases import LONG_REPLY, _barge_in_policy
from earful.livekit.harness import Case, run_case
from earful.livekit.providers import UserSpeech

seconds_per_word = float(sys.argv[1]) if len(sys.argv) > 1 else 0.3
reply = LONG_REPLY.split()
reply_starts, step = 3.3, 0.05

beyond_cut: Counter[int] = Counter()  # remembered - (fully played + cut word)
below_played = not_remembered = points = 0
at = 3.0
while at < reply_starts + len(reply) * seconds_per_word:
    case = Case(
        name="m1_sweep",
        agent=lambda backend: lk.Agent(instructions="You are a restaurant host."),
        speeches=[
            UserSpeech(start=0.5, end=2.5, transcript="Tell me about the menu."),
            UserSpeech(start=round(at, 3), end=round(at + 1.0, 3), transcript="Stop."),
        ],
        policy=_barge_in_policy,
        seconds_per_word=seconds_per_word,
    )
    events = run_case(case).ledger.events
    at += step
    cut_short = [
        e for e in events if e["kind"] == "playout.finished" and e["data"]["interrupted"]
    ]
    if not cut_short:
        continue  # the interruption arrived after the reply had finished
    points += 1
    played = len(cut_short[0]["data"]["words_played"])
    cut = 1 if cut_short[0]["data"]["word_cut"] else 0
    history = next(e for e in events if e["kind"] == "history.final")["data"]["items"]
    kept = None
    for item in history:
        words = (item.get("text") or "").split()
        if item.get("role") == "assistant" and words and words == reply[: len(words)]:
            kept = len(words)
            break
    if kept is None:
        not_remembered += 1
        if played:
            below_played += 1
        continue
    if kept < played:
        below_played += 1
    beyond_cut[kept - played - cut] += 1

print(
    json.dumps(
        {
            "livekit-agents": lk.__version__,
            "seconds_per_word": seconds_per_word,
            "interruption_points": points,
            "remembered_minus_played_and_cut": dict(sorted(beyond_cut.items())),
            "reply_absent_from_memory": not_remembered,
            "remembered_fewer_than_fully_played": below_played,
        },
        indent=2,
    )
)
