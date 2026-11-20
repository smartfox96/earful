# Ledger

Schema: **`earful.ledger/1`**, 17 November 2026.

A ledger is the complete record of one run. It is the only input to the checker
(`docs/contracts.md`), so anything a contract needs must be in it. One JSON object per
line, append-only, in the order events happened.

The shape of every event is defined by a JSON Schema (draft 2020-12) shipped with the
package: [`src/earful/core/schemas/earful.ledger-1.schema.json`](../src/earful/core/schemas/earful.ledger-1.schema.json).
That file is normative. Any JSON Schema validator, in any language, can check a ledger
line against it. This document explains the events and lists the rules that span lines,
which a per-line schema cannot express; `earful.core.schema.validate` applies both. A test
keeps this document and the schema in step.

## Envelope

```json
{"schema": "earful.ledger/1", "run": "run-5f0c2a9be41d7c03", "seq": 17, "t_us": 3300000, "source": "sink", "kind": "playout.started", "data": {...}}
```

| Field | Meaning |
| --- | --- |
| `schema` | Always `earful.ledger/1` |
| `schema_minor` | Additive revision within major version 1; absent means 0. The same throughout a run |
| `run` | Identifies the run. A digest of the data of `run.start`, so two runs share it exactly when they declare the same case, seed, framework release, clock, scenario and contract policy. What then happened is identified by the ledger hash |
| `seq` | Position in the ledger, from 0. The total order of the run: "before" means lower `seq` |
| `t_us` | Virtual time in integer microseconds. Never decreases |
| `source` | The witness that observed the event: `world`, `llm`, `sink`, `backend`, `framework` or `harness`. `docs/contracts.md` section 2 says which may decide a verdict |
| `kind` | What happened. Earful-owned kinds are listed below; `fw.*` kinds are the framework's own events |
| `data` | The event's fields. Required fields and their types are checked; unknown optional fields are retained and ignored |

Each event is a snapshot taken when it is recorded. Nothing a tool or provider does to its
own objects afterwards changes the ledger, and data that JSON cannot hold is refused at
the point it is recorded. Nothing can be appended after `run.end`.

Optional metadata may be added without changing `/1`. Readers ignore unknown fields;
hashes include them. Required fields, known types, event kinds and the rules below stay
checked. A change to what a run executes or a contract means needs a new major version.
Unknown adapter settings and contract policy values are refused.

## Time

Time in Earful is an integer count of microseconds. Scenario authors write seconds;
Earful converts once and does all of its own arithmetic in integers: the clock, the
scripted providers, the sink and the ledger. A float appears only where a framework API
requires one, as a single division of an exact integer. A scripted speech that has lasted
500 000 µs is therefore reported to the framework as exactly 0.5 s, so a framework
threshold is decided by the scripted times and accumulated rounding cannot change the
result.

The virtual clock moves in 1 µs ticks. Timers due within the same microsecond fire in the
order they were scheduled. Time starts at 0 when the run's event loop starts.

Scenario times (speech start and end) are offsets from the **origin**: the first audio
frame the scripted user's providers receive, recorded as `user.origin`. The absolute time
of a speech boundary is `t_us` of `user.origin` plus its offset.

Fields inside `data` that end in `_us` are microseconds too: absolute virtual time when
they name an instant (`words_completed_us`), a duration otherwise (`ttft_us`,
`played_us`).

Audio is counted in whole samples. A sample is consumed once it has played in full, so a
playout position between two samples rounds down, and a word's completion time is the
first microsecond at which its last sample has played.

## Identifiers

All are deterministic: the same run gives the same identifiers.

