# Changelog

What changes for someone who uses Earful, release by release. The format is
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the version numbers follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The ledger, case and bundle
formats have versions of their own
([`docs/ledger.md`](https://github.com/smartfox96/earful/blob/main/docs/ledger.md), Versioning).

## [Unreleased]

### Added

- A Pipecat adapter, `earful.pipecat`, for pipecat-ai 1.16 on Python 3.11 or later.
  Install it with `pip install "earful[pipecat]"`. A case can run its own pipeline around
  Earful's scripted ends (`Case.pipeline`).
  [`docs/pipecat.md`](https://github.com/smartfox96/earful/blob/main/docs/pipecat.md)
  shows a first scenario and lists the limits.
- `examples/pipecat_booking` is the booking example as a Pipecat bot, and
  [`docs/pipecat.md`](https://github.com/smartfox96/earful/blob/main/docs/pipecat.md)
  walks through what a search of it finds and how each result is dealt with.
- `earful.pipecat.cases` holds three reference scenarios, `greeting`, `reserve_table` and
  `barge_in`, as `earful.livekit.cases` holds LiveKit's.
- `Case.allow_interruptions` and `Case.preemptive_generation` on LiveKit, for what the
  deprecated session options of those names used to set.
- `earful.scripted`, the package both adapters share: the virtual clock, the scripted
  user, model and voice, and a scenario's intents, delays and faults. `earful.livekit`
  exports the names it did before, and `earful.livekit.clock` and
  `earful.livekit.playbook` still import.
- `assert_contracts(require=[...])` fails when a named contract got no pass or fail.
- `M2/rule f`: a tool call or result shown to the model has the name and the arguments of
  the call the model made.
- With `pairs`, a search puts a second fault on a write that exists only because the
  first one failed.
- A bundle's manifest records the digest of each case file (`schema_minor` 1).
- `earful search` prints how many cases it is about to run, and `--max-runs` (`max_runs=`)
  stops it there. A search stopped by its budget is incomplete and fails.
- Four pages: an API reference (`docs/api.md`), the messages Earful prints and what to do
  about each (`docs/troubleshooting.md`), every limit in one place (`docs/limits.md`), and
  a glossary (`docs/glossary.md`).
- livekit-agents 1.8.13 is a tested release.

### Changed

- The `earful` command and the pytest fixture choose the adapter from the scenario or
  the kept bundle they are given. A kept bundle's directory is named after its framework
  (`lk-` or `pc-`).
- `allow=` and `--keep` refuse a rule that does not exist. `allow=["A3"]` was ignored
  before and is now an error: name the rule, as in `A3/rule`.
- A search that could judge none of its cases fails, and says why each case was not
  judged. A rule counts as breaking at single instants only if every value at which it
  breaks was measured.
- `earful replay` checks every file of a bundle against its manifest before it runs, and
  a replay with a run that was not valid evidence is neither reproduced nor clean.
  A bundle is written only for a failure that repeats.
- A run that starts a thread or a child process, waits on a file descriptor, or is woken
  from another thread is invalid. An exception inside a scripted provider makes the run
  invalid.
- `time.monotonic`, `time.monotonic_ns` and `time.perf_counter_ns` read the virtual clock
  during a run. [`docs/compatibility.md`](https://github.com/smartfox96/earful/blob/main/docs/compatibility.md)
  lists the reads that are virtual and those that are not.
- A scripted model call to a tool the agent does not offer, or with an argument the tool
  does not take, makes the run invalid.
- A LiveKit run takes only the session and agent options it models, and refuses any
  other in one line before it starts
  ([`docs/compatibility.md`](https://github.com/smartfox96/earful/blob/main/docs/compatibility.md#the-options-earful-models-on-livekit)).
  `session_kwargs` was passed through as it was: a deprecated option such as
  `allow_interruptions` was recorded in the header and ignored by livekit-agents. An
  agent with its own STT, model, voice or VAD is refused, and so is an agent whose
  endpointing or interruption options differ from the case's, which the header states.
  An agent that becomes active during the run is held to the same, and makes the run
  invalid if it breaks it. Set the option through the `Case` field the message names.
- On LiveKit a scripted reply that breaks what the request allowed makes the run
  invalid: a tool call under `tool_choice="none"`, which livekit-agents sends once
  `max_tool_steps` rounds of tool calls have run, no call under `"required"`, another
  tool than the one named, or two calls under `parallel_tool_calls=False`.
- A search in which a case raised an exception fails.
- A thread started with `_thread.start_new_thread` leaves the virtual clock, as a
  `threading.Thread` does, and makes the run invalid. A word that reaches the output
  with more audio than it has, which only a repeat gives, is a stamp error.
- On LiveKit `framework_warnings` counts every warning livekit-agents logs, whatever
  level the application sets on its loggers, so the ledger no longer depends on that
  level. The application's own handlers are passed only the records they would have had.
- On Pipecat a function the bot registers after `bot()` returns makes the run invalid:
  the header, written before the run, could not list it. A pipecat-ai release that keeps
  one of the private names the scripted model reads elsewhere is refused in one line
  before the run, where it raised `AttributeError`.
- A verdict that cannot be judged carries a code, and each code is nothing to judge or
  evidence lost ([`docs/contracts.md`](https://github.com/smartfox96/earful/blob/main/docs/contracts.md#5-verdicts)).
  Nothing to judge, such as a deadline after the run ended, is now `not_exercised`, not
  `unobservable`. Evidence lost, such as a tool that writes outside Earful's backend, is
  `unobservable` and fails `assert_contracts` and a search unless `allow` names its
  `CONTRACT/CODE`; a search reports it as a rule of its own, with the change that caused
  it. So some runs and searches that passed now fail. L2 has nothing to judge for a
  speech with no reply under way or waiting to play, so `require=["L2"]` means an
  interruption, or a reply held back, was judged. A search prints each contract's counts
  of pass, fail, nothing to judge and evidence lost, and a replay prints the code. The
  report format is `earful.report/2`: a finding carries `code`.
- M1 rule 2 fails a reply that played to its end without being cut short and is absent
  from memory, whatever `behind_us` is. A short reply that played in full and was
  forgotten passed under the default tolerance of one second. A cut-off reply is judged
  by its drift as before: the framework only estimates how much of it was heard.
- L2 rule a holds a reply that starts at the interruption point itself to the stop bound.
  Where the floor window opens at that instant, as it does for every speech on Pipecat,
  such a reply was judged by neither rule and could play on over the user.
- A replay is clean only when the bundle's contract passed in every run. A run in which
  that contract had nothing to judge or could not judge leaves the replay not judged, and
  `earful replay` exits 3: before, a replay of an A2 bundle on a run where A2 was not
  exercised and another rule broke was clean. The replay prints the contract's outcome
  and any other rule that broke. Loading a bundle refuses a manifest whose rule no
  contract has, or whose recorded ledger does not break it.
- A search fails when a run it makes to find where to put things raises or is not valid
  evidence, such as the case without a speech it is about to move: the cases that run
  would place were skipped and nothing said so. A search fails when it tried no case. An
  index in `speeches`, a name in `moments` or a number of `profiles` that does not exist
  is refused. A window end at a run that could not be judged says why.
- A mistake in a scenario (a reference that is not a scenario, a speech that ends before
  it starts, an unknown backend mode or fault) is refused in one line with exit status 2.
- Under pytest, a test's `.earful/<test>/` directory is cleared when the test starts.
- The contracts are `earful.contracts/2`: the rules listed here under Added, Changed and
  Fixed changed what the version names
  ([`docs/contracts.md`](https://github.com/smartfox96/earful/blob/main/docs/contracts.md)
  lists them). A bundle kept under `earful.contracts/1` is still replayed, is judged by
  version 2, and the replay says so.
- A1, A2 and A3 are all unobservable for a tool call that leaves nothing in the backend.
  M2 rule e also holds tool items and replies to their place among the user turns. L2
  no longer counts a gap in a streaming buffer as a reply stopping or starting.
- An adapter refuses to import on a framework release outside the range its extra accepts,
  and warns once (`UntestedRelease`) on a release inside the range that is not tested.
- The test `earful init` writes has a model that calls the tool once more after a failed
  call, and requires A2, A3 and L1 to reach a verdict. Its search fails on a write that
  is repeated with no idempotency key declared. `earful init` gives an argument that is a
  pydantic model, a date or an identifier a value the tool accepts, finds tools handed to
  the agent's constructor, and refuses a tool it cannot write a call for.
- `earful run`, `replay` and `crosscheck` refuse a scenario whose own code raises during
  the run, in one line and with exit status 2. It was a traceback and status 1, the
  status of a broken rule. An exception Earful raises is still a traceback.
- A scenario whose module cannot be imported is told apart from one that does not exist,
  and a missing livekit-agents or pipecat-ai is named with the extra that installs it.
- `run`, `explore`, `cross_check` and the fixture's `earful.run` take a case as data in
  `data=`. The keyword was `case=`, which also named the `Case` class.
- `help()` shows `run`, `explore`, `cross_check`, `keep` and `replay` as functions of
  their adapter, with the adapter's `Case` and `RunResult` in the signature.
- The scripted LiveKit VAD sets `speaking` as the Silero plugin does, and a word's audio
  has the same level whatever its stamp.
- A ledger's header states when the framework ends a turn (`turn_end_delay_us`), and a
  search aims there. On Pipecat a search aimed 200 ms early with the default settings.
  `earful.ledger/1` is at `schema_minor` 1 for this field, for `handoff` and for
  `word_timestamps`. Ledgers of minor 0 are read as before.
- M2 and L1 judge a conversation that is handed to another agent. A tool that returns an
  agent is recorded with `handoff`, with the text the model is shown for what came with
  the agent, and as asking for no reply when nothing did. M2 rule d owes that call in the
  final history and in the model call that follows it up, and no longer in the contexts
  of the agent handed over to.
- L1 gives `silenced_by_a_tool` only to a turn that got no spoken reply.

### Fixed

- An agent that starts with history, such as earlier turns in its `chat_ctx` on LiveKit or
  in the context `bot` returns on Pipecat, no longer fails M2 rules a, b and c and L1
  milestone 1 in a valid run. Both adapters record those items as the header's optional
  `history`, and the checker leaves them out of every context it reads.
- A Pipecat reply cut while the model is still generating it keeps its attribution. On
  the interruption Pipecat gives the voice the sentence written so far, which can end
  inside a word, and the text was matched to the reply word by word: the utterance was
  attributed to no reply, and M1 and L1 had no verdict while the assertion passed. A
  synthesized text is now matched to the reply it starts as text.
- A reply played in several segments was first audible when the first one began.
- L1 failed a turn whose milestone came due at the very instant the scenario ended. The
  milestone could still have been met then, so the turn is unobservable.
- A ledger whose header declares overlapping speeches is refused, as a case that
  declares them already was.
- A tuple a LiveKit tool returns is recorded with the text the model is shown, which is
  that of a list. M2 rule d failed on such a tool.
- The scripted LiveKit recognizer declares the interim transcript it sends
  (`interim_results`). A run is invalid if a whole segment reaches the output at another
  sample rate than the voice's, or if a word other than the last arrives with part of
  its audio missing.
- The logging level no longer changes a ledger's hash.
- A ledger's hash replaces only the framework's own identifiers, so application ids that
  look like them are kept.
- A reduced case no longer keeps a speech only because the case it came from had a hangup
  the failure does not need.
- On the real clock a Pipecat reply played each chunk of audio from when its write came,
  so every late wake-up added to its length and a cross-check of a long reply differed.

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

[Unreleased]: https://github.com/smartfox96/earful/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/smartfox96/earful/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/smartfox96/earful/releases/tag/v0.1.0
