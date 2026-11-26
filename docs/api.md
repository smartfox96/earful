# API reference

Every public name of `earful`, `earful.livekit` and `earful.pipecat`, the `earful` command
and the pytest plugin. [Writing a test](testing.md) and
[Writing a Pipecat scenario](pipecat.md) show them in use. A signature below is written
without its type annotations. Times are in seconds unless a name ends in `_us`.

## Names

`earful` needs no framework. `earful.livekit` needs livekit-agents and `earful.pipecat`
needs pipecat-ai. A test imports from the adapter of the framework its agent uses.

| Name | `earful` | `earful.livekit` | `earful.pipecat` |
| --- | --- | --- | --- |
| `BackendError` | yes | yes | yes |
| `BackendTimeout` | yes | yes | yes |
| `Case` | | yes | yes |
| `Claim` | | yes | yes |
| `ClockedSink` | | yes | |
| `Condition` | | yes | yes |
| `Context` | | yes | yes |
| `ContractFailure` | yes | yes | yes |
| `Delay` | | yes | yes |
| `Effect` | yes | yes | yes |
| `Ends` | | | yes |
| `Fault` | | yes | yes |
| `Intent` | | yes | yes |
| `InvalidRun` | | yes | yes |
| `Policy` | | yes | yes |
| `Predicate` | yes | yes | yes |
| `Reply` | | yes | yes |
| `Report` | yes | | |
| `RunResult` | | yes | yes |
| `Scenario` | | yes | yes |
| `ScriptedModel` | | yes | yes |
| `ToolCall` | | yes | yes |
| `ToolResult` | | yes | yes |
| `UserSpeech` | | yes | yes |
| `Verdict` | yes | | |
| `WorkersUnavailable` | | yes | yes |
| `__version__` | yes | | |
| `check` | yes | | |
| `cross_check` | | yes | yes |
| `effect` | yes | yes | yes |
| `effect_tool` | | yes | |
| `explore` | | yes | yes |
| `keep` | | yes | yes |
| `ledger_hash` | yes | | |
| `read_ledger` | yes | | |
| `replay` | | yes | yes |
| `reply` | | yes | yes |
| `run` | | yes | yes |
| `run_case` | | yes | yes |
| `tool_failed` | | yes | yes |
| `tool_returned` | | yes | yes |
| `tool_succeeded` | | yes | yes |
| `user_ever_said` | | yes | yes |
| `user_said` | | yes | yes |
| `witnessed` | | yes | |
| `write_ledger` | yes | | |

A name in more than one column is one object, imported from any of them. Nine names are
the exception: `Case`, `RunResult`, `Scenario`, `run`, `run_case`, `explore`,
`cross_check`, `keep` and `replay` are each adapter's own. The six functions among them
have the same signature in both adapters, and the three classes are described per adapter
below.

## A scenario

### `Case` for LiveKit

Everything that defines a run of a LiveKit `AgentSession`. The same case and seed give
the same ledger. `case.check()` raises `ValueError` for a case that contradicts itself,
and every run calls it first.