| Identifier | Assigned by | Scope |
| --- | --- | --- |
| `run` | Digest of `run.start` | One run |
| `speech` | Scenario order, from 0 | One scripted user utterance |
| `call` | Scripted model, from 1 | One model call; its request, its response and the reply it produced |
| `utterance` | Scripted TTS, from 1 | One synthesized text |
| stamp | Scripted TTS, from 1 | One synthesized word. Any three samples of that word's audio in a row name it |
| `segment` | Sink, from 1, at a segment's first frame | One playout segment, audible or not |
| `tool` | Tool witness, from 1 | One invocation of a witnessed tool |
| `call_id` | The model | Links a tool invocation to the model's tool call and to the framework's chat items |
| `request` | Backend, from 1 | One write request |
| `receipt` | Backend | One committed effect |
| `intent` | Scenario | One logical action |

Framework identifiers (chat item and speech ids) are random. Normalization replaces them
with ordinals, and replaces nothing else; see Hashing.

## Event kinds

### harness

| Kind | Fields |
| --- | --- |
| `run.start` | `case`, `seed`, `framework` (`name`, `version`), `clock` (`virtual`, or `real` for a control run), `scenario` (below), `contracts` (contract id to policy values; a contract that is absent uses its defaults). Always the first event |
| `history.final` | `read_from`, `items` (context items, below): the framework's chat history, read back at the end of the run. Framework state, usable only as a judged record |
| `run.end` | `stamp_errors`, `executor_calls`, `underruns`, `harness_errors`, `framework_warnings`. Always the last event. See Validity |

`scenario` holds what the test author declared:

| Field | Contents |
| --- | --- |
| `speeches` | Per speech: `speech`, `start_us`, `end_us` (offsets from the origin), `transcript` (empty for noise), `stt_delay_us`, `expects_reply`, `backchannel`, `retires` (intents it corrects or cancels) |
| `hangup_us` | When the user hangs up, as an offset from the origin. Null when the user stays to the end of the scenario |
| `intents` | Per intent: `intent`, `tools`, `authorized` (durable effects allowed) |
| `witnessed_tools` | The names of the function tools of the agent the run starts with. The tool witness records every call of each, so a call to one of them that leaves no `tool.called` was never made |
| `effects` | Tools that stand for one durable write. Per tool: `tool` and `intent`. In this run a call of such a tool made the write and did not run the tool's own body. Absent when there are none |
| `session` | The framework options the contracts depend on: `min_interruption_duration_us`, `false_interruption_timeout_us`, `min_endpointing_delay_us`, `max_endpointing_delay_us`, `turn_detection`, and `extra_options` (any further options, by name, with their values) |
| `delays` | Provider delays the scenario declares, each in place of what its code would ask for. Per delay: `of` (`llm.ttft`, `llm.duration`, `tts.ttfb` or `backend.latency`), `nth` (which model call, utterance or backend request, from 1) and `us`. Empty when the scenario declares none |
| `faults` | Faults the scenario declares, each for one backend write. Per fault: `of` (`backend.error`, `backend.timeout` or `backend.drop`), `nth` (which backend request, from 1) and `us` (how long the caller waits before it is told the write timed out; null for `backend.error`). Empty when the scenario declares none |
| `providers` | `vad` (`min_speech_us`, `min_silence_us`), `tts` (`seconds_per_letter_us`, `ttfb_us`), `sink` (`can_pause`), `backend` (`mode`: `plain` or `keyed`) |

The four `session` times are declared by the scenario and cannot be set any other way.
After constructing the session, the harness reads the same options back from it; a
difference is a harness error and the run is invalid. The header therefore states what
the framework was configured with.

Scripted speeches may be as close together as an author likes, short of overlapping. A
VAD reports the end of speech only after `min_silence_us` of silence, so speech that
resumes sooner is one **segment** to it, and the scripted VAD reports it as one: a start
when the first speech of the segment begins, an end after the last. While the segment
lasts it tells the framework the user has been speaking since the segment began, pauses
included. The recognizer still delivers one transcript per speech. Which speeches make a
segment follows from the header alone: the speeches and `min_silence_us`.

