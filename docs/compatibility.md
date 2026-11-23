# Compatibility

What Earful is tested with. A combination that is not in the tables below may work and
is not claimed to.

## livekit-agents

Six releases are supported. Each is installed with its dependencies resolved as of a
date a few days after it came out, which gives the set the release was tested with at the
time (`scripts/make_env.sh`). The date runs to the end of that day in UTC. The set each
release resolves to on Python 3.13 is recorded in `compat/`, and CI fails if an
environment it builds on Python 3.13 holds anything else.

| livekit-agents | Dependencies as of | Lock file | #3702 | #5509 |
| --- | --- | --- | --- | --- |
| 1.5.6 | 2026-04-30 | `compat/lk-1.5.6.lock.txt` | present | present |
| 1.5.7 | 2026-05-06 | `compat/lk-1.5.7.lock.txt` | present | repaired |
| 1.6.5 | 2026-07-20 | `compat/lk-1.6.5.lock.txt` | present | repaired |
| 1.6.6 | 2026-07-20 | `compat/lk-1.6.6.lock.txt` | repaired | repaired |
| 1.8.12 | 2026-11-14 | `compat/lk-1.8.12.lock.txt` | repaired | repaired |
| 1.8.13 | 2026-11-20 | `compat/lk-1.8.13.lock.txt` | repaired | repaired |

The last two columns say whether the release has the two known defects Earful reproduces
(`docs/cases/`). A search of your own agent on an affected release can meet them.

`pip install "earful[livekit]"` accepts livekit-agents from 1.5.6 up to, and not
including, 1.9. A release between two rows, or a later 1.8 release, is accepted and not
tested, except the newest one, in part ("What runs on the newest releases" below).
`earful.livekit` warns once when it is imported on such a release, and refuses to import
on a release outside the range. The warning's category is
`earful.scripted.releases.UntestedRelease`.

On any release Earful checks two things in every run, and a run that fails either is
invalid and yields no verdict: every tool the model is offered is one whose calls are
recorded, and every pydantic field whose default is a clock read was given the virtual
clock. A release that reads time some other way is not caught by these checks. The reads
that are virtual are listed under The virtual clock.

