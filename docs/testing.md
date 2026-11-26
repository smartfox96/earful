# Writing a test

For a Pipecat pipeline, see [Writing a Pipecat scenario](pipecat.md).

A test gives Earful a scenario, runs it against a real `AgentSession` on the virtual
clock, and asserts the seven contracts (`docs/contracts.md`). This page covers the parts
of a scenario, the calls a test makes, the `earful` command and the pytest fixture.
`docs/search.md` covers what a search does and what a saved failure holds.

[`api.md`](api.md) lists every name and option with its signature.
[`troubleshooting.md`](troubleshooting.md) is keyed on the messages a test or the command
prints. [`limits.md`](limits.md) says what a test cannot show, and
[`glossary.md`](glossary.md) defines the terms used here.

Everything a test of a LiveKit agent needs is imported from `earful.livekit`. What needs
no framework (checking a recorded ledger, the `effect` call of a stand-in service) is
imported from `earful`.

```python
import livekit.agents as lk

from earful.livekit import Case, Effect, ScriptedModel, UserSpeech, explore, run
from earful.livekit import tool_returned, tool_succeeded, user_ever_said

from myapp.agent import BookingAgent  # the agent as the application ships it

model = (
    ScriptedModel()
    .when(tool_succeeded(), say="Your table is booked.", claims=["book-table"])
    .when(tool_returned(), say="Sorry, I could not make that booking.")
    .when(
        user_ever_said("table"),
        say="One moment.",
        call=("create_reservation", {"party_size": 6}),
    )
    .otherwise(say="How can I help?")
)

speeches = [UserSpeech(start=0.5, end=2.5, transcript="Book a table for six tonight.")]

case = Case(
    name="booking",
    agent=BookingAgent,
    effects={"create_reservation": Effect("book-table")},
    speeches=speeches,
    policy=model,
)


def test_a_booking_is_made_once_and_confirmed_truthfully():
    run(case).assert_contracts()        # the scenario as written
    explore(case).assert_contracts()    # every timing and fault near it
```

## 1. The scenario

A `Case` holds everything that defines a run. The same case and seed give the same
ledger.

Earful builds the LiveKit session with separate scripted providers and lets silence
end a turn. Each result states which services and turn detection Earful used. If your application uses a turn-taking model, its decisions are outside
this test. Speech-to-speech sessions are unsupported.

| Field | What it is |
| --- | --- |
| `name` | Names the case in the ledger and in messages |
| `agent` | Builds the agent under test. Takes no arguments: an agent class, or a function that constructs one. Called once for each run |
| `speeches` | The scripted user: each `UserSpeech` has a `start` and an `end` in seconds from the first audio frame, and the `transcript` the scripted STT delivers. `backchannel`, `expects_reply` and `retires` say what the turn is (`docs/contracts.md`, section 4) |
| `policy` | The scripted model: a function from the context the framework presents to a `Reply` |
| `effects` | Tools that stand for one durable write, by tool name (section 3) |
| `intents` | Logical actions and how many durable effects each may have. Needed for an intent no declared effect names, or to authorize more than one effect |
| `hangup` | When the user hangs up, in seconds. The session is then closed |
| `delays`, `faults` | Provider delays and backend faults declared as data. A search adds and moves these itself |
| `contracts` | Policy values for the contracts, where the defaults do not fit |

The remaining fields set the scripted voice, the VAD and the session's turn handling.
The `Case` class lists them with their defaults.

A tool that deliberately waits can declare its completion allowance in microseconds.
For a three-second availability lookup, pass
`contracts={"L1": {"tool_wait_us": {"look_up_availability": 3_000_000}}}` to `Case`.
The allowance starts at the tool's finish check and applies only in a round that called it.

## 2. The model

The model is scripted. A reply depends only on the context the framework shows the
model, so the same context gets the same reply in every run, and a reply changes only
when the framework presents something different. The memory contracts measure what
the framework presents.