The scripted VAD reports what the Silero plugin reports for a detector that is never
wrong. From a segment's start to its end it reports every `min_speech_us`, with `speaking`
set. The speech duration it reports keeps growing until it reports the end,
`min_silence_us` after the last speech stopped. A framework that interrupts at a minimum
speech duration can therefore interrupt for a speech shorter than that. The end report
carries the segment's true length.

A declared delay takes the place of what the scenario's code asks for, where it is
applied: `llm.response` records the `ttft_us` and `duration_us` the model call had,
`tts.synthesized` the `ttfb_us` of that utterance, and `effect.requested` the `latency_us`
of that write, whether declared or chosen by the code. The header states which were
declared, so two runs that differ only in a declared delay have different run ids.

A scenario that declares `hangup_us` ends there. At that time the harness records
`user.hangup` and closes the session with its public `aclose`, which is what a
participant's disconnect does in livekit-agents. Writes already requested still have
their outcome, and the final history is read after the session has closed. No speech may
end after the hangup.

The ledger names the framework release because verdicts are interpreted against it. It
does not name the Python version or the platform: those do not change what a run means,
and a ledger must hash the same on every machine. They belong to a run's manifest.

### world: the scripted user

| Kind | Fields | When |
| --- | --- | --- |
| `user.origin` | `by` (`vad` or `stt`) | The scripted user's clock starts. Once per run |
| `user.vad_start` | `speech` | The scripted VAD reports speech to the framework: the true start of a segment's first speech, which `speech` names, plus `min_speech_us` |
| `user.vad_end` | `speech` | The scripted VAD reports silence: the true end of a segment's last speech, which `speech` names, plus `min_silence_us` |
| `user.transcript_final` | `speech`, `text` | The scripted STT delivers the final transcript: true end plus `stt_delay_us` |
| `user.hangup` | none | The user leaves: the origin plus `hangup_us`. At most once per run |

### world: synthesis and tools

| Kind | Fields | When |
| --- | --- | --- |
| `tts.synthesized` | `utterance`, `call`, `attribution`, `candidates`, `text`, `stamps` (one per word), `word_samples` (one per word), `sample_rate`, `ttfb_us`, `started_us` | One utterance is finalized at input flush/end or cancellation |
| `tool.called` | `tool`, `name`, `call_id`, `arguments` (by parameter name) | A witnessed tool starts |
| `tool.returned` | `tool`, `name`, `output`, `text`, `expects_reply` | It returns |
| `tool.raised` | `tool`, `name`, `error` (exception type), `message`, `expects_reply` | It raises |
| `tool.cancelled` | `tool`, `name` | It is cancelled |

The scripted voice gives each word audio in proportion to its letters (`word_samples`):
"I" is short and "reservation" is long, as in speech. A framework that estimates what was
spoken from the text is then measured against a voice shaped like a real one.

LLM text chunks arrive on integer deadlines across `[ttft_us, duration_us]`. Tool calls
arrive at `duration_us`. The voice accepts complete words while text is still arriving
and releases PCM after `ttfb_us` from the first complete word (`started_us`, an absolute
time). An unfinished trailing word waits for more text or an input flush. Cancellation
drops that fragment and finalizes the words already synthesized.

There is one utterance per nonempty TTS input segment, not per sentence or word, and at
most one attributed utterance per model call. Its metadata can follow `playout.started`:
the final word list is unknown at the first frame, and the ledger never rewrites it.
The validator resolves these references after reading the complete ledger and rejects
playout earlier than `started_us + ttfb_us`. Attribution considers replies available at
`started_us`, so a later model call cannot explain audio that already began.

`attribution` says which model reply a synthesized text belongs to. Only replies not yet
synthesized are candidates. `unique`: exactly one reply has this text. `prefix`: none
matches in full and exactly one starts with it. `ambiguous`: more than one fits. `none`:
nothing fits. `call` is set exactly when attribution is `unique` or `prefix`.