| Field | Default | Meaning |
| --- | --- | --- |
| `name` | required | Names the case in the ledger and in messages |
| `agent` | required | Builds the agent under test. Called with no arguments, once for each run: an agent class, or a function that returns an agent |
| `speeches` | required | The scripted user, a list of `UserSpeech` |
| `policy` | required | The scripted model: a `ScriptedModel`, or any `Policy` |
| `intents` | `[]` | `Intent` declarations. Needed for an intent no declared effect names, or to authorize more than one effect |
| `effects` | `{}` | Tool name to the `Effect` a call of that tool makes in place of its body |
| `contracts` | `{}` | Contract id to its policy values, such as `{"L1": {"slack_us": 3_000_000}}` ([`contracts.md`](contracts.md#6-the-seven-contracts)) |
| `delays` | `[]` | `Delay` declarations |
| `faults` | `[]` | `Fault` declarations |
| `hangup` | `None` | When the user hangs up, from the origin. No speech may end after it |
| `backend_mode` | `'plain'` | `'plain'` commits every write. `'keyed'` deduplicates a write whose idempotency key has committed |
| `seconds_per_letter` | `0.06` | How long the scripted voice takes for each letter of a word |
| `tts_ttfb` | `0.2` | The scripted voice's time to first audio |
| `vad_min_speech` | `0.05` | How much speech the scripted VAD hears before it reports the start |
| `vad_min_silence` | `0.5` | How much silence it hears before it reports the end |
| `sink_can_pause` | `True` | Whether the audio output says it can pause |
| `min_interruption_duration` | `0.5` | The session's interruption option of that name |
| `false_interruption_timeout` | `2.0` | The session's interruption option of that name |
| `allow_interruptions` | `True` | Whether the user can interrupt the agent: the session's interruption option `enabled` |
| `preemptive_generation` | `True` | Whether the session starts a reply before the user's turn ends: its preemptive generation option `enabled` |
| `min_endpointing_delay` | `0.5` | The session's endpointing `min_delay` |
| `max_endpointing_delay` | `6.0` | The session's endpointing `max_delay` |
| `drain_delay` | `5.0` | How long the run goes on after the last transcript, or after a hangup, before it is wound up. Without a hangup it then waits for the agent to fall quiet, up to `drain_timeout`: no tool running, no reply generated or played |
| `drain_timeout` | `30.0` | How long the agent then has to fall quiet, and the session to finish, before it is cut off |
| `sink` | `ClockedSink` | The class of the audio output. A subclass models a faulty output |
| `session_kwargs` | `{}` | Further `AgentSession` options, of those [`compatibility.md`](compatibility.md#the-options-earful-models-on-livekit) lists. Any other is refused |
| `scripted_turns` | `True` | Earful uses separate scripted providers and silence-based turn detection. The result describes the session. Setting this to `False` refuses a session whose turn detector Earful cannot run. |

### `Case` for Pipecat

Everything that defines a run of a Pipecat pipeline
([`pipecat.md`](pipecat.md#a-first-scenario)). The fields `name` to `tts_ttfb`, and the
two `drain` fields, mean what they mean for LiveKit.

| Field | Default | Meaning |
| --- | --- | --- |
| `name` | required | Names the case in the ledger and in messages |
| `bot` | required | The application. Called once for each run with Earful's model service: it registers its function handlers there and returns the `LLMContext` the bot starts with |
| `speeches` | required | The scripted user |
| `policy` | required | The scripted model |
| `intents` | `[]` | `Intent` declarations |
| `effects` | `{}` | Function name to the `Effect` a call of that function makes in its place |
| `contracts` | `{}` | Contract id to its policy values |
| `delays` | `[]` | `Delay` declarations |
| `faults` | `[]` | `Fault` declarations |
| `hangup` | `None` | When the user hangs up. The pipeline is then cancelled |
| `backend_mode` | `'plain'` | `'plain'` or `'keyed'` |
| `seconds_per_letter` | `0.06` | How long the scripted voice takes for each letter of a word |
| `tts_ttfb` | `0.2` | The scripted voice's time to first audio |
| `vad_start_secs` | `0.2` | Pipecat's `VADParams.start_secs`, for the scripted VAD |
| `vad_stop_secs` | `0.2` | Pipecat's `VADParams.stop_secs`, for the scripted VAD |
| `tts_word_timestamps` | `False` | Whether the scripted voice sends the time each word starts |
| `user_speech_timeout` | `0.6` | The timeout of `SpeechTimeoutUserTurnStopStrategy`. A pipeline of the case's own has to be built with the same value |
| `user_turn_stop_timeout` | `5.0` | The user aggregator's `user_turn_stop_timeout`, held to the same rule |
| `drain_delay` | `5.0` | How long the run goes on after the last transcript, or after a hangup |
| `drain_timeout` | `30.0` | How long the bot then has to fall quiet, and the pipeline to end |
| `pipeline` | `None` | A function from `Ends` to the pipeline's processors in order, or to a `Pipeline` of them. `None` runs Earful's standard seven ([`pipecat.md`](pipecat.md#your-own-pipeline)) |

### `UserSpeech`

One scripted user utterance. `start` and `end` count from the origin, the first audio
frame the scripted providers receive.

| Field | Default | Meaning |
| --- | --- | --- |
| `start` | required | When the user starts speaking |
| `end` | required | When the user stops |
| `transcript` | required | What the scripted speech-to-text delivers. Empty for noise: the VAD fires and no transcript follows |
| `stt_delay` | `0.2` | How long after `end` the final transcript arrives |
| `expects_reply` | `None` | Whether the turn is owed a response (L1). `None` means yes for a speech with a transcript that is not a backchannel |
| `backchannel` | `False` | The user acknowledges and does not take the floor |
| `retires` | `()` | The intents this turn corrects or cancels (A1) |

### `Intent`

A logical action the user wants done. A declared `Effect` declares its intent with it, so
an `Intent` is written out only to name tools that write through `effect` themselves, or
to change `authorized`.

| Field | Default | Meaning |
| --- | --- | --- |
| `intent` | required | Its name, as `effect`, `Effect` and a claim use it |
| `tools` | `()` | The tools that carry it out |
| `authorized` | `1` | How many durable effects the scenario allows for it (A2) |

### `Delay`

A provider delay declared as data, in place of what the scenario's code asks for.

| Field | Default | Meaning |
| --- | --- | --- |
| `of` | required | `'llm.ttft'`, `'llm.duration'`, `'tts.ttfb'` or `'backend.latency'` |
| `nth` | required | Which model call, utterance or backend write, counted from 1 |
| `seconds` | required | The delay |

### `Fault`

A fault declared for one backend write
([`search.md`](search.md#faults) says what each does).

| Field | Default | Meaning |
| --- | --- | --- |
| `of` | required | `'backend.error'`, `'backend.timeout'` or `'backend.drop'` |
| `nth` | required | Which backend write, counted from 1 |
| `seconds` | `None` | How long the caller waits before it is told of a timeout. Required for a timeout or a drop, and refused for an error |

### `Scenario`

A scenario's code: the function that builds its `Case`, and the `module:function` name
that function has, if it has one. `run`, `explore` and `cross_check` take a `Scenario`
wherever they take a case, and build one from anything else they are given.

```python
class Scenario:
    def of(source) -> Scenario: ...
    def default(self, seed=0): ...
    def build(self, data): ...
    def run(self, data): ...
```

`Scenario.of` is a class method. It takes a `module:function` reference, a function that
returns a `Case`, or a `Case`, and raises `TypeError` for anything else. `make` is the
function and `reference` its name, `None` for a scenario built inline. `default` gives
the scenario as its author wrote it, as an `earful.case/1` document. `build` gives the
`Case` such a document describes, and `run` runs it and returns the ledger.

## The scripted model

### `ScriptedModel`

A model as ordered rules. The first rule whose condition holds gives the reply.

```python
class ScriptedModel:
    def when(
        self, condition, answer=None, *, say="", call=(), claims=(), ttft=None, duration=None
    ) -> ScriptedModel: ...
    def otherwise(
        self, answer=None, *, say="", call=(), claims=(), ttft=None, duration=None
    ) -> ScriptedModel: ...
```

Both return a new model, so a model is built as one chained expression. A reply is given
as a `Reply`, or by its parts as `reply` takes them. Giving both, or neither, raises
`ValueError`. So does `when` after `otherwise`, and a second `otherwise`. A model called
with a context no rule matches, and with no `otherwise`, raises `NoRule`, and the run is
invalid ([`troubleshooting.md`](troubleshooting.md#the-scripted-model-has-no-rule-for-a-context)).

### Conditions

```python
def user_said(*phrases) -> Condition: ...
def user_ever_said(*phrases) -> Condition: ...
def tool_returned(tool=None) -> Condition: ...
def tool_succeeded(tool=None) -> Condition: ...
def tool_failed(tool=None) -> Condition: ...
```

| Condition | Holds when |
| --- | --- |
| `user_said` | The latest user message contains one of the phrases, in any case |
| `user_ever_said` | Some user message in the context contains one |
| `tool_returned` | The context holds a result of the tool, a success or an error |
| `tool_succeeded` | It holds a result of the tool that is not an error |
| `tool_failed` | It holds results of the tool and every one is an error |

With no tool named, the last three mean any tool. `user_said()` and `user_ever_said()`
with no phrase raise `ValueError`.

A `Condition` is a named test of a context. It is called with a `Context` and returns a
bool, and it combines with `&`, `|` and `~`. Any function from a `Context` to a bool can
stand where a condition is taken, and `Condition(test, name)` gives one a name for the
message that lists a model's rules.

### `Context`

What the model is shown for one call.

| Attribute | Meaning |
| --- | --- |
| `items` | The item summaries, as a policy function receives them |
| `user_turns` | The text of every user message, oldest first |
| `last_user_turn` | The text of the latest user message. Empty if there is none |
| `assistant_turns` | The text of every assistant message, oldest first |
| `tool_results` | Every tool result as a `ToolResult`, oldest first |
| `results_of` | A method: the results of one tool, or of every tool with no name given |

### `ToolResult`

One tool result in a context, as the framework presented it to the model.

| Attribute | Meaning |
| --- | --- |
| `name` | The tool's name, or `None` where the framework gives none |
| `output` | The result text |
| `call_id` | The id of the call it answers |
| `failed` | The framework marked it as an error |

### `reply`

```python
def reply(say="", *, call=(), claims=(), ttft=None, duration=None) -> Reply: ...
```

Builds a `Reply` from plain values. `call` is a tool's name, a `(name, arguments)` pair
with the arguments as a dict, a `ToolCall`, or a list of these. A claim is an intent's
name, which claims the whole text, or a `Claim`. `ttft` and `duration` left as `None`
take the defaults of `Reply`.

### `Reply`

| Field | Default | Meaning |
| --- | --- | --- |
| `text` | `''` | What the model says |
| `tool_calls` | `()` | The `ToolCall`s it makes |
| `ttft` | `0.1` | Its time to first token |
| `duration` | `0.3` | Its total generation time. Never shorter than `ttft` in a run |
| `claims` | `()` | The `Claim`s the text carries |

### `ToolCall`

| Field | Default | Meaning |
| --- | --- | --- |
| `name` | required | The tool the model calls |
| `arguments` | required | The arguments as JSON text |
| `call_id` | `None` | The call's id. The scripted model assigns one where it is left out |

### `Claim`

A span of a reply that tells the user an intent succeeded. A3 checks it against the
backend.

| Field | Default | Meaning |
| --- | --- | --- |
| `intent` | required | The intent claimed |
| `first_word` | `0` | The first word of the span, counted from 0 |
| `last_word` | `None` | The last word of the span, included. `None` means the reply's last word |

### `Policy`

The type of a model policy: a function from the context, as a list of item summaries, to
a `Reply`. A `ScriptedModel` is one. An item summary is a dict with `type` and `id`, and
further keys by type ([`ledger.md`](ledger.md#llm-the-scripted-model)). The same context
has to give the same reply.

## Side effects

### `effect`

```python
def effect(intent, payload=None, *, key=None, latency=0.0) -> str: ...
```

A coroutine. It requests one durable write for `intent` from the backend of the run in
progress and returns its receipt. `key` is the idempotency key and `latency` how long the
write takes to commit. It raises `BackendError` for a write the scenario declares
rejected, `BackendTimeout` for one whose caller the scenario declares gives up, and
`RuntimeError` outside a run.

### `Effect`

One durable write, declared for a tool as a whole and made in place of the tool's body.

| Field | Default | Meaning |
| --- | --- | --- |
| `intent` | required | The logical action the write is for |
| `payload` | `None` | A function from the call's arguments to what is written. `None` writes the arguments |
| `key` | `None` | The idempotency key: a string, or a function from the arguments to one |
| `result` | `None` | A function from the receipt and the arguments to what the tool returns. `None` returns the receipt |
| `latency` | `0.0` | How long the backend takes to commit |

An empty intent or a negative latency raises `ValueError`.

### `BackendError`

A `RuntimeError`. The backend rejected a write and nothing was committed.

### `BackendTimeout`

A `TimeoutError`. The caller stopped waiting for a write. The write may still have
committed.

### `effect_tool`

```python
def effect_tool(intent, *, payload=None, key=None, result=None, latency=0.0): ...
```

LiveKit only. A decorator that puts an `Effect` declaration on the tool itself, under
`function_tool`. Outside a run the decorated tool is the tool as written. It raises
`TypeError` on a tool that is already witnessed. A declaration in `Case.effects` for the
same tool is used over it.

### `witnessed`

```python
def witnessed(fn, *, name=None, effect=None): ...
```

LiveKit only. Wraps an async tool function so that each call in a run is recorded. A run
does this for every function tool of the agent and of the session, so a test seldom
calls it. Wrapping twice changes nothing. It raises `TypeError` for a function that is
not async.

## Running

### `run`

```python
def run(source, *, data=None, seed=None) -> RunResult: ...
```

Runs a scenario once on the virtual clock. `source` is a `Case`, a function that returns
one, or a `module:function` reference. `seed` is 0 when left out. `data` is a case as data
(`earful.case/1`), such as the example of a broken rule a search gives, and is run with
the scenario's code and the seed it holds. A different `seed` with it is refused, and so
is a case made for another scenario than `source`. A delay
or fault declared for a model call, utterance or write the run never reached is named in a
`UserWarning`. It raises `InvalidRun` for a run that is not valid evidence, and
`ValueError` for a scenario or a case that is refused before it runs.

### `run_case`

```python
def run_case(case, seed=0, *, virtual=True, require_valid=True) -> RunResult: ...
```

What `run` calls with the `Case` it has built. `require_valid=False` returns an invalid
run in place of raising `InvalidRun`. `virtual=False` runs on the real clock, with
nothing patched. Such a run is never valid evidence, and `cross_check` is the use for it.

### `RunResult`

| Attribute | Adapter | Meaning |
| --- | --- | --- |
| `case` | both | The case's name |
| `seed` | both | The seed of the run |
| `ledger` | both | The run's ledger. `ledger.events` is the list of events |
| `events` | both | The same list |
| `hash` | both | The ledger hash ([`ledger.md`](ledger.md#hashing)) |
| `invalid` | both | Why the run is not valid evidence. Empty for a valid run |
| `harness_errors` | both | The errors Earful's own simulation recorded |
| `report` | both | The `Report` of checking the ledger |
| `assert_contracts` | both | A method, below |
| `versions` | LiveKit | The releases of livekit-agents and livekit, the Python and the platform |
| `clock` | LiveKit | The pydantic fields the virtual clock was given to, and those it could not be |
| `stamp_errors` | LiveKit | Audio samples that reached the output altered |
| `executor_calls` | LiveKit | Pieces of work the run handed to a thread pool |
| `underruns` | LiveKit | Times playout ran ahead of its audio |
| `framework_warnings` | LiveKit | The warnings livekit-agents logged during the run, whatever level its loggers are set to |

```python
class RunResult:
    def assert_contracts(self, *, allow=(), require=()): ...
```

Raises `ContractFailure` if a rule broke that `allow` does not name, or if a contract in
`require` ended with no pass or fail. `allow` takes rules as `CONTRACT/RULE` and
`require` takes contract ids, each as one string or a list
([`testing.md`](testing.md#4-one-run)). A rule or a contract that does not exist raises
`ValueError`.

### `InvalidRun`

A `RuntimeError`. The run finished and may not be used as evidence
([`ledger.md`](ledger.md#validity)). `reasons` lists why and `result` holds the
`RunResult`, so the ledger can still be read.

### `explore`

```python
def explore(
    source,
    *,
    data=None,
    seed=None,
    workers=None,
    variables=(
        "speech.position",
        "llm.ttft",
        "llm.duration",
        "tts.ttfb",
        "backend.latency",
        "user.hangup",
    ),
    faults=("backend.error", "backend.timeout", "backend.drop"),
    pairs=False,
    profiles=0,
    speeches=None,
    moments=None,
    words=True,
    max_runs=None,
    planned=None,
    delay_limit_us=10000000,
    wait_us=1000000,
    stretch_us=1500000,
): ...
```

Searches a scenario for the timings and faults at which it breaks a contract
([`search.md`](search.md#3-the-boundary-scheduler)) and returns an `Exploration`.

| Argument | Meaning |
| --- | --- |
| `data` | A case as data to search from, in place of the scenario as written. A case made for another scenario than `source` is refused |
| `seed` | The seed of the scenario as written, 0 when left out. A case given as `data` holds its own, and a different `seed` with it is refused |
| `workers` | Worker processes to run the cases in. 0 runs them in this process, and more needs a scenario that can be imported ([`testing.md`](testing.md#6-where-a-scenario-lives)). Left out, a scenario that can be imported gets one per processor, or what `EARFUL_WORKERS` says, and this process where workers cannot start |
| `variables` | The kinds of time to move. `'speech.end'` and `'speech.stt_delay'` can be added |
| `faults` | The faults to add to each backend write |
| `pairs` | Also try two changes at once |
| `profiles` | How many seeded redraws of the provider delays to move the speeches under, 0 or more |
| `speeches` | The indices of the speeches to move, from 0. `None` moves every one. An index the case does not have raises `ValueError` |
| `moments` | The handles of a speech to place: `'vad_start'`, `'interrupt'`, `'transcript'`, `'vad_end'`, `'turn_end'`. `None` places every one. Any other name raises `ValueError` |
| `words` | Whether the end of each played word is a boundary |
| `max_runs` | A budget of runs. A search it stops is not `complete` |
| `planned` | A function that is handed the search's plan before the cases run. `print` shows it |
| `delay_limit_us` | The longest a delay or a wait is made ([`search.md`](search.md#3-the-boundary-scheduler)) |
| `wait_us` | How long the caller of a write with a timeout or a drop waits |
| `stretch_us` | How long `pairs` makes a slow write or generation, up to `delay_limit_us`. 0 makes none slow |

It raises `ValueError` for an unknown variable or fault, for a negative wait or stretch,
for a delay limit below 1, for a negative `workers`, for
`workers` with a scenario that cannot be imported, and for a case that cannot be run as
given. It raises `WorkersUnavailable` where worker processes cannot be used.

### `WorkersUnavailable`

A `RuntimeError`. Worker processes could not be started, or one died. The message says
which and what to do ([`troubleshooting.md`](troubleshooting.md#worker-processes)).

### `cross_check`

```python
def cross_check(source, *, data=None, seed=None, runs=3, tolerance=None): ...
```

Runs the case once on the virtual clock and `runs` times on the real clock, and returns a
`CrossCheck` ([`testing.md`](testing.md#10-on-the-real-clock)). `tolerance` is an
`earful.core.crosscheck.Tolerance`, by default `Tolerance(time_us=100_000, words=1)`. A
real-clock run takes as long as the conversation it scripts. It raises `InvalidRun` if
the virtual run is not valid evidence, and `ValueError` for `runs` below 1.

### `keep`

```python
def keep(found, rule, directory, *, repeats=3): ...
```

Reduces a case of the search `found` that breaks `rule` and writes it to `directory` as a
bundle ([`search.md`](search.md#6-the-bundle-earfulbundle1)). `rule` is `CONTRACT/RULE` or a
`Predicate`. It returns the `Bundle` as read back. It raises `ValueError` if the scenario
cannot be imported, if no case of the search breaks the rule, and if the failure does not
repeat. Nothing is written then.

### `replay`

```python
def replay(directory, *, repeats=3): ...
```

Checks every file of a bundle and runs its reduced case `repeats` times on the installed
release. It returns a `Replay`. It imports and runs the scenario the bundle names, so
replay only bundles you trust. A bundle that fails a check raises `ValueError`.

## Results

### `Report`

What the checker returns for one ledger.

| Attribute | Meaning |
| --- | --- |
| `run` | The run id |
| `session_scope` | What the LiveKit ledger records about the scripted session. Empty when the header does not identify one. |
| `valid` | Whether the run is valid evidence |
| `invalid` | Why it is not. An invalid run has no results |
| `results` | Contract id to its result: `outcome`, `policy`, `verdicts` and `notes` |
| `reading` | The reading taken, utterance to model call ([`contracts.md`](contracts.md#25-when-the-ledger-allows-more-than-one-reading)) |
| `readings` | How many readings the ledger allows |
| `outcome` | A method: the outcome of one contract |
| `outcomes` | A method: contract id to outcome |
| `failures` | Every failed `Verdict`. Empty for an invalid run, which has no verdicts: read `valid` first, or use `assert_contracts`, which fails on an invalid run |
| `broken` | A method: every rule a failed verdict names, as `CONTRACT/RULE` |
| `lost` | A method: every code of evidence lost a verdict carries, as `CONTRACT/CODE`. An assertion fails on each unless `allow` names it |
| `lines` | A method: the report as lines of text |
| `unjudged` | A method: each contract of those named that has no pass or fail, and why |
| `assert_contracts` | A method: as on a `RunResult`. It also raises for an invalid run |
| `to_json` | A method: the report as plain data (`earful.report/2`) |

An outcome is `'pass'`, `'fail'`, `'unobservable'` (evidence lost) or `'not_exercised'`
(nothing to judge) ([`contracts.md`](contracts.md#5-verdicts)).

### `Verdict`

The outcome of one contract for one instance.

| Attribute | Meaning |
| --- | --- |
| `contract` | The contract id |
| `instance` | What was judged, such as `{"intent": "book-table"}` |
| `outcome` | `'pass'`, `'fail'`, `'unobservable'` or `'not_exercised'` |
| `summary` | One sentence |
| `findings` | Each with the `rule` it names, a `detail` and the `seqs` of its events. A verdict that was not judged has one per reason, with its `code` |
| `evidence` | The `seq` of every event the verdict rests on |
| `witnesses` | The sources of those events |
| `measures` | What was measured |
| `flags` | Qualifications, such as `reading_split` |

### `ContractFailure`

An `AssertionError`, raised by `assert_contracts`. Its text is the report.

### `Predicate`

A contract rule that a run breaks: `Predicate("A2", "rule")`.
`Predicate.parse("A2/rule")` gives the same and raises `ValueError` for a rule no
contract has. `str` of one is `CONTRACT/RULE`.

### `Exploration`

What `explore` returns.

| Attribute | Meaning |
| --- | --- |
| `case` | The case the search started from, as data |
| `assert_contracts` | A method: raises `ContractFailure` if a rule broke, if the search has `no_verdict`, or if the case as given has no pass or fail for a contract in `require`. Takes `allow` and `require` as a `RunResult` does |
| `breaks` | A method: each broken rule as a `Break` |
| `describe` | A method: what the search found, as text |
| `predicates` | A method: every rule some case broke |
| `windows` | A method: how wide a rule's failure is, as a list of windows |
| `coverage` | A method: for each handle, the kinds of transition it was put beside and those it never was |
| `summary` | A method: the counts as plain data |
| `not_judged` | A method: why cases could not be judged, grouped by reason |
| `complete` | False if `max_runs` was spent first |
| `nothing_judged` | Whether the search judged no case: it tried none, or could judge none it tried |
| `by_contract` | A method: for each contract, how many cases judged gave it `pass`, `fail`, `not_exercised` and `unobservable` |
| `errored` | How many of the cases tried raised |
| `unread` | The runs made to find where to put things that could not be judged, each with its `run` (which one) and its `outcome` |
| `unreadable` | How many of those raised or were not valid evidence |
| `no_verdict` | Whether the search cannot pass whatever it found: `nothing_judged`, `errored`, `unreadable`, or not `complete` |

A `Break` has the `predicate`, how many `candidates` break it, whether it breaks
`everywhere`, what was `changed`, its `windows`, an `example` case as data with `what`
was changed in it, the checker's `details` for that example, and `instants_only`.
`run(scenario, data=one.example)` runs the example again.

### `Bundle` and `Replay`

`keep` returns an `earful.core.bundle.Bundle`: its `directory`, `manifest`, `predicate`,
the `minimal` and `original` cases, and `ledger(which)`.

`replay` returns an `earful.core.bundle.Replay`
([`search.md`](search.md#6-the-bundle-earfulbundle1), Replaying).

| Attribute | Meaning |
| --- | --- |
| `judged` | Every run was valid evidence and gave the rule's contract a pass or a fail |
| `reproduced` | Every run was judged and broke the rule |
| `clean` | Every run was judged and the rule's contract passed in each |
| `outcomes` | The contract's outcome in each run, `None` for a run that was not valid evidence |
| `others` | The other rules some run broke, as `CONTRACT/RULE` |
| `codes` | Why the rule's contract was not judged in some run, as codes ([`contracts.md`](contracts.md#5-verdicts)) |
| `broke` | How many runs broke the rule |
| `repeatable` | Every run gave the same ledger |
| `identical` | That ledger is the one the bundle recorded |
| `scenario_changed` | Whether the scenario's module differs from when the bundle was kept. `None` if that is not known |
| `invalid` | Why a run was not valid evidence |
| `unverified` | The case files the manifest records no digest of |
| `framework` | The release it was replayed on |
| `session_scope` | The scripted session used for the replay, when identified by its ledger |

### `CrossCheck`

What `cross_check` returns: `agrees`, `unusable`, the `virtual` ledger, the `real`
ledgers, one comparison for each real-clock run in `comparisons`, the `tolerance`, and
the methods `lines()` and `to_json()`.

## Ledgers without a framework

```python
def check(events) -> Report: ...
def read_ledger(path): ...
def write_ledger(events, path, *, normalized=False): ...
def ledger_hash(events) -> str: ...
```

`check` judges every contract against one ledger, given as its list of events. An invalid
run gives a report with no results. It raises `PolicyError`, a `ValueError`, for a policy
value its contract does not define. `read_ledger` reads a ledger file, one event for each
line. `write_ledger` writes one, and `normalized=True` writes the form that is hashed.
`ledger_hash` is the SHA-256 of that form.

```python
from earful import check, read_ledger

report = check(read_ledger("booking.ledger.jsonl"))
print("\n".join(report.lines()))
```

`__version__` is the installed version of Earful.

## Adapter parts

### `ClockedSink`

LiveKit only. The simulated audio output of a run, and the playout witness: it consumes
the frames the session sends on the virtual clock and records what played. `Case.sink`
takes a subclass of it.

### `Ends`

Pipecat only. What a case's `pipeline` function is handed.

| Attribute | Meaning |
| --- | --- |
| `input` | Earful's input, which scripts the VAD |
| `stt` | The scripted speech-to-text |
| `llm` | The scripted model service, the one `bot` was handed |
| `tts` | The scripted voice |
| `output` | The clocked output |
| `context` | The `LLMContext` that `bot` returned |

## The `earful` command

```
earful [--version] COMMAND ...
```

A scenario is named `module:function` and is imported from the directory the command
runs in. [`testing.md`](testing.md#9-from-the-command-line) describes what each command
prints.

| Exit status | Meaning |
| --- | --- |
| 0 | Nothing broke, the replay did what `--expect` names, or the report was written |
| 1 | A contract rule broke, the replay did not do what `--expect` names, or a real-clock run did not agree |
| 2 | The command was refused. One line on standard error says why |
| 3 | No verdict |

### `earful init`

`earful init AGENT` writes a first test for a livekit-agents agent, named as
`module:Class`, or for a Pipecat bot, named as `module:function`: the function that
registers the bot's functions on its model service and returns its `LLMContext`.

| Option | Meaning |
| --- | --- |
| `-o`, `--output FILE` | Write there. The default is `test_<agent>.py`, or `test_<function>.py` for a bot |
| `--write` | Name a durable write for the generated scenario. Repeat for additional writes. |
| `--scripted-turns` | Optional for LiveKit. Writes the choice of silence-based turn detection into the case. |

### `earful check`

`earful check LEDGER...` prints the verdicts for recorded ledgers. It needs no framework.

| Option | Meaning |
| --- | --- |
| `--json` | One JSON report for each ledger |
| `--measures` | Also print what each verdict measured |
| `--allow CONTRACT/RULE` | A rule that may break. Repeatable |
| `--require CONTRACT` | A contract each ledger must get a pass or a fail for. Repeatable |

### `earful run`

`earful run SCENARIO` runs a scenario once and prints its verdicts. A delay or fault the
case declares for a model call, utterance or write the run never reached is named on an
`unapplied` line under the heading, and in the JSON under `unapplied`.

| Option | Meaning |
| --- | --- |
| `--case FILE` | Run a saved case (`earful.case/1`) |
| `--seed N` | The seed. Default 0, or the one the case holds: a different one is refused |
| `--ledger FILE` | Write the ledger there |
| `--json` | The report as JSON |
| `--allow CONTRACT/RULE` | A rule that may break. Repeatable |
| `--require CONTRACT` | A contract the run must get a pass or a fail for. Repeatable |

### `earful search`

`earful search SCENARIO` searches a scenario.

| Option | Meaning |
| --- | --- |
| `--case FILE` | Search from a saved case |
| `--seed N` | The seed. Default 0, or the one the case holds: a different one is refused |
| `--variables A,B` | The kinds of time to move, or `none` |
| `--faults A,B` | The faults to add to each backend write, or `none` |
| `--pairs` | Also try two changes at once |
| `--profiles N` | Seeded provider-timing profiles |
| `--no-words` | The end of a played word is not a boundary |
| `--max-runs N` | Stop after N runs. A search cut short exits 3 unless a rule broke |
| `--wait-ms MS` | How long the caller of a faulted write waits. Default 1000 |
| `--stretch-ms MS` | How long `--pairs` makes a slow write or generation. Default 1500 |
| `--delay-limit-ms MS` | The longest a delay or a wait is made. Default 10000 |
| `--no-windows` | Do not measure how wide each failure is |
| `--keep CONTRACT/RULE` | Reduce a case that breaks the rule and write it as a bundle. Needs `--out`. Repeatable |
| `--out DIR` | Where bundles go |
| `--workers N` | Worker processes. The default is one for each processor, or what `EARFUL_WORKERS` says. 0 searches in the command's own process |
| `--json` | The whole result as JSON |
| `--allow CONTRACT/RULE` | A rule that may break. Repeatable |
| `--require CONTRACT` | A contract the case as given must get a pass or a fail for. Repeatable |

### `earful replay`

`earful replay BUNDLE_DIR` checks a bundle and runs its reduced case again.

| Option | Meaning |
| --- | --- |
| `--repeats N` | How many runs. Default 3 |
| `--expect reproduced` or `clean` | Exit 1 unless the rule still breaks, or no longer does |
| `--json` | The outcome as JSON |

### `earful crosscheck`

`earful crosscheck SCENARIO` or `BUNDLE_DIR` runs a case on the virtual clock and on the
real one.

| Option | Meaning |
| --- | --- |
| `--case FILE` | Run a saved case |
| `--seed N` | The seed. Default 0, or the one the case holds: a different one is refused |
| `--runs N` | Real-clock runs. Default 3 |
| `--tolerance-ms MS` | How far an event may be from its virtual time. Default 100 |
| `--words N` | Words a segment that was cut short may differ by. Default 1 |
| `--out DIR` | Write every run's ledger there |
| `--json` | The result as JSON |

### `earful report`

`earful report LEDGER` or `BUNDLE_DIR` draws a run as one HTML page, or writes it out as
text ([`report.md`](report.md)). It needs no framework.

| Option | Meaning |
| --- | --- |
| `--which minimal` or `original` | For a bundle: the reduced case or the one first found. Default `minimal` |
| `-o`, `--output FILE` | Write there. The default is standard output |
| `--format html` or `text` | The default is HTML for a file named `.html` and text otherwise |

## The pytest plugin

Installing Earful registers the plugin. It adds the `earful` fixture and two options
([`testing.md`](testing.md#8-with-pytest)).

| Option | Meaning |
| --- | --- |
| `--earful-out DIR` | Where the evidence of failing tests is written. Default `.earful`, under pytest's root directory |
| `--earful-workers N` | Worker processes for a search that names no number. 0 runs it in the test's process. Default `auto` |

The fixture chooses the adapter from the scenario it is given.

```python
class Earful:
    def run(self, scenario, *, data=None, seed=None): ...
    def explore(
        self,
        scenario,
        *,
        data=None,
        seed=None,
        workers=None,
        variables=None,
        faults=None,
        pairs=False,
        profiles=0,
        speeches=None,
        moments=None,
        words=True,
        max_runs=None,
        planned=None,
        delay_limit_us=None,
        wait_us=None,
        stretch_us=None,
    ): ...
    def keep(self, found, rule, directory=None, *, repeats=3): ...
    def replay(self, directory, *, repeats=3): ...
```

Each takes what the adapter's function of that name takes and returns what it returns.
`explore` without `variables` or `faults` uses the adapter's defaults. Without `workers`
it uses the number `--earful-workers` gives, or the adapter's default, and searches in the
test's process when pytest-xdist has spread the test run over the processors. `keep`
without a directory writes the bundle under the test's evidence directory. When the test fails, the fixture writes the ledger and the report of each run
and of each broken rule's example there.