`ScriptedModel` writes the model as ordered rules. The first rule whose condition holds
gives the reply. `otherwise` gives the reply when none does. A context that no rule
matches is an error in the scenario and makes the run invalid.

So does a call the script got wrong. Each tool call of a reply is checked against the
tools the framework offers the model for that call: a name none of them has, an argument
the tool does not take, or a missing argument it needs makes the run invalid, and the
reason says what was expected:

```
invalid: harness error: call 1: the scripted model called 'create_reservatoin', which the agent does not offer; it offers ['create_reservation']
invalid: harness error: call 1: the scripted model called 'create_reservation' with the arguments ['party']; the tool takes ['party_size'] and needs ['party_size']
```

Argument names are checked and their values are not. On Pipecat the names are checked
for a function whose parameters the context declares (`docs/pipecat.md`), and a function
registered by name alone takes whatever the script gives it.

| Condition | Holds when |
| --- | --- |
| `user_said("stop")` | The latest user message contains the phrase, in any case |
| `user_ever_said("table")` | Some user message in the context contains it |
| `tool_returned("name")` | The context holds a result of that tool, a success or an error |
| `tool_succeeded("name")` | It holds a result of that tool that is not an error |
| `tool_failed("name")` | It holds results of that tool and all are errors |

The tool name can be left out to mean any tool. Conditions combine with `&`, `|` and
`~`. Any function from a `Context` to a bool is a condition: `Context` gives
`user_turns`, `last_user_turn`, `assistant_turns` and `tool_results` as typed values.

```python
from earful.livekit import tool_failed

retried_once = tool_failed("create_reservation") & (lambda c: len(c.tool_results) == 1)
```

A reply is given by its parts:

| Argument | Meaning |
| --- | --- |
| `say` | The text the model generates |
| `call` | A tool call: a tool name, a name with its arguments as a dict, or a list of these |
| `claims` | Intents the text tells the user succeeded. Contract A3 checks each against the backend |
| `ttft`, `duration` | The model's time to first token and total generation time, in seconds |

A policy can also be a plain function. It receives the context as a list of item
summaries and returns a `Reply`:

```python
from earful.livekit import Claim, Reply, ToolCall

def policy(items: list[dict]) -> Reply:
    if any(item["type"] == "function_call_output" for item in items):
        return Reply(text="Your table is booked.", claims=(Claim("book-table"),))
    book = ToolCall("create_reservation", '{"party_size": 6}')
    return Reply(text="One moment.", tool_calls=(book,))
```

## 3. Side effects

Contracts A1 to A3 are about durable effects, such as a booking. Earful
records them in its own backend, separately from what any tool returns, and a search
makes that backend slow, failing or lossy. The agent's writes have to reach it. There are
two ways, and neither changes how the agent is constructed.

**The agent is handed the service it writes to.** Give it a stand-in whose method calls
`effect` where the real service would make the write durable. The tool's own code runs:
its arguments, its error handling and its return value are the application's.

```python
from earful import effect
from earful.livekit import Intent

class FakeReservations:
    async def create(self, party_size: int) -> str:
        return await effect("book-table", {"party_size": party_size})

case = Case(
    name="booking",
    agent=lambda: BookingAgent(FakeReservations()),
    intents=[Intent("book-table", tools=("create_reservation",))],
    speeches=speeches,
    policy=model,
)
```

`effect` returns a receipt. It raises `BackendError` if the scenario declares that the
backend rejects the write, and `BackendTimeout` if it declares that the caller stops
waiting. Outside a run it raises `RuntimeError`.

**The tool talks to its service itself.** Declare the tool as an effect. In a run, a call
of the tool makes one write in place of the tool's body. Nothing in the agent changes.

```python
case = Case(
    name="booking",
    agent=BookingAgent,
    effects={
        "create_reservation": Effect(
            "book-table",
            key=lambda args: f"table-for-{args['party_size']}",
            result=lambda receipt, args: f"Reservation {receipt} confirmed.",
        )
    },
    speeches=speeches,
    policy=model,
)
```

