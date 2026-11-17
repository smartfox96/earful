# livekit/agents#3702: a tool result is lost when the reply is interrupted

Upstream issue: [livekit/agents#3702](https://github.com/livekit/agents/issues/3702),
"Tool Call Results Lost During User Interruption Leading to Duplicate Executions".
Upstream fix: [PR #6349](https://github.com/livekit/agents/pull/6349), "preserve
completed tool results in chat context on interruption", merged 13 July 2026 as
[`3ca2494`](https://github.com/livekit/agents/commit/3ca2494f166405d9910603bcebfee98f91d0d9a9).

Classification: framework defect. The maintainers accepted the report and merged a fix.

## Result

| livekit-agents | M2 rule d | A2 | Replay of the bundle |
| --- | --- | --- | --- |
| 1.5.6 | fail | fail | reproduced; ledger identical to the recorded one |
| 1.5.7 | fail | fail | reproduced |
| 1.6.5 | fail | fail | reproduced |
| 1.6.6 | pass | pass | not reproduced |
| 1.8.12 | pass | pass | not reproduced |

Every row is the same case run through the same checker. `src/earful/core/` contains no
reference to this issue.

## Reproduce

Requires [uv](https://docs.astral.sh/uv/).

```bash
scripts/make_env.sh 1.5.6 2026-04-30
.venvs/lk-1.5.6/bin/earful replay tests/bundles/lk-1.5.6.book_then_interrupt.A2-rule --expect reproduced

scripts/make_env.sh 1.6.6 2026-07-20
.venvs/lk-1.6.6/bin/earful replay tests/bundles/lk-1.5.6.book_then_interrupt.A2-rule --expect clean
```

`make_env.sh` installs the release with its dependencies resolved as of the given date
and writes the resulting set to `compat/lk-<version>.lock.txt`. `earful replay` runs the case
three times and exits 0 when the outcome matches `--expect`. The first command prints
`breaks the rule       yes` and `the ledger recorded   yes`. The second prints
`breaks the rule       no`.

To print every verdict for the recorded run:

```bash
.venvs/lk-1.5.6/bin/earful check tests/bundles/lk-1.5.6.book_then_interrupt.A2-rule/minimal.ledger.jsonl
```

## The case

`earful.livekit.cases:book_then_interrupt`, seed 0, default contract policy.

- **Agent.** One tool, `create_reservation`. It writes one reservation to the backend and
  returns `"Reservation <receipt> confirmed for 6 guests."`.
- **Scripted model.** If the context holds a tool result, it says "Your table is booked."
  Otherwise, if the user asked for a table, it says a 5.2 s acknowledgement and calls
  `create_reservation`.
- **User.** "Book a table for six tonight." from 0.5 s to 2.5 s. "Is the table by the
  window?" from 5.0 s to 6.0 s.
- **Backend.** Every write commits. The reservation is one intent with one authorized
  effect.
- **Session.** VAD endpointing, 0.5 s minimum interruption duration, 0.5 s minimum
  endpointing delay.

## What the ledger shows on 1.5.6

| Time (s) | `seq` | Event |
| --- | --- | --- |
| 2.700 | 10, 11 | Model call 1. The reply is the acknowledgement plus a `create_reservation` call |
| 3.000 | 16 to 19 | The tool is called, the backend commits reservation R1, the tool returns |
| 3.200 | 21 | The acknowledgement starts playing |
| 5.500 | 27 | The second speech has lasted 0.5 s. The framework pauses the output |
| 6.200 | 32, 33 | The transcript arrives. The framework clears the output, 2.3 s into the reply |
| 6.500 | 39 | Model call 2. Its context holds the first user turn, the interrupted reply and the second user turn. It holds no call item and no result for `call-1-1` |
| 6.800 | 43 to 46 | The model calls the tool again. The backend commits reservation R2 |
| 18.200 | 64 | The final chat history also holds nothing for `call-1-1` |

The checker reports:

- **M2 rule d**, for model call 2, model call 3 and the final history:
  `create_reservation (call-1-1) finished before this context, which holds 0 call item(s) and 0 result item(s) for it`
- **A2**: `2 durable effects for 'book-table-1'; the scenario authorized 1`

On 1.6.6 the context of model call 2 holds the call and its result
(`Reservation R1 confirmed for 6 guests.`). The model says "Your table is booked." and the
backend holds one reservation.

## Which part is the framework's

M2 rule d is the framework's behaviour. A tool returned, and the next model call was shown
neither the call nor its result.

A2 also depends on the model. The scripted model books again because it is shown no
result. A model that did not book again would leave one reservation, and M2 rule d would
still fail.

## How much timing room the failure has

On 1.5.6, 1.5.7 and 1.6.5 the case fails for any start of the second speech from 3.000 s
to 7.859 s. In that range the speech interrupts the reply that made the tool call. The
first speech can start anywhere from 0 s to 2.5 s. Both ranges are in the bundle's
`manifest.json` under `windows`.

## Evidence

- Bundle: `tests/bundles/lk-1.5.6.book_then_interrupt.A2-rule/`.
  `minimal.case.json` is the case. `minimal.ledger.jsonl` is every event of the recorded
  run: the scripted user, the framework's own events, the sink and the backend.
  `manifest.json` holds the frozen rule, the windows and the environment it was made in.
- Recorded ledger hash on 1.5.6:
  `17cb49659910a6674810baa7ec8e43de59ee156ec414690fe07defcd2949d614`. A replay on 1.5.6
  gives this hash. CI checks that on Linux and macOS.
- Tests: `tests/test_historical_cases.py`, `tests/test_search_live.py`. CI runs both and
  replays the bundle on all five releases.

## Limits

- The VAD, STT, LLM and TTS are scripted. The `AgentSession` is the real one, run in
  process on a virtual clock.
- "Played" means consumed by Earful's audio sink. The audio never leaves the process.
- The run has not been compared with a run on a real clock.
- The case produces the behaviour the issue describes. It does not use the reporter's
  agent.
- Five releases were tested. 1.6.6 is the first of them that passes.