`candidates` lists every reply the text could be: one for `unique` and `prefix`, several
for `ambiguous`, none for `none`. The scripted voice is handed a text with no reference
to the model call it came from. When a framework generates a reply again and the scripted
model gives the same words, the ledger cannot say which copy was voiced, so it names them
all. The checker then reads the ledger every way the candidates allow
(`docs/contracts.md`, section 2.5).

A text with attribution `none` is something the application spoke itself, such as a
greeting. The contracts judge it like a reply: it was played, and it is remembered. The
exception is when a model reply was still unsynthesized at that moment. The text may then
be that reply, altered on its way to the voice, and both are unobservable.

Tools are witnessed without changing the agent. The harness replaces each function tool
of the agent it is given with a recording copy, through the agent's public `tools` and
`update_tools`. A tool the harness cannot reach (one belonging to an agent created during
the run) is witnessed only if it is wrapped with `earful.livekit.tools.witnessed`. Each
`tool.called` is followed by at most one of the other three.

A tool can stand for one durable write. The scenario declares it by tool name
(`Case.effects`), or the tool's definition does (`earful.livekit.tools.effect_tool`); the
scenario's declaration is used where both exist. A call of such a tool is recorded like
any other, and in place of the tool's body the witness makes the write: `payload` is the
call's arguments unless the declaration maps them, and the tool returns the receipt
unless the declaration maps that. The header lists these tools in `effects`, so a reader
knows which tool bodies did not run. A scenario that declares an effect for a tool the
agent does not have is refused before it runs.

A tool still running when the scenario is over is cut off by the harness, after the final
history has been read, and recorded as `tool.cancelled`. A tool that never returns
therefore cannot hang a run, and the ledger shows that it never finished.

A tool's result is recorded twice. `output` is the returned value itself, as JSON. `text`
is that value as the framework presents it to the model (for livekit-agents, `str(value)`,
and the empty string for a tool that returns nothing). Earful computes it from the value
the tool returned and never reads it from the framework. A contract compares what the
model was shown with `text`. A value JSON cannot hold, such as an object or an agent handoff, is
recorded as `{"unrepresentable": "<type name>"}` and its `text` is null: its default text
form would put a memory address in the ledger.

`expects_reply` says whether the outcome asks the framework to give the model another
call. It is false when the tool asked for silence: in livekit-agents, by returning nothing
or raising `StopResponse`. The progress contract does not wait for a reply that was not
asked for.

### llm: the scripted model

| Kind | Fields | When |
| --- | --- | --- |
| `llm.request` | `call`, `items` | The framework asks for a completion. `items` is the context exactly as presented, in order |
| `llm.response` | `call`, `text`, `tool_calls`, `claims`, `ttft_us`, `duration_us` | The model decides its answer, before streaming it |

Each entry of `tool_calls` has `name`, `arguments` (the argument text exactly as the model
produced it) and `call_id`.

`claims` lists spans of the reply that tell the user an intent succeeded: `intent`,
`first_word`, `last_word` (0-based, inclusive).

**Context items.** `llm.request` and `history.final` carry the same item shape, reduced to
the fields contracts use. Every item has `type` and `id`.

| `type` | Further fields |
| --- | --- |
| `message` | `role`, `text`, `interrupted` |
| `function_call` | `name`, `arguments`, `call_id` |
| `function_call_output` | `name`, `output`, `call_id`, `is_error` |
| anything else | none. Framework bookkeeping, such as a handoff marker |

### sink: commands received

| Kind | Fields | When |
| --- | --- | --- |
| `sink.pause` | `segment` | The framework asks the output to pause, and the output was not already asked to |
| `sink.resume` | `segment` | The framework asks it to resume, and it had last been asked to pause |
| `sink.clear` | `segment` | The framework asks it to discard buffered audio |
| `sink.flush` | `segment` | The framework marks the end of a segment's audio |

`segment` is null when no segment has audio.