| `Effect` argument | Meaning | Default |
| --- | --- | --- |
| `intent` | The logical action the write is for | required |
| `payload` | What is written, from the call's arguments | the arguments |
| `key` | The idempotency key: a string, or a function of the arguments | no key |
| `result` | What the tool returns, from the receipt and the arguments | the receipt |
| `latency` | How long the backend takes to commit, in seconds | 0 |

The same declaration can sit on the tool, for a team that wants it beside the code.
Outside a run the decorated tool is the tool as written:

```python
from earful.livekit import effect_tool

class BookingAgent(lk.Agent):
    @lk.function_tool
    @effect_tool("book-table")
    async def create_reservation(self, party_size: int) -> str: ...
```

A scenario's declaration for a tool is used over the tool's own. A scenario that names a
tool the agent does not have is refused before it runs. The ledger header lists the tools
whose bodies were replaced.

A write commits at its own time whatever happens to the tool that asked for it, so the
ledger shows a tool that was cancelled or timed out and a write that committed anyway.

## 4. One run

`run(case)` runs the scenario once and returns the result. `result.report` holds the
verdicts, `result.events` the ledger, and `result.assert_contracts()` raises
`ContractFailure`, an `AssertionError`, if a rule broke:

```
inline_booking (seed 0, livekit-agents 1.8.13) breaks A3/rule

A1  not_exercised
A2  pass
A3  fail
    fail: {"reply": 2, "intent": "book-table-1", "words": [0, 3]} the user heard that 'book-table-1' succeeded before the backend recorded it
        rule: the claim was heard at 9780000 us; the first effect for 'book-table-1' was never committed
M1  pass
    pass: {"reply": 2} 4 of 4 words played; remembered 4
        flags: judged_on_final_history
M2  pass
L1  pass
L2  pass
```

`assert_contracts(allow=["L2/rule b"])` lets a named rule break, for a known defect of a
release under test. A rule is named by its contract and by what the report calls it:

| Contract | Rules |
| --- | --- |
| A1 | `A1/rule`, `A1/in-flight` |
| A2 | `A2/rule` |
| A3 | `A3/rule` |
| M1 | `M1/rule 2`, `M1/rule 3`, `M1/rule 4`, `M1/rule 5` |
| M2 | `M2/rule a`, `M2/rule b`, `M2/rule c`, `M2/rule d`, `M2/rule e`, `M2/rule f` |
| L1 | `L1/milestone 1`, `L1/milestone 2`, `L1/milestone 3` |
| L2 | `L2/rule a`, `L2/rule b` |

A failed contract fails the assertion, and so does evidence lost: a verdict that is
`unobservable` because, say, a tool wrote outside Earful's backend fails it as
`A2/outside-backend` unless `allow` names that. A contract that is `not_exercised` had
nothing to judge and does not fail, so a run in which the model never calls the booking
tool writes nothing, leaves A2 not exercised and passes. `require` names the contracts a
test is about. Each must end with a pass or a fail:

```python
result = run(case)
result.assert_contracts(require=["A2", "A3"])
```

```
booking (seed 0, livekit-agents 1.8.13) has no pass or fail for the required A2

A2 was not exercised: the run has no write request for a declared intent
```

The verdicts follow, as above. A rule in `allow` or a contract in `require` that does not
exist raises `ValueError`, which lists those that do. A run that is not valid evidence
raises `InvalidRun` and yields no verdicts (`docs/ledger.md`, Validity).

## 5. A search

`explore(case)` moves each time the scenario controls to just before, at and just after
every moment the system changes state, and adds each backend fault to each write
(`docs/search.md`, section 3). `assert_contracts()` on the result raises if any run
broke a rule, and says what was changed to reach the failure and how wide it is:

```
inline_booking on livekit-agents 1.8.13: 857 cases tried, 857 judged, 1 contract rule(s) broken
Earful built the session with scripted speech recognition, model replies, speech synthesis and silence-based turn detection. Application entry code was not run.
    898 run(s): 6 to find where to put things, 853 for the cases, 39 to measure the failures
    by contract: pass, fail, nothing to judge, evidence lost
    A1      0     0   857     0
    A2    846     0    11     0
    A3    674    89    94     0
    M1    853     0     4     0
    M2    857     0     0     0
    L1    786     0    71     0
    L2      0     0   857     0

A3/rule  in 89 case(s)
    the claim was heard at 9780000 us; the first effect for 'book-table-1' was never committed
    changed: backend.error 1
    also by changing: backend.drop 1; backend.drop 1 + backend.wait 1; backend.timeout 1, commit first + backend.latency 1
    breaks with backend.wait 1 under backend.drop 1 from 0.000 s (no case exists beyond it) to 30.000 s (still breaks where the search stopped)
    breaks with backend.latency 1 under backend.timeout 1, commit first from 6.781 s (passes beyond it) to 36.790 s (still breaks where the search stopped)
```

A rule that breaks only where two events fall on the same microsecond is reported and
not counted (`docs/search.md`, section 4). A rule the case breaks as written counts,
whether or not a case the search tried breaks it.

Cases that could not be judged are listed after the rules, grouped by reason, each with
its count and an example. A case whose run raised an exception fails the assertion: that
is an error in the scenario's code or in Earful, and the part of the search it stood for
was not looked at. Cases that were refused, or whose runs were not valid evidence, are
listed and do not fail it, unless none of the cases tried could be judged: such a search
shows nothing about the contracts.

On a search, `require` is asked of the case as written: the run the search starts from
must give each named contract a pass or a fail. The cases a search tries are not held to
it. A fault can remove what a contract judges, as a rejected write leaves no confirmation
for A3 to check.

`found.breaks()` gives each broken rule with an example case, as data. `run(case,
data=example)` runs that example again.

The options narrow a search: `variables` (which times to move), `faults` (which faults to
add), `speeches`, `moments`, `words`, `pairs` and `profiles`. `docs/search.md`, section
3, says what each does, and how a search reports the number of cases it is about to run.
The default search above is 857 cases.

## 6. Where a scenario lives

A scenario is given in one of three forms.

| Form | Worker processes | Can be saved as a bundle |
| --- | --- | --- |
| A `Case` object | No | No |
| A function that returns a `Case`, defined in a test or as a lambda | No | No |
| A function at the top level of a module, passed as the function or as `"module:function"` | Yes | Yes |

`explore` without `workers` gives a scenario that can be imported one worker process per
processor, and runs the other forms in the process that calls it. A worker process and a
saved bundle both build the scenario again from its name, so both need a scenario that
can be imported. `explore(case, workers=4)` and `keep(...)` refuse one that cannot, before
anything runs, and say what to change. A search also checks what it is asked for
(the case, the variables, the faults) before it starts a worker, and raises
`WorkersUnavailable` where worker processes cannot be started at all.

`explore(scenario, workers=N)` runs the cases in N worker processes, and `workers=0` in the
process that calls it. On a 15-core machine
the 857 cases of section 5 take 18 s in the process of the test and 5 s in 15 workers. The
workers are started for the search and stopped when it ends, so each search runs the
scenario's code as it is at that moment. Each worker is a fresh interpreter that imports
the main module of the process that started it, so a script that searches with workers
needs the `if __name__ == "__main__":` guard. Where workers cannot be started, a search that
names no number runs in the process that calls it. Under pytest nothing is needed
(section 8).

Passed as a function, a scenario is built anew for each run. Passed as a `Case`, it is
copied for each run: its agent is built by its own factory each time, and its policy is
one object for all runs. A `ScriptedModel` cannot be changed and carries nothing between
calls. A policy function that keeps state of its own needs the function form.

## 7. Keeping a failure

Run the capture below on livekit-agents 1.5.6, where this booking breaks A2.