A tool that returns an agent hands the session over on every release above. The scripted
user and the tool witness follow the session to that agent, and the contracts are judged
across the handoff ([`contracts.md`](contracts.md#m2-turn-integrity), rule d).

## pipecat-ai

Supported pipecat-ai series: 1.16. The table gives the release and resolution date
used by `scripts/make_pipecat_env.sh`.

| pipecat-ai | Dependencies as of | Lock file |
| --- | --- | --- |
| 1.16.0 | 2026-11-18 | `compat/pc-1.16.0.lock.txt` |

`pip install "earful[pipecat]"` accepts pipecat-ai from 1.16 up to, and not
including, 1.17, on Python 3.11 or later. pipecat-ai does not support Python 3.10, and
there the extra installs Earful alone. `earful.pipecat` warns once when it is imported on
a 1.16 release other than 1.16.0, and refuses to import on a release outside the range.

The adapter uses Pipecat's VAD turn-start and speech-timeout turn-stop strategies.
The scripted model reads three private names of `LLMService`: `_functions`,
`_cancel_tool_names` and `_sync_registered_tool_handlers`. A release that keeps one of
them elsewhere is refused in one line before a run.
[Writing a Pipecat scenario](pipecat.md)

## The options Earful models on LiveKit

A run refuses, in one line before it starts, an option it does not model. The list is
closed: on a release whose `AgentSession` or `Agent` takes an option that is not below,
`tests/test_livekit_options.py` fails, and the nightly canary runs it on the newest
release.

`AgentSession` options. Earful sets those of the first six rows, `session_kwargs` may hold
those of the seventh, and every other option is refused.

| Option | What Earful does | In the header |
| --- | --- | --- |
| `vad`, `stt`, `llm`, `tts` | Earful's scripted providers | `providers` |
| `turn_handling` | Built from the `Case` fields of the next three rows | `session` |
| `min_endpointing_delay`, `max_endpointing_delay` | The `Case` fields of those names | `session.min_endpointing_delay_us`, `session.max_endpointing_delay_us` |
| `min_interruption_duration`, `false_interruption_timeout` | The `Case` fields of those names | `session.min_interruption_duration_us`, `session.false_interruption_timeout_us` |
| `allow_interruptions`, `preemptive_generation` | The `Case` fields of those names | `session.extra_options`, when false |
| `turn_detection` | The framework's default on the scripted VAD | `session.turn_detection`, null |
| `tools`, `max_tool_steps`, `userdata`, `min_consecutive_speech_delay`, `user_away_timeout`, `session_close_transcript_timeout`, `transcription_timeout`, `tts_text_transforms` | Passed as they are | `session.extra_options` |
| `aec_warmup_duration` | Refused, and off: it has the framework ignore the user while echo cancellation warms up, and a run has no echo | |
| `use_tts_aligned_transcript` | Refused: the scripted voice gives no word timings | |
| `mcp_servers` | Refused: it reaches a server over the network | |
| `video_sampler` | Refused: a run has no video | |
| `ivr_detection` | Refused: a phone menu is not modelled | |
| `conn_options` | Refused: retries and timeouts of the providers are not modelled | |
| `loop` | Refused: the loop is Earful's virtual clock | |
| `stt_context_options`, `keyterms_options` | Refused: hints for a real speech-to-text | |
| `tool_handling` | Refused: asynchronous tools are not modelled | |
| `expressive` | Refused: it needs a real voice | |
| `discard_audio_if_uninterruptible`, `min_interruption_words`, `resume_false_interruption`, `agent_false_interruption_timeout` | Refused: deprecated, and ignored by livekit-agents once `turn_handling` is given, as Earful gives it | |

`Agent` options. An agent that becomes active during a run, by a handover or as a task,
is held to the same rules, and one that breaks them makes the run invalid.

| Option | What Earful does |
| --- | --- |
| `instructions`, `id`, `chat_ctx`, `tools`, `min_consecutive_speech_delay` | Run as written. Each tool is witnessed |
| `allow_interruptions`, and `enabled` of the interruption and of preemptive generation in `turn_handling` | Run as written. L2 holds the agent to its policy `interruptions_allowed` |
| `min_endpointing_delay`, `max_endpointing_delay`, and `min_duration` and `false_interruption_timeout` of the interruption in `turn_handling` | Must equal the `Case` field the header states |
| `turn_detection` | `None` or `"vad"`, which run as the session's default does |
| Any other key of `turn_handling` | Must equal what the session holds, such as the endpointing mode `"fixed"` and the interruption mode `"vad"` |
| `stt`, `vad`, `llm`, `tts` | Refused: the agent's own would replace Earful's scripted providers |
| `mcp_servers`, `tool_handling`, `expressive`, `use_tts_aligned_transcript` | Refused, as on the session |

The scripted model keeps to what a request allows. A reply that calls a tool under
`tool_choice="none"`, which livekit-agents sends once `max_tool_steps` rounds of tool
calls have run and while the session closes, calls none under `"required"`, calls another
tool than the one named, or calls two tools at once under `parallel_tool_calls=False` is a
harness error. A run without a hangup ends once the agent is quiet, with no tool running
and no reply generated or played, so the session closes over a tool only after
`drain_timeout`.

## What CI runs for LiveKit

The full tier of CI runs every cell below: each night on `main`, on request, and on the
commit of each release. A cell runs every test, checks that each of the three reference
scenarios gives the committed ledger byte for byte in 100 runs in one process and 20 in
fresh processes, runs the two historical cases, and replays both kept failures. A pull
request runs the cells its changed paths select (`CONTRIBUTING.md`, "What CI runs"): a
change to the LiveKit adapter or to the core runs every release on Linux with Python 3.13,
the middle four without the slow tests.

| livekit-agents | Python 3.13 | Other Pythons, on Linux |
| --- | --- | --- |
| 1.5.6 | Linux, macOS | 3.11 |
| 1.5.7 | Linux, macOS | |
| 1.6.5 | Linux, macOS | |
| 1.6.6 | Linux, macOS | 3.10 |
| 1.8.12 | Linux, macOS | |
| 1.8.13 | Linux, macOS | 3.12, 3.14 |

Python 3.10 to 3.14 are each tested with one release. Windows is not tested.

The reference ledgers are the same on Linux and macOS and on every Python above: a ledger
recorded on one machine is what every other must reproduce.

A separate job builds the wheel, installs it alone in a fresh environment outside the
repository, without livekit-agents, and uses it (`scripts/wheel_smoke.sh`): the package
imports and checks a ledger with nothing but its own dependencies. In the full tier
another runs every test that needs no framework with Earful alone, on Python 3.10 and the
oldest jsonschema the package accepts, 4.18.0 (`scripts/make_bare_env.sh`).

## What CI runs for Pipecat

The full tier runs every row below. A row builds the pinned environment without
livekit-agents and runs every test that does not need it: the Pipecat tests and the
tests of the core. It fails if any of those is skipped
(`scripts/ran_every_test.sh --without livekit`). Each row also runs the five Pipecat
reference cases 120 times and compares them with the committed ledgers
(`scripts/repeat.py`). A pull request that changes the Pipecat adapter or the core runs
the two rows on Linux from the checkout.

| pipecat-ai | Python | System | Earful installed from |
| --- | --- | --- | --- |
| 1.16.0 | 3.11 | Linux | the checkout |
| 1.16.0 | 3.12 | Linux | the built wheel |
| 1.16.0 | 3.13 | Linux | the checkout |
| 1.16.0 | 3.13 | macOS | the checkout |

The reference ledgers are the same on every row. On Python 3.11 pipecat-ai replaces
`asyncio.wait_for` with the one from the `wait_for2` package, which takes more steps of
the event loop: tasks due at one instant would run in another order than on Python 3.12,
and the ledger would differ. During a run `asyncio.wait_for` is the standard library's,
as Python 3.12 and later have it.

## What runs on the newest releases

Each night the `canary` workflow installs the newest livekit-agents and the newest
pipecat-ai the extras accept, with their dependencies as of that night, on Linux and
Python 3.13 (`scripts/canary.sh`). It runs the tests that take under a second. A
release with no row above has no reference ledgers, and there the tests that compare a
run with one do not run. The workflow also fails when PyPI has a release
an extra does not accept (`scripts/newer_release.sh`). No pull request runs it.


## The virtual clock

A run is on a clock that jumps to the event loop's next timer whenever the loop has
nothing else to do. Work that takes real time cannot be moved that way, so a run that does
any of the following is invalid (`docs/ledger.md`, Validity):

| Work | Reported as |
| --- | --- |
| `loop.run_in_executor`, `asyncio.to_thread` | `executor_calls` |
| A `threading.Thread` started from the run, or a thread started with `_thread.start_new_thread` | `threads started` |
| A callback handed to the loop from another thread with `call_soon_threadsafe`, which is how a thread, a thread pool or a native library returns a result | `wake-ups from another thread` |
| A socket, a pipe or any other descriptor the loop waits on | `file descriptors watched` |
| A child process started through asyncio | `child processes started` |

The last four are counted in one harness error, such as
`work left the virtual clock (threads started: 1)`.

Two things are not seen, and the run stays valid. A call that blocks the loop, such as
`time.sleep`, `subprocess.run` or a synchronous HTTP client, takes no virtual time. A
thread that was running before the run began is seen only when it wakes the loop: one
that answers after the run has ended, or whose result the run polls for, is not.

During a run these reads return the loop's time, on the thread the run is on:

| Read | Value |
| --- | --- |
| `time.time()`, `time.time_ns()` | 1 700 000 000 s plus the loop's time. A read at the same loop instant as the one before it is 1 µs later |
| `time.perf_counter()` | The loop's time, with the same 1 µs step |
| `time.perf_counter_ns()` | The loop's time, 1 ns later on each read at one instant |
| `time.monotonic()`, `time.monotonic_ns()`, `loop.time()` | The loop's time |
| A pydantic field whose `default_factory` is one of the six functions above, when its model is built by its class or by `model_construct` | As that function. If the model's validator cannot be rebuilt, the run is invalid and names the field |
| The start and end of an OpenTelemetry span | As `time.time_ns()` |

The 1 µs step keeps two timestamps the framework takes at one instant in order. A log
record and a logging handler read the clock without the step, so the logging level does
not change a ledger.

These return real time, and the run is not marked:

- `datetime.now()`, `datetime.utcnow()` and `date.today()`;
- `time.localtime()`, `time.gmtime()` and `time.strftime()` called without a time, and
  `time.process_time()`, `time.thread_time()` and `time.clock_gettime()`;
- a function taken from the `time` module before the run, as `from time import time` and
  a dataclass `field(default_factory=time.time)` take it;
- a validator pydantic built before the run around such a field: a `TypeAdapter`, or a
  model that holds the model as a field and builds it from a dict;
- any read on another thread.

A tool that measures a wait with one of them sees no time pass.

## The portable core

`earful` without framework extras depends only on `jsonschema`. It reads and checks
ledgers, and reads bundles. It runs nothing. Python 3.10 or later.

## What is outside

- Realtime (speech-to-speech) models, and any pipeline other than STT, LLM and TTS.
- Real providers: the STT, the model, the voice and the VAD are Earful's scripted ones.
- A room, a network transport, or audio as a caller's device plays it. The output is a
  sink that consumes frames on the virtual clock.
- A turn detector that is a model. Turns end on the scripted VAD's silence.
- Interruption detection that is a model. The session is set to detect interruptions
  from the VAD, whatever the environment configures.
- A tool whose calls cannot be recorded, such as one inside a toolset. The run is invalid
  and names the tool.
- livekit-agents 2.0. It runs the session in a compiled core with its own runtime and
  threads, which Earful's virtual clock does not reach, and the `livekit` extra accepts
  releases below 1.9.