Pause belongs to the output as a whole, as in livekit-agents' room output. Once
paused, the output plays nothing, including a segment that arrives later, until it is
resumed; clearing a segment does not resume it. The framework repeats pause on every VAD
update. Because pausing a paused output changes nothing, only changes of the requested
state are recorded, and no information is lost.

### sink: what playout did

| Kind | Fields | When |
| --- | --- | --- |
| `playout.started` | `segment`, `utterance` | A segment becomes audible |
| `playout.paused` | `segment`, `played_us` | An audible segment stops on a pause or when its streaming buffer empties |
| `playout.resumed` | `segment`, `played_us` | It becomes audible again |
| `playout.finished` | `segment`, `interrupted`, `audible`, `utterances`, `position_us`, `pushed_us`, `words_played`, `words_completed_us`, `word_cut`, `word_cut_samples`, `text_played` | A segment ends, played out or cleared |

`words_played` are the stamps whose every sample was consumed, and `words_completed_us`
the instant each one's last sample was consumed. `word_cut` is the stamp the playout
position stopped inside, if any, and `word_cut_samples` how many of its samples played.
`audible` is false for a segment cleared before it ever started; such a segment has no
`playout.started`.

### backend

| Kind | Fields | When |
| --- | --- | --- |
| `effect.requested` | `request`, `intent`, `key`, `payload`, `tool`, `latency_us` | A tool asks for a write. `tool` is the witnessed invocation that asked, if any |
| `effect.committed` | `request`, `intent`, `key`, `receipt`, `count_for_intent` | The write becomes durable |
| `effect.deduplicated` | `request`, `intent`, `key`, `receipt` | In `keyed` mode, the key already committed; the original receipt is returned |
| `effect.failed` | `request`, `intent`, `key` | The backend rejected the write. Nothing was committed and the caller was told |
| `effect.timed_out` | `request`, `intent`, `key`, `after_us` | The caller stopped waiting, `after_us` after it asked |

A write reaches the backend from code the agent runs: `earful.core.backend.effect`,
called where the real service would make the write durable, usually by the stand-in the
test gives the agent for that service. The agent is built as it is built anywhere and is
handed no backend. A tool declared as an effect (above) makes the same call.

A keyed backend decides whether a key is a repeat at the moment the write would become
durable. Two overlapping writes with one key therefore commit once.

Once a write is requested its outcome is the backend's. It is decided `latency_us` after
the request, by the write's latency and its fault, whatever happens to the caller in the
meantime. A tool that is cancelled while it waits has still written.

`effect.committed`, `effect.deduplicated` and `effect.failed` are outcomes: a request has
at most one. `effect.timed_out` says what the caller was told and is separate from the
outcome. A request can have both a commit and a timeout, in either order. A request with a
timeout and no outcome was lost, or had not committed when the scenario ended.

### framework

`fw.*` events are the framework's public events, recorded as explanation. Their fields are
adapter-defined. Each carries `created_us`, the framework's own timestamp for the event.
They never decide a verdict.

## Rules that span events

`validate` applies the JSON Schema to every line and then checks:

- the ledger begins with its only `run.start` and ends with its only `run.end`;
- `seq` counts from 0 without gaps, and `t_us` never decreases;
- every event carries the same `run`, and it is the digest of `run.start`. Editing the
  header of a recorded ledger is detected;
- speeches are numbered in order, end no earlier than they start, and scripted-user
  events name a declared speech, come after the origin, and occur at most once per
  speech; a VAD report names the first or the last speech of a segment;
- a hangup is recorded at most once, only if the scenario declares one, and no declared
  speech ends after it. On the virtual clock it is at the declared time after the origin,
  to the microsecond;
- every intent named by a write, a claim or a speech's `retires` is declared, once. A
  misspelt intent is an error, so it cannot leave a contract with nothing to check;
- a tool listed in `effects` is listed once, is one of `witnessed_tools`, and is among the
  tools of the intent it names;
