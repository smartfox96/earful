"""Spike: does scripted timing expose livekit/agents#5509?

Reports every playout that started while the scripted user was speaking, and which words
the sink consumed during that time.
"""
import json
import sys
from pathlib import Path

from earful.core.ledger import write_jsonl
from earful.livekit.cases import CASES
from earful.livekit.harness import run_case

can_pause = "--no-pause" not in sys.argv
out_dir = next((a for a in sys.argv[1:] if not a.startswith("--")), None)


def make():
    case = CASES["speak_over_new_turn"]()
    case.sink_can_pause = can_pause
    return case


runs = [run_case(make(), seed=0) for _ in range(20)]
result = runs[0]
ledger = result.ledger

# Windows in which the scripted user is speaking, from Earful's own "world" record.
windows, open_at = [], None
for e in ledger.of("world"):
    if e["kind"] == "user.vad_start":
        open_at = e
    elif e["kind"] == "user.vad_end" and open_at is not None:
        windows.append((open_at, e))
        open_at = None

overlaps = []
for start in ledger.of("sink", "playout.started"):
    for begin, end in windows:
        if begin["seq"] < start["seq"] < end["seq"]:
            finish = next(
                f for f in ledger.of("sink", "playout.finished")
                if f["data"]["segment"] == start["data"]["segment"]
            )
            overlaps.append({
                "user_speech": begin["data"]["speech"],
                "user_speaking_since": begin["t_us"] / 1e6,
                "playout_started": start["t_us"] / 1e6,
                "started_after_user_by_ms": round((start["t_us"] - begin["t_us"]) / 1000),
                "audible_for_ms": round(finish["data"]["position_us"] / 1000),
                "words_played_over_user": finish["data"]["text_played"],
            })

summary = {
    "versions": result.versions,
    "sink_can_pause": can_pause,
    "playouts_started_while_user_speaking": overlaps,
    "verdict_no_playout_starts_over_user": "FAIL" if overlaps else "pass",
    "agent_states": [
        f'{e["t_us"] / 1e6:.2f} {e["data"]["old"]}->{e["data"]["new"]}'
        for e in ledger.of("framework", "fw.agent_state_changed")
    ],
    "repeat_runs": len(runs),
    "distinct_hashes": len({r.hash for r in runs}),
    "framework_warnings": result.framework_warnings,
}
print(json.dumps(summary, indent=2))
if out_dir:
    tag = f"lk-{result.versions['livekit-agents']}.speak_over_new_turn{'' if can_pause else '.no_pause'}"
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{tag}.summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    write_jsonl(ledger.events, out / f"{tag}.ledger.normalized.jsonl", normalized=True)
