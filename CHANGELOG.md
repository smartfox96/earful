# Changelog

What changes for someone who uses Earful, release by release. The format is
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the version numbers follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The ledger, case and bundle
formats have versions of their own
([`docs/ledger.md`](https://github.com/smartfox96/earful/blob/main/docs/ledger.md), Versioning).

## [Unreleased]

- Existing LiveKit cases disclose the scripted providers and turn detection. Recognized realtime setups are refused by model name.
- L1 accepts intentional tool waits in `tool_wait_us`.
- Handoffs with uncertain context ownership report `handoff-context`. Later turns are still checked.
- First tests exercise a conversation. Declare a durable write with `earful init --write TOOL`.

## [0.3.1] - 2026-11-24

LiveKit and Pipecat share the case fields and run lifecycle. Both reproduce the
reference ledgers from 0.3.0.

## [0.3.0] - 2026-11-23

### Runs

- A case is checked before it runs, on both adapters, and one that cannot run is refused
  in one line: a time that is negative, not a number or beyond the clock's range, a
  transcript that is not text, `expects_reply` on a speech with no words, an `nth` that
  is not a whole number, an intent whose `tools` is not a list of names, and
  `reissue_allowed` naming an intent the case does not declare. An intent that names a
  tool the agent does not have is refused too.
- A scripted reply whose `ttft` is longer than its `duration` makes the run invalid.
- A run whose tool calls `sys.exit` is refused in one line that names the tool.
- On LiveKit, `userdata` in `session_kwargs` is copied for each run, so every run of a
  case starts from the same data. A value that cannot be copied is refused.
- On Pipecat, a bot that returns no `LLMContext` is refused, and a processor that reports
  an error makes the run invalid once, under the processor's name.
- A backend write whose payload is not a JSON object, or whose key is not a string, is
  refused and takes no request number.
- A ledger file is read as UTF-8, with or without a byte order mark.
- `run_virtual` works with `pip install earful` alone: it reads pydantic models only
  where the application has imported pydantic.

### Contracts

- A verdict that fails keeps the code of each piece of evidence it also lost.
- A ledger is not valid when a number that must be an integer is a float, when it holds
  no final history or more than one, when a tool call carries a call id no model response
  asked for, when a receipt is committed twice, or when a playout repeats a word stamp.

### Search

- A search's first line counts the rules that fail it, without the ones `allow` names.
- A scenario that calls `sys.exit`, or meets an error of the operating system, is a case
  the search reports as refused.
- A reduced case keeps each speech the author wrote that the failure did not change, also
  where several speeches have the same words, and holds no difference from the author's
  case that the failure can do without.

### Bundles and replay

- A kept loss of evidence replays. `earful replay` prints `loses the evidence`, and counts
  a run that shows the loss when another repeat gives a verdict. Evidence lost for other
  contracts is listed apart from the other rules broken (`other_evidence_lost` in JSON).
- A run from a saved case leaves the scenario's own case as it was.

### Command line and pytest

- `earful run` and `earful check` exit 1 when a rule is broken, and 3 when evidence was
  lost and no rule broke, unless `--allow` names each lost code.
- Refused in one line with exit status 2: a ledger whose `schema` is not text, a ledger
  that is not valid given to `earful report`, a directory given where a file is read or
  written, `search --out` without `--keep` or naming a file, `--stretch-ms` without
  `--pairs`, and `crosscheck` of a bundle with `--case`.
- The command prints none of Pipecat's debug log.
- The pytest fixture keeps the evidence of each test in a directory of its own, also for
  tests whose ids differ only in characters a directory name leaves out. Its failure text
  names the evidence a run lost.

### Documentation

- [`docs/limits.md`](https://github.com/smartfox96/earful/blob/main/docs/limits.md#known-defects-of-earful)
  lists the situations in which Earful reports a wrong result, and a test holds the list
  to the expected failures in the repository.
- The stand-in service of the testing page declares its intent, and
  `examples/pipecat_booking` offers its model the booking tool.
- Every page states the one pipecat-ai series that is supported, 1.16.

## [0.2.0] - 2026-11-22

### Frameworks

- A Pipecat adapter, `earful.pipecat`, for pipecat-ai 1.16 on Python 3.11 or later:
  `pip install "earful[pipecat]"`. A case can run its own pipeline around Earful's
  scripted ends (`Case.pipeline`). [`docs/pipecat.md`](https://github.com/smartfox96/earful/blob/main/docs/pipecat.md) covers a
  first scenario, a bot that speaks first, and what cannot run.
- `earful.scripted` holds what both adapters share: the virtual clock, the scripted user,
  model and voice, and a case's intents, delays and faults.
- An adapter refuses to import on a framework release outside the range its extra
  accepts, and warns once (`UntestedRelease`) on an untested release inside it.
  livekit-agents 1.8.13 is tested.
- A LiveKit run takes only the session and agent options Earful models, and refuses any
  other in one line before it starts
  ([`docs/compatibility.md`](https://github.com/smartfox96/earful/blob/main/docs/compatibility.md#the-options-earful-models-on-livekit)).
  `Case.allow_interruptions` and `Case.preemptive_generation` set those two session
  options. An agent that becomes active during the run is held to the same table.
- A LiveKit run ends once the agent is quiet, with no tool running and no reply generated
  or played, waiting up to `drain_timeout`.
- On LiveKit a scripted reply keeps to what the request allows. A tool call under
  `tool_choice="none"`, none under `"required"`, another tool than the one named, or two
  calls under `parallel_tool_calls=False` makes the run invalid.
- On Pipecat a function the bot registers after `bot()` returns makes the run invalid.
  A pipecat-ai release that keeps the private names the scripted model reads elsewhere
  is refused before the run.
- Reference scenarios with committed ledgers in `earful.livekit.cases` and
  `earful.pipecat.cases`, and `examples/pipecat_booking`, the booking example as a
  Pipecat bot.

### Runs

- A run is invalid if it starts a thread or a child process, waits on a file descriptor,
  is woken from another thread, or a scripted provider raises. `time.monotonic`,
  `time.monotonic_ns` and `time.perf_counter_ns` read the virtual clock
  ([`docs/compatibility.md`](https://github.com/smartfox96/earful/blob/main/docs/compatibility.md#the-virtual-clock) lists every
  read).
- A scripted model call to a tool the agent does not offer, or with an argument the tool
  does not take, makes the run invalid.
- A run is invalid if a segment reaches the output at another sample rate than the
  voice's, or a word arrives with part of its audio missing or with more audio than it
  has.
- The ledger header records the history an agent starts with (`history`), which the
  contracts leave out of every context, when the framework ends a turn
  (`turn_end_delay_us`), and a tool call that hands over to another agent (`handoff`).
  `earful.ledger/1` is at `schema_minor` 1.
- A ledger's hash does not depend on the logging level or on an OpenTelemetry tracer
  provider. `framework_warnings` counts every warning livekit-agents logs.
- `run`, `explore` and `cross_check` take a case as data in `data=`, run with the seed it
  holds. A different `seed` with it is refused.

### Contracts

- The contracts are `earful.contracts/2`
  ([`docs/contracts.md`](https://github.com/smartfox96/earful/blob/main/docs/contracts.md)).
- A verdict that was not judged carries a code: nothing to judge gives `not_exercised`,
  evidence lost gives `unobservable`. Evidence lost fails `assert_contracts` and a search
  unless `allow` names its `CONTRACT/CODE`, and a search reports it as a rule with the
  change that caused it. `assert_contracts(require=[...])` fails when a named contract
  got no pass or fail. The report format is `earful.report/2`.
- M1 rule 2 fails a reply that played to its end and is absent from memory, whatever
  `behind_us` is.
- M2 rule f: a tool call or result shown to the model has the name and the arguments of
  the call the model made. Rule e holds tool items and replies to their place among the
  user turns. Rule d loses its evidence for a tool that ran without the call id the model
  asked for.
- M2 and L1 judge a conversation that is handed over to another agent.
- L2 holds a reply that starts at the interruption point to the stop bound, and has
  nothing to judge for a speech with no reply under way or waiting to play.
- A1, A2 and A3 are unobservable for a tool call that leaves nothing in the backend.

### Search

- A search prints how many cases it is about to run, and `--max-runs` (`max_runs=`)
  stops it there. A search stopped by its budget is incomplete and fails.
- Its report gives each contract's counts of pass, fail, nothing to judge and evidence
  lost, and how many runs it made and what for.
- A search fails when it judged none of its cases, tried none, a case raised, or a run it
  made to find where to put things could not be judged. It says why each case was not
  judged.
- With `pairs`, a search puts a second fault on a write that exists only because the
  first one failed.
- The shrinker repeats its passes until a round removes nothing.

### Bundles and replay

- A bundle is written only for a failure that repeats. Its manifest records a digest of
  each file, which `earful replay` checks before it runs. Loading refuses a manifest whose
  rule no contract has, or whose recorded ledger does not break it.
- A replay is reproduced when every run breaks the rule, and clean when the contract
  passed in every run. Otherwise it is not judged, and `earful replay` exits 3. It prints
  the contract's outcome with its code, and any other rule that broke.
- A kept bundle's directory starts with its framework, `lk-` or `pc-`.

### Command line and pytest

- The `earful` command and the pytest fixture choose the adapter from the scenario or the
  bundle they are given.
- `earful init` writes a first test for a livekit-agents agent class or a Pipecat bot
  function, from its tools. The test's model calls a failed tool once more, and its search
  fails on a write repeated with no idempotency key.
- A mistake in a scenario, or a scenario whose own code raises during a run, is refused in
  one line with exit status 2. `allow=` and `--keep` refuse a rule that does not exist.
- The fixture's `explore` takes the adapter's keywords by name, and its `keep` takes
  `repeats`. A test's `.earful/<test>/` directory is cleared when the test starts.
- `help()` shows `run`, `explore`, `cross_check`, `keep` and `replay` as functions of their
  adapter.

### Documentation

- Pages for the API ([`docs/api.md`](https://github.com/smartfox96/earful/blob/main/docs/api.md)), the messages Earful prints
  ([`docs/troubleshooting.md`](https://github.com/smartfox96/earful/blob/main/docs/troubleshooting.md)), every limit
  ([`docs/limits.md`](https://github.com/smartfox96/earful/blob/main/docs/limits.md)), the terms
  ([`docs/glossary.md`](https://github.com/smartfox96/earful/blob/main/docs/glossary.md)), scripting the model
  ([`docs/scripting.md`](https://github.com/smartfox96/earful/blob/main/docs/scripting.md)) and writing an adapter
  ([`docs/adapters.md`](https://github.com/smartfox96/earful/blob/main/docs/adapters.md)).

## [0.1.1] - 2026-11-18

### Added

- `earful init module:Class` writes a first test for an agent from the agent's own
  function tools. The test runs as written.
- `EARFUL_WORKERS` sets how many worker processes a search starts when no number is
  given. The pytest fixture and `earful search` read it.
- [`skills/earful/SKILL.md`](https://github.com/smartfox96/earful/blob/main/skills/earful/SKILL.md),
  one page that tells a coding assistant how to write and read an Earful test.
- `docs/testing.md` shows how to keep a failing test's reports as an artifact in CI.

### Changed

- Under pytest, `--earful-workers` defaults to `auto`: a search of a scenario that can
  be imported starts one worker process per processor. A number on the command line or
  in the call is used as given.
- The README opens with the report of a failing run and links to two example reports.

## [0.1.0] - 2026-11-18

The first release.

### Added

- A livekit-agents `AgentSession` run on a virtual clock against a scripted user, a
  scripted model, a scripted voice and a fake backend, and recorded as a ledger that is
  the same byte for byte on every run. Tested with livekit-agents 1.5.6, 1.5.7, 1.6.5,
  1.6.6 and 1.8.12, on Linux and macOS, Python 3.10 to 3.14.
- Seven contracts decided from the ledger alone: A1, A2, A3, M1, M2, L1 and L2
  ([`docs/contracts.md`](https://github.com/smartfox96/earful/blob/main/docs/contracts.md)).
- A search of the timings and backend faults near a scenario, the reduction of a
  failing run to a small case, and a bundle that replays it.
- A report of a run as one HTML page: a timeline, the verdict of each contract and
  every event.
- The `earful` command (`check`, `run`, `search`, `replay`, `crosscheck`, `report`) and
  the `earful` pytest fixture.
- The formats `earful.ledger/1`, `earful.case/1` and `earful.bundle/1`, each with a
  JSON Schema.

[Unreleased]: https://github.com/smartfox96/earful/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/smartfox96/earful/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/smartfox96/earful/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/smartfox96/earful/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/smartfox96/earful/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/smartfox96/earful/releases/tag/v0.1.0