```python
from earful.livekit import explore, keep, replay

from earful.livekit.cases import book_then_interrupt

found = explore(book_then_interrupt)                # a top-level function
kept = keep(found, "A2/rule", "failures/double-booking")
again = replay("failures/double-booking")
assert again.reproduced and again.identical
```

`keep` reduces a case that breaks the rule to the smallest that still does, runs it three
more times, and writes it as a bundle if every run gives the ledger that broke the rule
(`docs/search.md`, section 6). A failure that does not repeat raises `ValueError` and
nothing is written.

`replay` checks every file of the bundle, then runs the reduced case again on whatever
release is installed. `reproduced` says that every run broke the rule, and `identical`
that the ledger is the one recorded. Once the defect is fixed, the bundle is the
regression test:

```python
from earful.livekit import replay


def test_the_double_booking_stays_fixed():
    assert replay("failures/double-booking").clean
```

`clean` says that every run was judged and the rule's contract passed in each. Assert
`clean`, and never `not reproduced`: a replay whose runs were not valid evidence, or in
which the contract had nothing to judge, is not reproduced either, and it is not clean.
For a saved rule, `judged` means every replay gave its contract a pass or a fail.
For a saved loss, each run can also answer by observing that loss.

`scenario_changed` says whether the module that defines the scenario's function differs
from when the bundle was kept. It covers that one file. An agent or a service stand-in
imported from another module is outside it (`docs/search.md`, section 6).

## 8. With pytest

Installing Earful registers a pytest plugin. Its `earful` fixture has the same `run`,
`explore`, `keep` and `replay`, and keeps the evidence of a test that fails:

```python
def test_a_booking_is_made_once_and_confirmed_truthfully(earful):
    earful.run(case).assert_contracts()
    earful.explore(case).assert_contracts()
```

When the test fails, the ledger of each run it made, and an example case with its ledger
for each rule a search of its found broken, are written under `.earful/`, in a directory
named after the test. Each ledger has a report beside it, one HTML page to open in a
browser (`docs/report.md`). The failure output ends with a section that says where:

```
---------------------------------- earful ----------------------------------
run 1 (booking) breaks A3/rule
    ledger  .earful/test_booking.py-test_with_a_failed_write/run-1.booking.ledger.jsonl
    report  .earful/test_booking.py-test_with_a_failed_write/run-1.booking.html
search 1 (booking) breaks A3/rule: backend.error 1
    case    .earful/test_booking.py-test_searched/search-1.booking.A3-rule.case.json
    ledger  .earful/test_booking.py-test_searched/search-1.booking.A3-rule.ledger.jsonl
    report  .earful/test_booking.py-test_searched/search-1.booking.A3-rule.html
```

A test's directory is cleared when the test starts, so it holds the evidence of its last
run and nothing older. A test that passes writes nothing, and leaves no directory.
`.earful/` belongs in `.gitignore`.

| Option | Meaning |
| --- | --- |
| `--earful-out DIR` | Where the evidence goes. Default `.earful`, under pytest's root directory |
| `--earful-workers N` | Worker processes for a search that does not name a number; 0 runs it in the test's process. Default `auto` |

With `auto`, `earful.explore` gives a search one worker process per processor if its
scenario can be imported (section 6), and runs it in the test's process otherwise. Three
things keep a search in the test's process under `auto`: a scenario built inline, a test
run that pytest-xdist has already spread over the processors, and a machine where worker
processes cannot be started. A number, on the command line or in the call, is always taken
as given.

`EARFUL_WORKERS=N` in the environment gives `auto`, and `earful search`, a number to use
in place of one per processor. Set it where several test runs share a machine, since each
otherwise starts a worker for every processor. `EARFUL_WORKERS=0` keeps every search in its own process.

`earful.keep(found, "A3/rule")` without a directory writes the bundle under the test's
evidence directory.

