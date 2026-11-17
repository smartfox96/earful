# Gate 0: can a real LiveKit AgentSession be run deterministically?

> **Historical record.** Everything below was produced with ledger schema
> `earful.ledger/0` and is reproduced exactly by commit `70bbc4c`
> (`git checkout 70bbc4c && scripts/gate0_all.sh`). Later commits record schema 1
> (`docs/ledger.md`), rename the scripts and keep current golden ledgers in `tests/golden/`,
> so hashes and evidence here no longer match current output. Two limits listed below are
> addressed since: time is integer microseconds throughout, with same-instant timers fired
> in scheduling order, and `compare_view` makes ledgers comparable across releases.

Status: **passed.** 14 November 2026. Re-verified 15 November 2026 with every case under
the full repeat and with CI asserting the recorded hashes.

## Exit criteria

Gate 0 is complete when each of these is demonstrated.

| Criterion | Where it is shown |
| --- | --- |
| The same scenario and seed give identical normalized ledgers on repeated runs | Result; `scripts/gate0_repeat.py` |
| A full playout and memory event path is observable | Sample trace; `sink` and `llm` events in every ledger |
| A public technical note states the exact versions, commands, sample trace, repeatability result, raw output and limits | This document and `docs/gate0/evidence/` |
| If threads, timers or room requirements escape control, the supported boundary is stated | Supported boundary |

## Result

Yes, within a stated boundary. A real, unmodified `AgentSession` from five LiveKit Agents
releases runs in-process against scripted providers on a virtual clock, and produces a
byte-identical normalized event ledger on every run.

| livekit-agents | Cases | Runs per case (in-process + subprocess) | Distinct ledger hashes per case | Version-specific code needed |
| --- | --- | --- | --- | --- |
| 1.5.6 | 3 | 100 + 20 | 1 | none |
| 1.5.7 | 3 | 100 + 20 | 1 | none |
| 1.6.5 | 3 | 100 + 20 | 1 | none |
| 1.6.6 | 3 | 100 + 20 | 1 | none |
| 1.8.12 | 3 | 100 + 20 | 1 | none |

The three cases are `barge_in_mid_word`, `book_then_interrupt` (a tool call with a durable
effect) and `speak_over_new_turn` (overlapping turns). Each subprocess run used a
different `PYTHONHASHSEED`. One hundred runs of an 11-second conversation take about 2.2
seconds of wall time.

For every case, the hash for each release is the same on macOS arm64 and Linux x86_64,
and on Python 3.10, 3.12 and 3.13. CI asserts it: each of the 12 jobs fails unless every
case's hash equals the SHA-256 of the ledger committed in `docs/gate0/evidence/`, which
was recorded on macOS arm64. All 12 jobs pass.

Negative control: the same case on the real clock, with nothing patched, gave 3 distinct
hashes in 3 runs, with the same 43 events in the same order. The hash is sensitive to
timing, and the virtual clock makes it stable. CI runs this control on one job and
fails if the real clock does not change the hash.

Beyond the gate's requirement, two historical LiveKit defects already reproduce with
scripted timing alone:

