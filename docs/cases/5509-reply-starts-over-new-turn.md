# livekit/agents#5509: a reply starts playing after the user has begun a new turn

Upstream issue: [livekit/agents#5509](https://github.com/livekit/agents/issues/5509),
"Agent transitions `thinking → speaking` after user has already transitioned to
`speaking` (stale reply plays over new user turn)".
Upstream fix: [PR #5535](https://github.com/livekit/agents/pull/5535), "pause output when
user starts speaking during thinking", merged 24 April 2026 as
[`63fc7fc`](https://github.com/livekit/agents/commit/63fc7fc7a63721c0700fc3c59890ba77d4cee009).

Classification: framework defect. The maintainers accepted the report and merged a fix.

## Result

| livekit-agents | L2 rule b | Replay of the bundle |
| --- | --- | --- |
| 1.5.6 | fail | reproduced; ledger identical to the recorded one |
| 1.5.7 | pass | not reproduced |
| 1.6.5 | pass | not reproduced |
| 1.6.6 | pass | not reproduced |
| 1.8.12 | pass | not reproduced |
| 1.8.13 | pass | not reproduced |

Every row is the same case run through the same checker. `src/earful/core/` contains no
reference to this issue.

## Reproduce

Requires [uv](https://docs.astral.sh/uv/).

```bash
scripts/make_env.sh 1.5.6 2026-04-30
.venvs/lk-1.5.6/bin/earful replay tests/bundles/lk-1.5.6.speak_over_new_turn.L2-rule-b --expect reproduced

scripts/make_env.sh 1.5.7 2026-05-06
.venvs/lk-1.5.7/bin/earful replay tests/bundles/lk-1.5.6.speak_over_new_turn.L2-rule-b --expect clean
```

`make_env.sh` installs the release with its dependencies resolved as of the given date
in UTC. Set `EARFUL_LOCK=write` to record the result in `compat/lk-<version>.lock.txt`. `earful replay` runs the case
three times and exits 0 when the outcome matches `--expect`. The first command prints
`breaks the rule       yes` and `the ledger recorded   yes`. The second prints
`breaks the rule       no`.

To print every verdict for the recorded run:

```bash
.venvs/lk-1.5.6/bin/earful check tests/bundles/lk-1.5.6.speak_over_new_turn.L2-rule-b/minimal.ledger.jsonl
```

## The case

`earful.livekit.cases:speak_over_new_turn`, seed 0, default contract policy.

- **Agent.** No tools.
- **Scripted model.** Asked about the menu, it takes 0.6 s to generate a 7.4 s reply.
  Asked about dessert, it gives a one-sentence answer.
- **User.** "Tell me about the menu." from 0.5 s to 2.5 s. "Actually what about dessert?"
  from 3.2 s to 4.6 s.
- **Session.** VAD endpointing, 0.5 s minimum interruption duration, 0.5 s minimum
  endpointing delay. The scripted VAD reports speech 50 ms after it starts. The scripted
  voice delivers its first audio 0.5 s after its first complete word arrives.

## What the ledger shows on 1.5.6

| Time (s) | `seq` | Event |
| --- | --- | --- |
| 2.700 | 10, 11 | Model call 1 for the menu question |
| 3.250 | 16 | The VAD reports the second speech. The user holds the floor from here |
| 3.518 | 19 | The reply to the menu question starts playing |
| 3.700 | 22, 23 | The second speech has lasted 0.5 s. The framework pauses the output, 0.182 s into the reply |
| 4.800 | 26, 28, 29 | The transcript arrives. The framework clears the output |

The checker reports:

- **L2 rule b**, for the second speech:
  `segment 1 started 268000 us after the user took the floor`

On 1.5.7 the framework pauses the output at 3.250 s, when the VAD reports the second
speech. The reply never becomes audible. Model call 2 is shown the two user turns and no
assistant message.

## How much timing room the failure has

On 1.5.6 the case fails for any start of the second speech from 3.019 s to 3.467 s. In
that range the user begins speaking after the VAD has reported the end of the first
speech at 3.0 s, and the VAD reports the new speech before the reply becomes audible at
3.518 s. The first speech can start anywhere from 0.233 s to 0.681 s. Both ranges are in the
bundle's `manifest.json` under `windows`.

## On the real clock

The reduced case was also run on the real clock, with nothing patched, three times on
the release that has the defect and three times on the first release that passes
(`docs/testing.md`, section 10). 18 November 2026, macOS, Python 3.13.

```bash
.venvs/lk-1.5.6/bin/earful crosscheck tests/bundles/lk-1.5.6.speak_over_new_turn.L2-rule-b
.venvs/lk-1.5.7/bin/earful crosscheck tests/bundles/lk-1.5.6.speak_over_new_turn.L2-rule-b
```

| livekit-agents | Virtual clock | Real clock, 3 runs | Largest drift |
| --- | --- | --- | --- |
| 1.5.6 | breaks L2 rule b | 3 of 3 break L2 rule b and agree with the virtual run | 39.8, 38.6, 38.4 ms |
| 1.5.7 | breaks nothing | 3 of 3 break nothing and agree with the virtual run | 34.3, 34.9, 35.8 ms |

A real-clock run agrees when the same events happen in the same order, each within
100 ms of its virtual time, each segment plays the same words give or take one where it
was cut short, every model call is shown the same context and the same rules break. The
failure has 448 ms of room in the start of the second speech, which is more than ten
times the drift. A real-clock run is not repeatable, and none of these six is evidence
on its own.

## Evidence

- Bundle: `tests/bundles/lk-1.5.6.speak_over_new_turn.L2-rule-b/`.
  `minimal.case.json` is the case. `minimal.ledger.jsonl` is every event of the recorded
  run: the scripted user, the framework's own events, the sink and the backend.
  `manifest.json` holds the frozen rule, the windows and the environment it was made in.
- Recorded ledger hash on 1.5.6:
  `5e0e140d97ad5e36271fc652470a677099b5ece35a5ea508c7ecd224a11b6875`. A replay on 1.5.6
  gives this hash. CI checks that on Linux and macOS.
- Tests: `tests/test_historical_cases.py`, `tests/test_search_live.py`. CI runs both and
  replays the bundle on all six releases.

## Limits

- The VAD, STT, LLM and TTS are scripted. The `AgentSession` is the real one, run in
  process on a virtual clock.
- "Started playing" means Earful's audio sink consumed the first sample. The audio never
  leaves the process.
- The real-clock runs remove the virtual clock and nothing else. The providers, the sink
  and the backend are the same scripted ones.
- The case produces the behaviour the issue describes. It does not use the reporter's
  agent.
- The case runs on the six supported releases. 1.5.7 is the first of them that passes.