- each model call is requested once and answered once; a claim fits inside its reply; no
  two tool calls share a call id, since the id is what links a call to its result;
- an attributed synthesis names a call that has answered and has at least one word, and
  no call is attributed twice; its candidates fit its attribution and none is already
  attributed; there is one stamp and one sample count per word, and no utterance number or
  stamp is used twice;
- each tool invocation and each backend request has at most one outcome, a caller gives
  up on a request at most once, every backend event carries its request's intent, and a
  deduplication names a receipt that was committed;
- segments start at most once and finish at most once, pause only while playing and
  resume only while paused, `audible` matches whether the segment started, and every
  played word has a completion time, in order and no later than the event;
- a segment names only utterances that were synthesized, and every word it played or cut
  belongs to one of them.

## Validity

A run produces verdicts only if it is valid. `earful.core.schema.validity` is the single
definition; it reads only the ledger and returns every reason a run is not valid:

- the ledger does not validate;
- `clock` is not `virtual`;
- `stamp_errors` is not zero: audio reached the sink altered;
- `executor_calls` is not zero: work left the virtual clock for a thread;
- `underruns` is not zero: playout ran ahead of the audio it had been given, so word
  times are unreliable;
- `harness_errors` is not empty: Earful's own simulation or recording failed, or the run
  did not stay on the virtual clock. Examples: a recording handler raised, the scripted
  providers disagreed about the origin, a declared session option differs from the one
  the session holds, a scripted claim does not fit its reply, a tool started a thread or
  waited on a socket (`work left the virtual clock (threads started: 1)`). What counts as
  leaving the clock is listed in `docs/compatibility.md`, The virtual clock.

`framework_warnings` is recorded but does not invalidate a run: a warning is the
framework describing its own behaviour, and the contracts judge that behaviour from the
ledger. An exception raised inside one of Earful's scripted providers or its output does
invalidate it. The framework would treat it as a provider that failed and carry on, so it
is recorded in `harness_errors`, as `the scripted voice raised KeyError: ...`.

The harness refuses to return an invalid run unless asked to: `run_case` raises
`InvalidRun`, which carries the result so the ledger can still be inspected.

## Hashing

A ledger is normalized before hashing: keys are sorted, separators are fixed, and random
framework identifiers (`item_3f2a…`) are replaced by first-appearance ordinals
(`item_#1`). The hash is SHA-256 over the normalized lines, each followed by a newline,
so `shasum -a 256` of a normalized ledger file equals its hash.

An identifier is replaced only where the framework's own identifiers are recorded: a field
named `id` or `speech_id`, in a `fw.*` event or in a context item of `llm.request` or
`history.final`. There, a letter-led prefix that ends in `_` followed by 12 lowercase
hexadecimal digits becomes that prefix, `#` and a number: 1 for the first identifier with
that prefix in the ledger, 2 for the next. The same identifier gets the same ordinal
everywhere, so references between events survive.

Nothing else is rewritten. A payload, a tool's arguments and output, a call id, a
transcript and a reply are hashed as recorded, including a value that looks like a
framework identifier. Two runs that differ in an order number have different hashes. A
run seeds Python's `random` module with the case's seed, so an identifier the application
draws from it repeats. One drawn from `uuid.uuid4` differs on every run, and the hash with
it.

Earful 0.1.1 and earlier replaced such a value wherever it appeared. A ledger that holds
one in application data therefore hashes differently now. None of the reference ledgers
does.

## Reference ledgers

`tests/golden/` holds one normalized ledger per case and supported release. Every
platform must reproduce them byte for byte: the test suite compares a fresh run with the
committed file, and `scripts/repeat.py --check` does the same over 120 runs.

Checking never writes. `scripts/check_all.sh --update` is the one way the reference
ledgers change; the diff it produces is reviewed like any other change.

## Comparing releases

