# Troubleshooting

This page is keyed on the message Earful prints. Each message is quoted as livekit-agents
1.8.13 or pipecat-ai 1.16.0 gives it, with the names of the quickstart's booking example.
Search the page for a few words of yours.

A message reaches you in one of three ways. The `earful` command prints a refusal as one
line that starts with `earful:` and exits 2. A test gets the same text as a `ValueError`.
A run that is not valid evidence raises `InvalidRun` in a test, and the command prints
each reason after `invalid:` and exits 3.

## Installing and importing

### The framework is not installed

```text
ImportError: earful.livekit needs livekit-agents, which is not installed. Install it with: pip install "earful[livekit]"
```

`pip install earful` installs the part that reads ledgers and nothing else. Install the
extra of the framework the agent uses: `earful[livekit]` or `earful[pipecat]`. On
Python 3.10 the `pipecat` extra installs Earful alone, since pipecat-ai needs 3.11.

From the command, a scenario whose module imports a framework that is not installed is
refused with the extra to install:

```text
earful: scenario 'test_booking:booking' could not be imported: it needs livekit-agents, which is not installed. Install it with: pip install "earful[livekit]"
```

### The framework release is outside the range

```text
ImportError: earful.livekit supports livekit-agents from 1.5.6 up to, and not including, 1.9, and 1.9.0 is installed. Install a supported release with: pip install "earful[livekit]"
```

The adapter refuses to import on a release its extra does not accept
([`compatibility.md`](compatibility.md)). Install a release in the range.

### The framework release is not tested

```text
UntestedRelease: Earful is not tested on livekit-agents 1.8.11. The tested releases are: 1.5.6, 1.5.7, 1.6.5, 1.6.6, 1.8.12, 1.8.13
```

A warning, given once when the adapter is imported. The release is in the range and no
test of Earful has run on it. Runs are still checked for validity. To silence it once the
suite passes on that release:

```python
import warnings

from earful.scripted.releases import UntestedRelease

warnings.simplefilter("ignore", UntestedRelease)
```

## A scenario that is refused

Nothing has run when one of these is printed.

### The scenario's name

```text
earful: a scenario is named module:function, such as test_booking:booking; got 'test_booking'
```

```text
earful: no scenario 'test_booking:bookings': module 'test_booking' has no attribute 'bookings'
```

```text
earful: scenario 'test_booking:model' is not a function that takes no arguments and returns a Case: model is of type ScriptedModel
```

The command takes a scenario as `module:function`, where the function takes no arguments
and returns a `Case`. The module is imported from the directory the command runs in.

### The scenario's own code raises

```text
earful: scenario 'test_booking:booking' could not be built: TypeError: Case.__init__() got an unexpected keyword argument 'speechs'. Did you mean 'speeches'? (test_booking.py, line 41)
```

The module raised while it was imported, or the function raised while it built the case.
The file and the line are those of the scenario.

### The agent is an object

```text
earful: agent is called once for each run and returns the agent, so it is a class or a function; got an object of BookingAgent. Write agent=lambda: BookingAgent(...)
```

Each run needs a fresh agent, so `Case.agent` is what builds one.

### The case contradicts itself

```text
earful: the speeches at 0.5 s and 2.0 s overlap
```

```text
earful: the user hangs up at 1.0 s, before the speech at 0.5 s is over
```

```text
earful: unknown backend mode 'idempotent'; choose from ['plain', 'keyed']
```

```text
earful: L1: unknown policy values ['slack']; it defines ['max_steps', 'slack_us']
```

Each names what is wrong and what is accepted. A search does not build such cases: a
value that gives no case is skipped and counted as impossible.

### An effect names a tool the agent does not have

```text
earful: effects are declared for ['create_booking'], which are not function tools of the agent the run starts with; it has ['create_reservation']
```

```text
ValueError: effects are declared for ['create_reservation'], which are not functions the bot registers; it registers []
```

The first is LiveKit's and the second Pipecat's. A key of `Case.effects` has to be the
name the model calls the tool by. The message lists the names the agent or the bot has. On
Pipecat an empty list means `bot` registered no function on the model service it was
handed.

### A session or agent option Earful does not model

```text
earful: session_kwargs may not set 'min_endpointing_delay': use Case.min_endpointing_delay
```

```text
earful: session_kwargs 'mcp_servers' reaches an MCP server over the network
```

```text
earful: BookingAgent sets endpointing min_delay=0.8 and the case gives the session 0.5: set Case.min_endpointing_delay to the agent's value
```