In CI the evidence is worth keeping, since the machine that ran the test is gone when the
job ends. On GitHub Actions, two steps upload it when a test fails:

```yaml
      - run: pytest
      - uses: actions/upload-artifact@v4
        if: failure()
        with:
          name: earful-evidence
          path: .earful/
          include-hidden-files: true
          if-no-files-found: ignore
```

`include-hidden-files` is needed: the action leaves out a directory whose name starts
with a dot, and without it the upload is empty. The artifact holds each failing test's
ledgers, cases and reports. A report is one self-contained HTML page, so it opens
straight from the downloaded archive.

## 9. From the command line

```bash
earful init myapp.agent:BookingAgent
earful run myapp.scenarios:booking --ledger booking.ledger.jsonl
earful check booking.ledger.jsonl
earful search myapp.scenarios:booking --keep A3/rule --out failures
earful replay failures/lk-1.8.13.booking.A3-rule --expect reproduced
earful report failures/lk-1.8.13.booking.A3-rule -o report.html
```

| Command | Does |
| --- | --- |
| `earful init AGENT` | Writes a first conversation test requiring L1. Use an importable `module:Class` for LiveKit, or `module:function` for Pipecat. Setup lists the tools it can see. Declare durable writes by name with `--write TOOL` (repeatable) to generate a write and retry test requiring A2, A3 and L1. Required constructor or bot arguments receive `None`; setup refuses a factory that cannot run this way. Use `-o FILE` to choose the destination. |
| `earful check LEDGER...` | Prints the verdicts for recorded ledgers. `--measures` adds what each verdict measured, `--json` prints one report per line |
| `earful run SCENARIO` | Runs a scenario once and prints its verdicts. `--case FILE` runs a saved case, `--ledger FILE` writes the ledger |
| `earful search SCENARIO` | Searches a scenario, in one worker process per processor unless `--workers` says otherwise. Where worker processes cannot be started, as in some sandboxes and containers, it says so and searches in its own process; with `--workers N` given it refuses. `--keep CONTRACT/RULE --out DIR` writes a bundle for a rule that broke, in a directory of `DIR` named after the framework release, the scenario's function and the rule: `lk-1.8.12.booking.A3-rule` on livekit-agents, `pc-1.16.0.booking.A3-rule` on pipecat-ai. `--variables`, `--faults`, `--pairs`, `--profiles`, `--no-words` and `--no-windows` narrow or widen the search. It prints how many cases it is about to run on standard error, and `--max-runs N` stops it after N runs: a search cut short is incomplete and exits 3 unless a rule broke. With `--no-windows` a rule that breaks only at single instants counts as broken, and the output names the rules that were not measured |
| `earful replay BUNDLE_DIR` | Checks every file of a bundle against its manifest, then runs its reduced case again. `--expect reproduced` exits 0 if every run broke the rule, `--expect clean` if its contract passed in every run, and 1 otherwise. A replay with a run that was not valid evidence or gave the contract no pass or fail, or with runs that differ, exits 3 whatever is expected. `--repeats N` sets the number of runs |
| `earful crosscheck SCENARIO` or `BUNDLE_DIR` | Runs a case once on the virtual clock and `--runs` times on the real clock, and says whether the real-clock runs agree with the virtual one (section 10). `--tolerance-ms` and `--words` set how far they may differ, `--out DIR` writes every run's ledger |
| `earful report LEDGER` or `BUNDLE_DIR` | With `-o FILE.html`, draws the run as one HTML page: a timeline, the verdicts and every event (`docs/report.md`). Without it, writes the same out as text. `--format` names one or the other |

A scenario is named `module:function` on the command line, so it has to be importable
from where the command runs. `check` and `report` read files and work without the
framework installed. `--allow CONTRACT/RULE` on `check`, `run` and `search` lets a named
rule break. `--require CONTRACT` on the same three asks for a pass or a fail of that
contract, of each ledger, of the run, or of the case a search starts from. Both are
checked before anything runs.

