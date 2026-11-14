# Gate 0: can a real LiveKit AgentSession be run deterministically?

Status: **passed on macOS, Linux run pending.** 14 November 2026.

## Result

Yes, within a stated boundary. A real, unmodified `AgentSession` from five LiveKit Agents
releases runs in-process against scripted providers on a virtual clock, and produces a
byte-identical normalized event ledger on every run.

| livekit-agents | Runs (in-process + subprocess) | Distinct ledger hashes | Version-specific code needed |
| --- | --- | --- | --- |
| 1.5.6 | 100 + 20 | 1 | none |
| 1.5.7 | 100 + 20 | 1 | none |
| 1.6.5 | 100 + 20 | 1 | none |
| 1.6.6 | 100 + 20 | 1 | none |
| 1.8.12 | 100 + 20 | 1 | none |

Each subprocess run used a different `PYTHONHASHSEED`. One hundred runs of an 11-second
conversation take about 2.2 seconds of wall time.

Negative control: the same case on the real clock, with nothing patched, gave 3 distinct
hashes in 3 runs, with the same 43 events in the same order. The hash is sensitive to
timing; the virtual clock is what makes it stable.

Two things went beyond the gate's requirement. Both historical defects the project brief
names already reproduce, using scripted timing alone:

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
- Linux has not been run yet. `.github/workflows/ci.yml` runs the same checks on
  `ubuntu-latest` and `macos-latest` once the repository is pushed.

Old releases do not install cleanly against today's package index: 1.6.5 imports `httpx`
without declaring it, and a fresh resolve no longer pulls it in. `scripts/make_env.sh`
resolves dependencies as of a date shortly after each release.

## Commands

```bash
scripts/gate0_all.sh                                      # everything below, all five releases

scripts/make_env.sh 1.6.5 2026-07-20                      # one pinned environment
.venvs/lk-1.6.5/bin/python -m pytest -q                   # 8 tests
.venvs/lk-1.6.5/bin/python scripts/gate0_repeat.py        # 100 + 20 runs, compare hashes
.venvs/lk-1.6.5/bin/python scripts/gate0_realtime_control.py 3   # negative control (~35 s)
.venvs/lk-1.6.5/bin/python scripts/gate0_3702_spike.py    # run on 1.6.5, then on 1.6.6
.venvs/lk-1.5.6/bin/python scripts/gate0_5509_spike.py    # run on 1.5.6, then on 1.5.7
.venvs/lk-1.6.5/bin/python scripts/show_run.py barge_in_mid_word   # print one ledger
```

Raw output is in `docs/gate0/evidence/`. A normalized ledger's hash can be checked with
standard tools: `shasum -a 256 <file>.ledger.normalized.jsonl` equals `reference_hash` in
the matching summary.

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
chat history kept all eight. That one-word difference is exactly what contract M1 (played
vs. remembered) has to rule on, so the tolerance and cut-word rule need to be explicit.

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
  altered, so the audio path carries ground truth end to end.
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

- **One platform so far.** macOS arm64, Python 3.13. Hashes across operating systems and
  Python versions have not been compared.
- **Three short cases.** Determinism of longer or more concurrent conversations is not
  established.
- **The sink is a model.** Its playout semantics follow LiveKit's test sink, not the real
  room output. Verdicts that depend on pause or clear timing need a real-time cross-check.
- **The TTS is not incremental.** It emits a whole reply once the model has finished,
  where real providers stream.
- **Same-instant ordering is fixed, not explored.** Timers due at one instant fire in
  scheduling order. Nothing here permutes them.
- **Ledgers are not comparable across releases at full precision.** Framework timestamps
  differ by a few microseconds between releases because each release reads the clock a
  different number of times per step. Within a release they are exact. Cross-release
  comparison needs a coarser projection.
- **The spike checks are queries, not the contract checker.** They show the evidence each
  case turns on. Policies, tolerances and the `unobservable` verdict do not exist yet.
- **A duplicate booking depends on the model policy.** The scripted model books whenever
  it sees a booking request and no tool result. The missing tool result is unconditional;
  the second booking follows only under that policy.

## Findings to carry forward

1. **The #5509 repair only protects outputs that can pause.** With a sink that reports
   `pause=False`, the stale reply still starts on 1.5.7 and on 1.8.12. LiveKit's avatar
   outputs declare `pause=False`. This is a lead, not a finding: it has not been run
   against those real output classes, and it may be an accepted limitation.
2. **Session close takes 2.0 virtual seconds longer on 1.5.x and 1.8.12 than on 1.6.x.**
   Conversation events are the same. Unexplained, low priority.
3. **Dependency drift is a maintenance cost.** Every pinned historical release needs a
   dated resolve, and that date belongs in the audit package.