`compare_view` in `src/earful/core/ledger.py` drops what differs between releases for
reasons unrelated to behaviour: the release itself (`framework` in `run.start`, and the
run id derived from it) and the framework's `created_us`, which drifts by microseconds
because each release reads the wall clock a different number of times per step.
Everything else is kept. On `barge_in_mid_word`, the comparison views of 1.6.6 and 1.8.12
differ only in when the session closes.

## Versioning

`earful.ledger/1` is the format of the first release. An optional field can be added to
it without a new major version: readers ignore fields they do not know, and a field that
is absent means what it meant before the field existed. Such an addition raises
`schema_minor` once a release has shipped without it. A new kind, a new required field, a
change to the type or meaning of an existing field, or a change to a rule that spans
events is a new major version, with its own schema file. A change that alters what a
recorded ledger hashes to regenerates the reference ledgers.

## Before `earful.ledger/1`

The format was numbered 0 to 5 while it was being developed, and was reset to 1 for the
first release. The sections below record what each of those development versions changed.
Their numbers are not versions of the released format: "development version 1" below is
not `earful.ledger/1`.

### Changes in `earful.ledger/1` from development version 5

- The scripted model delivers its text over the time it generates, and the scripted voice
  speaks words as they arrive. `tts.synthesized` is recorded once per utterance when its
  input ends or is cancelled, carries `started_us`, and can follow the `playout.started`
  of its own audio.
- `playout.paused` is also recorded when a streaming buffer runs empty while audible.
- Events can carry `schema_minor`, and fields the schema does not name are accepted.
- The scenario can state `effects`: tools that stand for one durable write.

### Changes in development version 5

- The scenario states when the user hangs up, in `hangup_us`.
- One scripted-user event: `user.hangup`.
- A write commits although its caller was cancelled. Before, cancelling the caller
  cancelled the write. The event shapes are unchanged.

### Changes in development version 4

- The scenario states the faults it declares, in `faults`.
- Two backend events: `effect.failed` and `effect.timed_out`.

### Changes in development version 3

- The scenario states the provider delays it declares, in `delays`.
- Speeches closer together than the VAD's silence window are no longer refused. They are
  one segment to the VAD, so `user.vad_start` and `user.vad_end` are recorded once per
  segment, for its first and its last speech, and no longer once per speech.
- A scripted tool call that names no call id is given one by the scripted model, unique
  to the model call (`call-<model call>-<n>`). The reference cases no longer invent ids,
  so their recorded call ids changed.

### Changes in development version 2

- `tts.synthesized` carries `word_samples`, one length per word, in place of a single
  `samples_per_word`; the scenario declares `seconds_per_letter_us` in place of
  `seconds_per_word_us`. The scripted voice no longer gives every word the same length.
- `tool.returned` and `tool.raised` carry `expects_reply`.
- The scenario names the tools the witness records, in `witnessed_tools`.
- `tts.synthesized` carries `candidates`: every reply the text could be.
- A final transcript is never empty; tool call ids are unique; a segment's pauses and
  resumes alternate; a segment refers only to utterances and words that were synthesized.

### Changes in development version 1

Development version 0 was used for Gate 0 (`docs/history/gate0-feasibility.md`); commit `70bbc4c`
reproduces it. These came with development version 1.

- Added: a published JSON Schema; the run id; the framework release and clock in the
  header; the scenario header; `user.origin`; tool events; `effect.requested` and
  `effect.deduplicated`; sink commands; claims; provider delays; TTS attribution; word
  completion times; validity counters and harness errors.
- Changed: segments are numbered at their first frame, so a segment cleared before it
  became audible has its own number. Pause and resume are recorded as commands
  (`sink.*`) separately from their effect (`playout.*`), and a pause outlives the segment
  it was issued during.
- Time: same-instant timers were ordered by nudging their due times apart by a
  nanosecond each, which moved events by up to a few microseconds, and the scripted
  providers computed durations in floating point; either could move a threshold decision
  by one VAD tick. Time is now integer microseconds throughout, and same-instant timers
  fire in scheduling order.