| Case | Affected release | Repaired release | Evidence |
| --- | --- | --- | --- |
| [#3702](https://github.com/livekit/agents/issues/3702) lost tool result, duplicate booking | 1.6.5: 2 reservations for one request; model call 2 is shown no tool result | 1.6.6: 1 reservation; tool result visible | backend record + contexts captured by the scripted model |
| [#5509](https://github.com/livekit/agents/issues/5509) stale reply plays over a new user turn | 1.5.6: playout starts 250 ms after the user began speaking, audible for 200 ms | 1.5.7: no playout starts | sink record + scripted user record |

The same case definitions and the same ledger queries run on both sides of each fix. No
framework method is patched or wrapped to reach either failure.

## Environment

- macOS 27.2 on arm64, Python 3.13.12, uv 0.11.2.
- `livekit-agents` 1.5.6, 1.5.7, 1.6.5, 1.6.6, 1.8.12 from PyPI. Exact dependency sets
  are in `compat/lk-<version>.lock.txt`.
- CI (`.github/workflows/ci.yml`) repeats the tests, the 100 + 20 runs of every case and
  the two historical cases on `ubuntu-latest` (x86_64; Python 3.10, 3.12, 3.13) and
  `macos-latest` (arm64).

Old releases do not install cleanly against today's package index: 1.6.5 imports `httpx`
without declaring it, and a fresh resolve no longer pulls it in. `scripts/make_env.sh`
resolves dependencies as of a date shortly after each release.

## Commands

```bash
scripts/gate0_all.sh                                      # everything below, all five releases

scripts/make_env.sh 1.6.5 2026-07-20                      # one pinned environment
.venvs/lk-1.6.5/bin/python -m pytest -q                   # 8 tests
.venvs/lk-1.6.5/bin/python scripts/gate0_repeat.py --check docs/gate0/evidence
                                                          # 100 + 20 runs of every case; hashes must
                                                          # agree with each other and with the record
.venvs/lk-1.6.5/bin/python scripts/gate0_realtime_control.py 3   # negative control (~35 s)
.venvs/lk-1.6.5/bin/python scripts/gate0_3702_spike.py    # run on 1.6.5, then on 1.6.6
.venvs/lk-1.5.6/bin/python scripts/gate0_5509_spike.py    # run on 1.5.6, then on 1.5.7
.venvs/lk-1.6.5/bin/python scripts/show_run.py barge_in_mid_word   # print one ledger
```

Raw output is in `docs/gate0/evidence/`. A normalized ledger's hash can be checked with
standard tools: `shasum -a 256 <file>.ledger.normalized.jsonl` equals `reference_hash` in
the matching `.repeat.json`. Reproducing a result rewrites these files byte for byte, so
`git status` stays clean after `scripts/gate0_all.sh`.

## Sample trace

`barge_in_mid_word` on 1.6.5: a user turn, an 8.7-second reply, and an interruption.
Selected events (time in virtual seconds, then who observed it):

```text
 0.550  world      user.vad_start            speech 0
 2.700  world      user.transcript_final     "Tell me about the menu."
 2.700  llm        llm.request               call 1 (the exact context shown to the model)
 3.100  world      tts.synthesized           29 words, stamps 1-29, 0.3 s each
 3.300  sink       playout.started           segment 1
 3.300  framework  agent_state_changed       thinking -> speaking
 5.050  world      user.vad_start            speech 1
 5.500  sink       playout.paused            2.200 s played
 5.500  framework  agent_state_changed       speaking -> listening
 6.200  world      user.transcript_final     "Stop."
 6.200  sink       playout.finished          interrupted, words 1-7 played, word 8 cut
 6.200  framework  conversation_item_added   assistant, interrupted:
                                             "Tonight we have a roasted squash soup to"
 6.500  llm        llm.request               call 2
 7.000  sink       playout.started           segment 2 ("Okay, stopping.")
```

The sink consumed seven whole words and one third of the eighth ("to"). The framework's
chat history kept all eight. Contract M1 (played vs. remembered) rules on that one-word
difference, so its tolerance and cut-word rule need to be explicit.

## How it works

- **Clock.** `earful/livekit/clock.py` runs the session on an event loop whose clock
  jumps to the next timer instead of sleeping. It points `time.time`, `time.perf_counter`
  and `time.time_ns` at that clock for the duration of a run, and rewrites the pydantic
  validators of event and chat-context models whose `created_at` captured `time.time` at
  import (19 fields on 1.5.x, 23 on 1.6.x, 24 on 1.8.12). Adapted from LiveKit's own
  `tests/virtual_time.py`, which exists only from 1.6.0 and is not shipped in the wheel.
- **Providers.** Scripted VAD, STT, LLM and TTS subclass LiveKit's public plugin base
  classes. The LLM is a pure function of the context it is shown and records that context.
- **Audio.** The TTS writes each word's stamp into every PCM sample of that word. The sink
  reads stamps back from the frames it consumes. Across all runs, zero frames arrived
  altered.
- **Sink.** An `AudioOutput` subclass assigned to `session.output.audio`. It models
  playout on the virtual clock, including pause, resume, flush and clear.
- **Ledger.** One JSON object per event, tagged with its observer: `world`, `llm`, `sink`,
  `backend`, `framework` or `harness`. Normalization replaces random IDs with
  first-appearance ordinals and sorts keys; the hash is SHA-256 over those lines.

Nothing inside `AgentSession` or `AgentActivity` is patched, wrapped or subclassed.

## Supported boundary

Deterministic, as demonstrated:

- Cascaded STT-LLM-TTS pipeline in one process, no room and no network.
- Scripted providers that emit raw PCM.
- VAD-based endpointing and interruption (`turn_detection=None`).
- Async function tools that do no real I/O.

Not covered, and expected to escape control until shown otherwise:

- The model-based turn detector, real VAD inference and compressed TTS audio. Each runs
  work on a thread.
- Session recording (`recorder_io` starts a thread) and room I/O (native WebRTC).
- Realtime speech-to-speech models.
- Tools that block or call real services: the virtual clock does not see their latency.
- Any clock read the patch does not reach. None was observed, but nothing yet proves
  there is none; a per-version scan is Gate 1 work.

A run counts executor hand-offs and altered audio frames, and both were zero in every run
here. A non-zero count means the run left the boundary and must not be labeled
deterministic.

## Limits of this result

- **Two platforms.** macOS arm64 and Linux x86_64. Windows has not been run.
- **Three short cases.** Determinism of longer or more concurrent conversations is not
  established.
- **The sink is a model.** Its playout semantics follow LiveKit's test sink. The real
  room output may differ, so verdicts that depend on pause or clear timing need a
  real-time cross-check.
- **The TTS is not incremental.** It emits a whole reply once the model has finished,
  where real providers stream.
- **Same-instant ordering is fixed.** Timers due at one instant fire in scheduling order.
  Nothing here permutes them.
- **Ledgers are not comparable across releases at full precision.** Framework timestamps
  differ by a few microseconds between releases because each release reads the clock a
  different number of times per step. Within a release they are exact. Cross-release
  comparison needs a coarser projection.
- **The spike checks are ledger queries.** They show the evidence each case turns on. The
  contract checker, with its policies, tolerances and `unobservable` verdict, does not
  exist yet.
- **A duplicate booking depends on the model policy.** The scripted model books whenever
  it sees a booking request and no tool result. The missing tool result is unconditional;
  the second booking follows only under that policy.

## Findings to carry forward

1. **The #5509 repair only protects outputs that can pause.** With a sink that reports
   `pause=False`, the stale reply still starts on 1.5.7 and on 1.8.12. LiveKit's avatar
   outputs declare `pause=False`. This is an unverified lead: it has not been run
   against those real output classes, and it may be an accepted limitation.
2. **Session close takes 2.0 virtual seconds longer on 1.5.x and 1.8.12 than on 1.6.x.**
   Conversation events are the same. Unexplained, low priority.
3. **Dependency drift is a maintenance cost.** Every pinned historical release needs a
   dated resolve, and that date belongs in the audit package.
