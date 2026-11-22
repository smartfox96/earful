# Writing an adapter

An adapter runs one voice framework on Earful's virtual clock, in place of its providers,
and writes what happened as a ledger. Everything else is shared: the contracts, the
search, the shrinker, the bundle, the report and the command line read the ledger and the
case data, and never import a framework. Two adapters exist, `earful.livekit` and
`earful.pipecat`. Read them side by side before starting a third: where they differ is
where the framework does.

## What an adapter is held to

The ledger. [`ledger.md`](ledger.md) defines every event, and section 8 of
[`contracts.md`](contracts.md#8-what-the-ledger-must-record) lists what the contracts
need from it. A run that leaves something out is not judged more kindly: the event's
absence is read as the thing not having happened, or the run is invalid. So an adapter
is done when its ledgers validate (`earful.core.schema`), the checker gives the verdicts
its reference cases are known to have, and the same case gives the same ledger in every
run and on every platform.

Two rules decide most of the design:

- **The reference record does not come from the framework.** When the user started
  speaking, what played and when, what the model was asked: each is written by the
  scripted user, the output or the scripted model, from what they did. The framework's
  own account goes in `fw.*` events, which explain a run and decide no verdict.
- **The run stays on the virtual clock.** Every time the framework reads, every sleep and
  every timeout runs on `earful.scripted.clock`. Work that leaves the clock, a thread or
  a socket, makes the run invalid and says so, because its timing is real and unrepeatable.

## The parts

An adapter is a package of the same parts as `src/earful/livekit/` and
`src/earful/pipecat/`:

| Module | What it does |
| --- | --- |
| `__init__.py` | Imports the framework and refuses a release outside the supported range in one line (`earful.scripted.releases`). Exports the public names, the same as the other adapters' where they mean the same |
| `harness.py` | `Case`, the scenario as code, and `run_case`, which builds the framework's session or pipeline around Earful's providers, runs it on the virtual clock, writes the header and returns a `RunResult` with the ledger and the report |
| `providers.py` | The scripted speech-to-text and VAD (the user's side), the scripted model, and the voice, each in the shape the framework expects a provider to have |
| `sink.py` | The output: consumes audio on the virtual clock, reads each word's stamp back out of the samples, and writes `playout.*` and `sink.*` events |
| `tools.py` | The tool witness: wraps every tool or function the application registers and writes `tool.*` events, and stands in for a declared effect |
| `clock.py` | Only where the framework needs more than the shared virtual clock: Pipecat's binds Pipecat's own clock to the virtual loop |
| `scenario.py` | `Scenario`, a case and how to rebuild it, and the conversion between a `Case` and `earful.case/1` data, so a search can move its times and add faults |
| `api.py` | `run`, `explore`, `keep`, `replay` and `cross_check`, an `earful.scripted.api.AdapterAPI` given the adapter's `run_case` and scenario module |
| `scaffold.py` | What `earful init` writes for the framework, from `earful.scripted.scaffold` |

Most of what a run needs is already shared, in `earful.scripted`:

| Module | What it gives |
| --- | --- |
| `clock` | The virtual event loop, the wall-clock patches, and the count of work that left the clock |
| `world` | The scripted user: each `UserSpeech`, the origin every true time is measured from, and the VAD's segments |
| `model` | Replies, claims and tool calls, and `ReplyRegister`, which attributes a synthesized text to the reply it came from |
| `playbook` | `ScriptedModel` and its conditions |
| `voice` | Stamped audio: each word's samples carry its stamp, and `StampReader` reads them back |
| `case` | Intents, delays and faults, and the checks a case passes before it runs |
| `scenario`, `api`, `releases`, `scaffold` | Resolving `module:function`, the public calls, the release range, the text of a first test |

## The order to build it in

1. **One run of one case, on the virtual clock.** The scripted user speaks, the model
   replies, the voice speaks, the output plays it. Write `run.start` and `run.end`
   first, then each event as its provider does what it records. Run the ledger through
   `earful.core.schema.validate` after every step.
2. **The header.** State every option the contracts depend on, and read each one back
   from the framework after it is built. A difference between what the case declared
   and what the framework holds is a harness error. Refuse, before the run, any option
   the adapter does not model, as the LiveKit adapter does in `options.py` and the
   Pipecat adapter in `pipeline.py`. An option passed through unmodelled is a verdict
   that rests on nothing.
3. **Tools and effects.** Witness every tool, however the application registers it, and
   list the witnessed ones in the header: a call that leaves no `tool.called` was never
   made only if the tool was witnessed.
4. **Interruptions and hangups.** The VAD's start and stop, the framework's pause and
   clear commands at the output, a reply cut short, the user leaving mid-reply.
5. **Repeatable.** The same case and seed give the same normalized ledger in process,
   across processes and on every platform. `scripts/repeat.py` checks it. A difference
   is almost always a time read off the clock, or two events at one instant whose order
   depends on something outside the scenario.
6. **Reference cases.** A few cases that each rest on one behaviour, kept as reference
   ledgers in `tests/golden/` and checked on every supported release, as
   `earful.livekit.cases` and `earful.pipecat.cases` are.

## Registering it

- `earful.scripted.releases.SUPPORT`: the package name, the adapter module, the extra,
  the tested releases and the range the extra accepts.
- `pyproject.toml`: the extra, with the same range.
- `earful._adapters`: the framework's package name and the adapter module, so the
  command line and a bundle find it.
- `earful.cli`: the short name a kept bundle's directory starts with.
- `scripts/repeat.py`: the framework, so its reference ledgers can be checked and
  updated.
- [`compatibility.md`](compatibility.md): the releases tested and what CI runs.
- CI: a job that runs every test in an environment with only that framework installed,
  where a skip other than a file that needs another framework fails the job.

## Tests

A test file that needs the framework starts with `pytest.importorskip` of it, before its
other imports, and is named `tests/test_<framework>_<subject>.py`. A file with no such
line imports no framework: `tests/test_core_boundary.py` holds every test file to that.
Each new rule of the adapter gets a test on its edge and a deliberate break in
`tests/mutants.py` that the test catches. The contracts' own tests read ledgers and need
no framework.

## What not to do

- Do not read the framework's account to decide a verdict. Write it as `fw.*`, and get
  the reference from the scripted user, model, voice and output.
- Do not pass through an option the contracts do not model. Refuse it with what to use
  instead.
- Do not patch the framework's behaviour to make a contract pass. An adapter replaces
  providers and the clock; what the framework does with them is what is tested.
