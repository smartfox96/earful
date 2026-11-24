# Writing an adapter

The shared kernel drives one case and writes the ledger. Each adapter supplies the
operations that require a real session or pipeline. Start with the smallest case that
produces an observable reply, then compare the recorded inputs, playout and final
history with the contract you expect that case to exercise.

## The ledger it writes

[`ledger.md`](ledger.md) defines every event, and section 8 of
[`contracts.md`](contracts.md#8-what-the-ledger-must-record) lists what the contracts
need from it. An event left out reads as the thing not having happened, or makes the run
invalid. An adapter is done when its ledgers validate (`earful.core.schema`), the checker
gives its reference cases the verdicts they are known to have, and the same case gives
the same ledger in every run and on every platform.

Two rules decide most of the design:

- **The reference record comes from Earful's side.** The scripted user, model and voice,
  and the output, each write what they did. The framework's own account goes in `fw.*`
  events, which explain a run and decide no verdict. An adapter replaces providers and
  the clock, and leaves the framework's behaviour alone.
- **The run stays on the virtual clock.** Every time the framework reads, every sleep and
  every timeout runs on `earful.scripted.clock`. Work that leaves the clock, a thread or
  a socket, makes the run invalid and says so, because its timing is real and unrepeatable.

## Binding a run

The public `run_case` entry point in `harness.py` passes the framework name and version,
case validator, builder and result constructor to `earful.scripted.kernel.run.run`.
Keep adapter options on the adapter's `Case` in `case.py`. The shared case holds the
script, and the shared result supplies the ledger, verdicts and contract assertions.

Implement `build(case, context)` in `ports.py` as an async context manager. It receives
the run's ledger, backend, declared delays, clock choice and error list. Construct a
fresh session or worker, install observers and yield the following bound operations.
Restore every installed observer and backend binding when the context exits, including
after an exception.

| Operation | What the adapter supplies |
| --- | --- |
| `config()` | The scenario header read from the built application, including starting history and effective options |
| `start()` | Start the session or worker and enable scripted input |
| `drive(wait)` | Deliver the scenario, handle hangup and drain according to the framework. Await `wait()` when waiting for the framework to become idle |
| `quiet()` | Read live activity, including outstanding tools and playout. The kernel polls this query on its clock |
| `closed()` | An awaitable for the framework's own shutdown, with the reason when available |
| `history()` | The final memory as `read_from` and ordered `items` |
| `close()` | Finish shutdown and collect output diagnostics |

The kernel writes `run.start` before startup, `history.final` after driving the input,
and `run.end` after leaving the builder's context. It checks the completed ledger and
returns the result. The LiveKit harness adds its clock and warning diagnostics.

## Connecting the framework

- `earful.scripted.parties` supplies the scripted user, timed voice-activity events,
  transcripts, model fragments and stamped synthesis. Translate these into framework
  frames or stream items in `providers.py`. Keep the order in which the framework
  receives an event and the independent witness records it.
- `sink.py` reads the stamps out of the audio and records playout. `tools.py` wraps the
  functions the application registers and records invocations and outcomes.
- Put each read of private framework state, release-specific import and attribute
  fallback behind a named operation in `seams.py`. Exercise it on every supported
  release. `tests/test_core_boundary.py` checks the source boundaries.
- Pipecat also binds its worker clock in `clock.py` and inspects the application's
  processor graph in `pipeline.py`.

## Connecting search and replay

In `api.py`, pass four arguments to `earful.scripted.api.AdapterAPI` in order. Supply the
scenario module, `run_case`, a module-level worker function that accepts case data and
returns ledger events, and a context manager that silences the framework during search.
The fourth argument defaults to `nullcontext`. Export the resulting `run`, `explore`,
`keep`, `replay` and `cross_check` functions through the package.

The scenario module supplies these operations to the API and its callers.

| Name | Responsibility |
| --- | --- |
| `Scenario.of(source)` | Accept a case, factory or module reference. The instance provides `make`, `reference`, `default(seed)`, `build(data)` and `run(data)` |
| `describe(case, reference, seed)` and `build(data, make)` | Convert between the adapter's case and `earful.case/1` using its settings table |
| `default(reference, seed)` | Describe the author's original case for reduction |
| `run(data)` | Run a saved case and return its events, including evidence of an invalid run |
| `importable(reference, purpose)` | Require a reference a separate process can resolve |
| `pool(workers)` | Create the worker pool |
| `environment()` | Identify the installed environment in a saved bundle |
| `fingerprint(reference)` | Identify the scenario's source for replay |

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

1. Record the package, module, extra, supported range and tested releases in
   `releases.toml`. Align `scripted.releases.SUPPORT`, the `pyproject.toml` extra, lock
   files, CI matrix and compatibility table with that list.
2. Register the package in `earful._adapters` for dispatch and in
   `earful.scripted.scenario._FRAMEWORKS` for missing-dependency messages. Export the
   adapter's public case, scenario, result and API from its package.
3. In `earful.cli`, connect `_init` to the adapter's scaffold through its ports and
   supply the short name for bundle directories. Update the fixture's type imports in
   `pytest_plugin.py` so typed callers can pass the new case or scenario.
4. Add the reference cases to `scripts/repeat.py`, register the framework in
   `scripts/ran_every_test.sh`, and run a CI cell with only that framework installed.
   Record the tested releases and checks in the compatibility guide. [Compatibility](compatibility.md)

## Tests

A test file that needs the framework starts with `pytest.importorskip` of it, before its
other imports, and CI selects it from that import. A file with no such
line imports no framework: `tests/test_core_boundary.py` holds every test file to that.
Each new rule of the adapter gets a test on its edge and a deliberate break in
`tests/mutants.py` that the test catches. The contracts' own tests read ledgers and need
no framework.