| Exit status | Meaning |
| --- | --- |
| 0 | Nothing broke, or the replay did what `--expect` names. For `report`: the report was written |
| 1 | A contract rule broke, the replay did not do what `--expect` names, or a real-clock run did not agree with the virtual one |
| 2 | The command could not be carried out: bad arguments, a file that is not what it should be, a bundle with a file that is not what its manifest records, a scenario that does not exist, cannot be imported, built or run, or contradicts itself |
| 3 | No verdict: evidence was lost without its code being allowed, a run was not valid evidence, did not repeat, or could not be compared, a required contract got no pass or fail, a case of a search raised, a search could judge none of the cases it tried, or its budget of runs cut it short |

A rule that broke outranks a run without a verdict.

A refusal is one line on standard error, and it says what was expected. An exception
from the scenario's own code, while its module is imported or its function builds the
case, is refused the same way, with the file and the line that raised it:

```
earful: a scenario is named module:function, such as test_booking:booking; got 'test_booking'
earful: scenario 'test_booking:model' is not a function that takes no arguments and returns a Case: model is of type ScriptedModel
earful: scenario 'test_booking:booking' could not be built: TypeError: Case.__init__() got an unexpected keyword argument 'speechs'. Did you mean 'speeches'? (test_booking.py, line 41)
earful: agent is called once for each run and returns the agent, so it is a class or a function; got an object of BookingAgent. Write agent=lambda: BookingAgent(...)
earful: unknown backend mode 'idempotent'; choose from ['plain', 'keyed']
```

An exception from the scenario's own code once a run has started, such as an agent whose
constructor raises, is refused the same way:

```
earful: scenario 'test_booking:booking' could not be run: RuntimeError: no database (agent.py, line 12)
```

An error in Earful itself is shown whole, as a traceback.

## 10. On the real clock

Every run above is on a virtual clock: time moves only when the harness moves it, which
is what makes a run repeat. A production system has no such clock, so a finding is worth
one more check before it is reported. `cross_check` runs the same case on the real clock,
with nothing patched, and compares each of those runs with the virtual one.

```python
from earful.livekit import cross_check
from earful.livekit.cases import book_then_interrupt

checked = cross_check(book_then_interrupt, runs=3)
print("\n".join(checked.lines()))
```

```
book_then_interrupt on livekit-agents 1.5.6
on the virtual clock it breaks A2/rule, M2/rule d
3 of 3 real-clock runs agree with the virtual run, within 100 ms and 1 word(s)
run 1: agrees; breaks A2/rule, M2/rule d; largest drift 19.4 ms
run 2: agrees; breaks A2/rule, M2/rule d; largest drift 19.4 ms
run 3: agrees; breaks A2/rule, M2/rule d; largest drift 42.9 ms
```

A real-clock run agrees with the virtual run if it matches on six things.

| Aspect | What has to match |
| --- | --- |
| order | The same events happened. Two events further apart on the virtual clock than the time tolerance came in the same order |
| timing | Each event, from the user's first audio on, is within the time tolerance of its virtual time |
| audible | Each segment said the same text and was or was not cut short. A segment cut short played the same number of words, give or take the word tolerance |
| memory | Each model call was shown the same items, and the final history holds the same. A reply that was cut short may be remembered shorter or longer by the word tolerance |
| effects | The same writes were requested, with the same outcome each |
| verdicts | The same contract rules broke |

The tolerances are declared: `Tolerance(time_us=100_000, words=1)` by default. Session
start-up is left out of the timing, since it takes no virtual time and as long as the
machine takes.

A real-clock run takes as long as the conversation it scripts, and it is never evidence
on its own: `validity` marks every one invalid and `check` gives it no verdicts. The
cross-check reads it only against the virtual run. A real-clock run that is incomplete
for another reason, such as playout that ran ahead of its audio, cannot be compared, and
the result says so.

Run cross-checks on a machine that is doing nothing else. Other work delays the event
loop, and the delay shows as drift.