LiveKit. A run takes only the options it models, and the run's header states them. Set
the option through the `Case` field the message names. An option that is refused with a
reason cannot be run: run the agent without it.
[`compatibility.md`](compatibility.md#the-options-earful-models-on-livekit) lists every
option and what Earful does with it.

### The run lost the evidence for a contract

```text
the run lost the evidence for A2/outside-backend
```

A verdict could not be judged because evidence it needs was lost, and the code after the
slash says which ([`contracts.md`](contracts.md#5-verdicts)). `outside-backend`: a tool
for an intent was called and nothing reached Earful's backend, so the tool writes some
other way; declare it an effect, or have it write through `effect`.
`audio-unattributed`: a reply's text was altered on its way to the voice. `tool-unwitnessed`:
a tool the model called is not one Earful witnesses. When the loss is expected, name it in
`allow`, as a rule is named. A search reports each as a rule of its own, with the change
that caused it.

### A required contract had nothing to judge

```text
L2 had nothing to judge: no reply was under way, or waiting to play, while the user held the floor (no-reply-under-way)
```

The run never reached what the contract judges, so `require` fails. For L2, have the user
speak while a reply plays. For L1, `run-ended` means the run ended before the deadline:
give the scenario more `drain_delay`.

### A rule or a contract that does not exist

```text
earful: no contract rule 'A3'. A rule is CONTRACT/RULE, one of: A1/rule, A1/in-flight, A2/rule, A3/rule, M1/rule 2, M1/rule 3, M1/rule 4, M1/rule 5, M2/rule a, M2/rule b, M2/rule c, M2/rule d, M2/rule e, M2/rule f, L1/milestone 1, L1/milestone 2, L1/milestone 3, L2/rule a, L2/rule b; or evidence lost, CONTRACT/CODE, one of: A1/outside-backend, A2/outside-backend, A3/outside-backend, A3/audio-unattributed, A3/readings-over-cap, A3/word-start-unknown, M1/audio-unattributed, M1/readings-over-cap, M1/memory-unaligned, M1/history-before-playout, M2/no-stable-text, M2/tool-unwitnessed, L1/audio-unattributed, L1/readings-over-cap, L1/tool-unwitnessed
```

```text
earful: no contract 'A9'. The contracts are: A1, A2, A3, M1, M2, L1, L2
```

`allow` and `--keep` take a rule, such as `A3/rule`, or a code of evidence lost, such as
`A2/outside-backend`. `require` takes a contract, such as `A3`.

### The scenario cannot be imported

```text
ValueError: to keep a failure as a bundle, the scenario has to be importable: define it as a function at the top level of a module and pass that function, or its module:function name. A Case object, a lambda and a function defined inside another can only run in the process that built them
```

A worker process and a saved bundle build the scenario again from its name. The same
text, starting `to search in worker processes`, is raised by `explore(case, workers=4)`
for a scenario built inline ([`testing.md`](testing.md#6-where-a-scenario-lives)).

### A run started from async code

```text
RuntimeError: an Earful run starts its own event loop and cannot be started from inside a running one: call it from a function that is not async
```

`run`, `explore`, `replay` and `cross_check` are plain functions. Call them from a test
that is not `async def`.

### `effect` called outside a run

```text
RuntimeError: effect('book-table') was called outside an Earful run: there is no backend to write to
```

A stand-in service that calls `effect` works only inside a run. Code that uses the
stand-in elsewhere, such as a unit test of the tool alone, needs another stand-in.

## A run that is invalid

An invalid run finished and yields no verdict
([`ledger.md`](ledger.md#validity)). In a test:

```text
earful.scripted.case.InvalidRun: run is invalid: harness error: work left the virtual clock (threads started: 1)
```

`exc.reasons` lists the reasons and `exc.result` holds the run, with its ledger.

### The scripted model called a tool the agent does not offer

```text
invalid: harness error: call 1: the scripted model called 'create_reservatoin', which the agent does not offer; it offers ['create_reservation']
```

```text
invalid: harness error: call 1: the scripted model called 'create_reservation' with the arguments ['party']; the tool takes ['party_size'] and needs ['party_size']
```

The script is wrong. Correct the name or the arguments in the rule that makes the call.

### The scripted model called a tool where the request allowed none

```text
invalid: harness error: call 4: the request allowed no tool call (tool_choice='none'), and the scripted model called ['look_up_hours']. livekit-agents asks so once max_tool_steps rounds of tool calls have run: script a reply without a call for that request
```

A real model answers such a request in words. Give the model a rule that answers without
a call once the tool has run, or raise `max_tool_steps` in `session_kwargs`.

### The scripted model has no rule for a context

```text
invalid: harness error: policy raised on call 1: NoRule: no rule matches a context whose latest user message is 'A table for four tonight, please.' and which holds 0 tool result(s); rules: user_said('menu',). Add a rule for it, or otherwise()
```

The framework showed the model a context the script does not cover. Add a rule, or end
the model with `.otherwise(say=...)`. In a search this is expected of some cases: a moved
speech can lead to a context the script was not written for. Those cases are listed as
not judged.

Any other exception a policy raises is reported the same way, as
`policy raised on call 1: RuntimeError: ...`.

### A claim does not fit its reply

```text
invalid: harness error: call 1: claim Claim(intent='book-table', first_word=0, last_word=5) does not fit reply 'Booked.'
```

A claim's words are counted from 0 and `last_word` is included. The reply here has one
word.

```text
invalid: schema: event 27 (llm.response): claim names undeclared intent 'book-table'; the scenario has to list it among its intents
```

A claim names an intent of the case: one in `Case.intents`, or one an `Effect` declares.

### Work left the virtual clock

```text
invalid: harness error: work left the virtual clock (threads started: 1)
```

```text
invalid: 1 pieces of work left the virtual clock for a thread
```

Something in the agent's code, or in a tool, used a thread, a socket or a child process.
That work takes real time, which the virtual clock cannot move
([`compatibility.md`](compatibility.md#the-virtual-clock)). The second message is for
`asyncio.to_thread` and `loop.run_in_executor`. Give the agent a stand-in for the service
that does the work, or declare the tool an effect so that its body does not run.

### A tool whose calls cannot be recorded

A reason of this form names the tools:

```text
invalid: harness error: call 1: the model is offered ['look_up_hours'] without the witness
```

LiveKit. Earful records the function tools of the agent and of the session. A tool
inside a toolset is offered to the model with no record of its calls. A function tool
listed on the agent or on the session itself is recorded.

### A clock read that could not be made virtual

```text
invalid: harness error: __main__.Unbuilt.at reads the wall clock and could not be given the virtual one
```

A pydantic model has a field whose default is a clock read, and its validator could not
be rebuilt. The message names the model and the field
([`compatibility.md`](compatibility.md#the-virtual-clock)).

## A run that raises

An exception from the scenario's own code during a run fails a test as it is. The
command refuses it in one line, with the file and the line of the scenario, and exits 2:

```text
earful: scenario 'test_booking:booking' could not be run: RuntimeError: no database (agent.py, line 12)
```

An agent whose constructor raises is the common case. An exception that Earful itself
raised is not caught: the command prints the traceback, and that is a bug to report.

A run in which nothing is scheduled and nothing can happen is ended after 60 real seconds:

```text
VirtualTimeStall: virtual-time loop idle for 60 real seconds with nothing scheduled: the run is deadlocked (check the framework's error log)
```

Something awaits an event that no timer and no scripted provider will produce. A tool
that waits on a real service is the usual cause.

## A contract with no pass or fail

### Not exercised

```text
booking (seed 0, livekit-agents 1.8.13) has no pass or fail for the required A2

A2 was not exercised: the run has no write request for a declared intent
```

`require=["A2"]` asked for a verdict and the run held nothing for A2 to judge. A contract
is exercised when the run has:

| Contract | At least one |
| --- | --- |
| A1 | speech that retires an intent and whose transcript was delivered |
| A2 | write request for a declared intent |
| A3 | claim whose last word was played |
| M1 | reply or spoken text whose playout finished or was abandoned |
| M2 | model call |
| L1 | delivered speech that expects a reply |
| L2 | speech the VAD reported that is neither noise nor a backchannel |

For A2 and A3 the usual causes are a scripted model that never calls the tool, and a
tool whose write does not reach Earful's backend ([`testing.md`](testing.md#3-side-effects)).

### Unobservable

```text
A2 is unobservable: a tool for the intent was called but nothing reached the backend
```

The tool ran and made no write through `effect`, so what it did cannot be known
([`contracts.md`](contracts.md#26-a-tool-that-leaves-nothing-in-the-backend)). Hand the
agent a stand-in that calls `effect`, or declare the tool an `Effect`. Each contract's
section in `contracts.md` has a row, Unobservable, that lists its other causes.

## A search

### The case cannot be run as written

```text
earful: the case as given cannot be searched: harness error: policy raised on call 1: NoRule: no rule matches a context whose latest user message is 'A table for four tonight, please.' and which holds 0 tool result(s); rules: user_said('menu',). Add a rule for it, or otherwise()
```

A search starts from one valid run of the scenario as written. Make `earful run` of it
valid first.

### No case could be judged

```text
booking on livekit-agents 1.8.13: 1 cases tried, 0 judged, 0 contract rule(s) broken

not judged: 1 of 1 cases, which leaves the search with no result
```

Every case the search tried was refused, raised or gave an invalid run. The lines that
follow give each reason with its count and an example. The assertion fails and the
command exits 3, since such a search shows nothing about the contracts.

```text
no case was tried, which leaves the search with no result
```

The search was asked for nothing it could move: no variable, no fault, or speeches and
moments the case does not reach. Widen it.

### A run made to find where to put things could not be judged

```text
not read: 1 run(s) made to find where to put things could not be judged, so the cases they would place were not tried; the search fails
    the case as given, without speech 1: invalid: harness error: policy raised on call 1: NoRule: ...
```

Before it moves a speech, a search runs the case without that speech, and before it
moves the hangup, the case without the hangup. It also runs the case under each fault,
and with each delay stretched. The cases each such run would place are made from its
ledger, so when the run is invalid or raises, none of them is tried, and the search
fails. Most often the scripted model has no rule for the context the run reaches: give
it one, or an `otherwise()`. To leave a speech out, pass `speeches=` with the others; to
leave a fault out, pass `faults=` without it.

A search with some cases not judged and others judged passes, unless one of them raised:

```text
booking on livekit-agents 1.8.13: 60 cases tried, 58 judged, 0 contract rule(s) broken

not judged: 2 of 60 cases; the search fails, since 2 raised
    2 error: RuntimeError: boom
        such as backend.timeout 1, commit first
```

A case that raises is an error in the scenario's code or in Earful
([`search.md`](search.md#2-the-same-failure-a-predicate)).

### The budget ran out

```text
incomplete: the budget of 20 runs was spent with 586 case(s) not run. Nothing is concluded about them
```

`max_runs` stopped the search. Raise it, or narrow the search with fewer variables
([`search.md`](search.md#3-the-boundary-scheduler), How many runs).

### An unknown variable or fault

```text
earful: unknown variables ['speech.start']; choose from ['speech.position', 'speech.end', 'speech.stt_delay', 'llm.ttft', 'llm.duration', 'tts.ttfb', 'backend.latency', 'user.hangup']
```

```text
earful: unknown faults ['backend.slow']; choose from ['backend.error', 'backend.timeout', 'backend.drop']
```

### Nothing was kept

```text
earful: --keep needs --out, the directory to write bundles to
```

```text
not kept, since no case broke them: L2/rule b
```

```text
ValueError: the search found no case that breaks L2/rule b
```

A bundle is written for a rule the search found broken. The second line is the command's
and the third is what `keep` raises.

### The test `earful init` wrote fails on A2

`earful init` ends its output with:

```text
           its search fails (A2) while a write has no idempotency key declared;
           the file says where to declare one
```

The generated model calls the tool again after a failure, and no tool body runs, so the
second call writes twice. This is the test doing its work. If the tool sends an
idempotency key its service honours, declare both in the file: `Effect(..., key=...)`
for the tool and `backend_mode="keyed"` on the case.

## Worker processes

```text
earful.scripted.api.WorkersUnavailable: a worker process died before any had run a case. A worker is a fresh interpreter that imports this process's main module, so a script that searches with workers must do so under `if __name__ == "__main__":`, and cannot be run from standard input or an interactive session. workers=0 runs the search in this process
```

Put the search under the guard, or pass `workers=0`. Under pytest and from the `earful`
command nothing is needed.

A sandbox or a container that may not start processes gives:

```text
earful.scripted.api.WorkersUnavailable: worker processes cannot be started here (PermissionError: [Errno 13] Permission denied). workers=0 runs the search in this process
```

`earful search` then searches in its own process and says so, unless `--workers N` was
given. The pytest fixture searches in the test's process unless a number was given.

A case that ends the worker running it gives:

```text
earful.scripted.api.WorkersUnavailable: a worker process died after the workers had run 1 cases. Something in the case it was running ended the process: memory ran out, or native code crashed. workers=0 runs the search in this process, where that case can be seen
```

```text
earful: EARFUL_WORKERS is a number of processes, 0 for none; got 'many'
```

```text
ERROR: --earful-workers takes a number of processes or auto; got many
```

The environment variable takes a number. The pytest option takes a number or `auto`.

## Bundles and replays

### A bundle that is not there or was changed

```text
earful: cannot read failures/lk-1.8.13.booking.A2-rule/manifest.json: No such file or directory
```

```text
earful: failures/lk-1.8.13.booking.A2-rule/minimal.case.json is not the case the manifest records: its digest is 2e3c54615c7f10dc20f6cfa748dd46a989bfeb5f2ae91ccf86f1a9e27dcb27ef, and the manifest has b57976836c39e4b8c46be18020c285424bee074930feb2974dbb76d09390b7c5
```

Every file of a bundle is checked against its manifest before anything runs
([`search.md`](search.md#6-the-bundle-earfulbundle1), Reading). A file that was edited is
refused. Keep the failure again to change it.

### The replay says the scenario changed

```text
  scenario code changed yes
```

The module that defines the scenario's function differs from when the bundle was kept.
Any edit to that file shows here, a comment included. The replay's other lines still
describe the code as it is now.

### The replay is neither reproduced nor clean

```text
  breaks the rule       not judged
  A2 gave               not_exercised
  other rules broken    L2/rule b
```

`earful replay` exits 3, whatever `--expect` says, when a run was not valid evidence,
when the rule's contract had nothing to judge or could not judge in a run, or when the
runs differ. Read the `invalid:` lines it prints and the contract's outcome. A contract
with nothing to judge means the reduced case no longer reaches what the rule is about:
the scenario's code changed, or the release behaves another way before that point. A
test of a fix asserts `clean`, which needs a pass.

### A bundle that does not show the failure it names

```text
earful: failures/lk-1.8.13.booking.A2-rule/minimal.ledger.jsonl does not break A2/rule under earful.contracts/2: the bundle does not show the failure it names
```

The recorded ledger of the reduced case does not break the rule the manifest names. The
manifest was edited, or the bundle was kept under an earlier version of the contracts
whose rule judged that ledger otherwise; the message then names both versions. Keep the
failure again with this Earful.

## `earful init`

```text
earful: test_booking_agent.py is already there; move it, or name another file with -o
```

```text
earful: agent:Reservations is not a class of agent (a subclass of livekit.agents.Agent). `earful init` writes a test for a livekit-agents agent class or a Pipecat bot function
```

```text
earful: CannotBeBuilt() raised RuntimeError: no configuration. `earful init` builds the agent to read its tools
```

`earful init` builds the agent to read its tools, with `None` for each argument its
constructor requires. If that raises, it reads the tools from the class and says so in
its output. The last message is for a constructor that requires no argument and raises.

```text
earful: `earful init` cannot write a call of the tool 'redeem': it has no value for the argument 'voucher.code' that the tool accepts (String should match pattern '^[A-Z]{3}-[0-9]{4}$')
```

The scripted call needs a value for each required argument, and none it knows fits this
one. Write the test by hand from [`testing.md`](testing.md).

```text
earful: needs_a_service(llm, reservations=None) raised AttributeError: 'NoneType' object has no attribute 'connect'. `earful init` calls the bot to read the functions it registers
```

Pipecat. `earful init` calls the bot function with a model service of its own and `None`
for each argument after it, to see what it registers. Give it a function that registers
the bot's functions without using those arguments, or one that takes only the model
service.

## A Pipecat pipeline that is refused

```text
ValueError: TurnAnalyzerUserTurnStopStrategy, Pipecat's default, runs a turn model on the user's audio, and Earful sends none: give UserTurnStrategies stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=...)]
```

```text
ValueError: the pipeline has to hold ends.output once, at its top level: Earful scripts and witnesses the run there. In order: ends.input, ends.stt, a user context aggregator, ends.llm, ends.tts, ends.output
```

A case's own pipeline is checked before the run starts, and each refusal is one line
that says what to change. [`pipecat.md`](pipecat.md#what-runs-and-what-does-not) lists
what a pipeline cannot hold.

## Where the evidence is

A test that fails with the `earful` fixture writes the ledger and an HTML report of each
run under `.earful/`, in a directory named after the test, and its failure output ends
with the paths ([`testing.md`](testing.md#8-with-pytest)). From the command,
`earful run SCENARIO --ledger FILE` writes the ledger of a run, valid or not, and
`earful report FILE -o report.html` draws it.
