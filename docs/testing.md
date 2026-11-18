# Writing a test

A test gives Earful a scenario, runs it against a real `AgentSession` on the virtual
clock, and asserts the seven contracts (`docs/contracts.md`). This page covers the parts
of a scenario, the calls a test makes, the `earful` command and the pytest fixture.
`docs/search.md` covers what a search does and what a saved failure holds.

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

case = Case(
    name="booking",
    agent=BookingAgent,
    effects={"create_reservation": Effect("book-table")},
    speeches=[UserSpeech(start=0.5, end=2.5, transcript="Book a table for six tonight.")],
    policy=model,
)


def test_a_booking_is_made_once_and_confirmed_truthfully():
    run(case).assert_contracts()        # the scenario as written
    explore(case).assert_contracts()    # every timing and fault near it
```

## 1. The scenario

A `Case` holds everything that defines a run. The same case and seed give the same
ledger.

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

## 2. The model

The model is scripted. A reply depends only on the context the framework shows the
model, so the same context gets the same reply in every run, and a reply changes only
when the framework presents something different. That is what the memory contracts
measure.

`ScriptedModel` writes the model as ordered rules. The first rule whose condition holds
gives the reply. `otherwise` gives the reply when none does. A context that no rule
matches is an error in the scenario and makes the run invalid.

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

Contracts A1 to A3 are about durable effects: a booking, an order, a refund. Earful
records them in its own backend, separately from what any tool returns, and a search
makes that backend slow, failing or lossy. The agent's writes have to reach it. There are
two ways, and neither changes how the agent is constructed.

**The agent is handed the service it writes to.** Give it a stand-in whose method calls
`effect` where the real service would make the write durable. The tool's own code runs:
its arguments, its error handling and its return value are the application's.

```python
from earful import effect

class FakeReservations:
    async def create(self, party_size: int) -> str:
        return await effect("book-table", {"party_size": party_size})

case = Case(
    name="booking",
    agent=lambda: BookingAgent(FakeReservations()),
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
inline_booking (seed 0, livekit-agents 1.8.12) breaks A3/rule

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
release under test. A run that is not valid evidence raises `InvalidRun` and yields no
verdicts (`docs/ledger.md`, Validity).

## 5. A search

`explore(case)` moves each time the scenario controls to just before, at and just after
every moment the system changes state, and adds each backend fault to each write
(`docs/search.md`, section 3). `assert_contracts()` on the result raises if any run
broke a rule, and says what was changed to reach the failure and how wide it is:

```
inline_booking on livekit-agents 1.8.12: 112 cases tried, 112 judged, 1 contract rule(s) broken

A3/rule  in 29 case(s)
    the claim was heard at 9780000 us; the first effect for 'book-table-1' was never committed
    changed: backend.error 1
    also by changing: backend.drop 1, backend.drop 1 + backend.wait 1, backend.timeout 1, commit first + backend.latency 1
    breaks with backend.latency 1 from 6.781 s (passes beyond it) to 39.558 s (still breaks where the search stopped)
```

A rule that breaks only where two events fall on the same microsecond is reported and
not counted (`docs/search.md`, section 4).

`found.breaks()` gives each broken rule with an example case, as data. `run(case,
case=example)` runs that example again.

The options narrow a search: `variables` (which times to move), `faults` (which faults to
add), `speeches`, `moments`, `words`, `pairs` and `profiles`. `docs/search.md`, section
3, says what each does. A default search of a one-turn booking scenario is about
860 runs.

## 6. Where a scenario lives

A scenario is given in one of three forms.

| Form | Worker processes | Can be saved as a bundle |
| --- | --- | --- |
| A `Case` object | No | No |
| A function that returns a `Case`, defined in a test or as a lambda | No | No |
| A function at the top level of a module, passed as the function or as `"module:function"` | Yes | Yes |

`explore` runs every form in the process that calls it unless workers are asked for. A
worker process and a saved bundle both build the scenario again from its name, so both
need a scenario that can be imported. `explore(case, workers=4)` and `keep(...)` refuse one that cannot,
before anything runs, and say what to change. A search also checks what it is asked for
(the case, the variables, the faults) before it starts a worker, and raises
`WorkersUnavailable` where worker processes cannot be started at all.

`explore(scenario, workers=N)` runs the cases in N worker processes. On a 15-core
machine the 860 runs above take 36 s in the process of the test and 9 s in 15 workers.
The workers are started for the search and stopped when it ends, so each search runs the
scenario's code as it is at that moment. Each worker is a fresh interpreter that imports
the main module of the process that started it, so a script that searches with workers
needs the `if __name__ == "__main__":` guard. Under pytest nothing is needed, and the
`earful` fixture starts the workers by itself (section 8).

Passed as a function, a scenario is built anew for each run. Passed as a `Case`, it is
copied for each run: its agent is built by its own factory each time, and its policy is
one object for all runs. A `ScriptedModel` cannot be changed and carries nothing between
calls. A policy function that keeps state of its own needs the function form.

## 7. Keeping a failure

```python
from earful.livekit import explore, keep, replay

found = explore(book_then_interrupt)                # a top-level function
kept = keep(found, "A2/rule", "failures/double-booking")
again = replay("failures/double-booking")
assert again.reproduced and again.identical
```

`keep` reduces a case that breaks the rule to the smallest that still does, checks that
it gives the same ledger each time, and writes it as a bundle (`docs/search.md`,
section 6). `replay` runs the reduced case again on whatever release is installed:
`reproduced` says whether it still breaks the rule, and `identical` whether the ledger is
the one recorded.

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

A test that passes writes nothing. `.earful/` belongs in `.gitignore`.

| Option | Meaning |
| --- | --- |
| `--earful-out DIR` | Where the evidence goes. Default `.earful`, under pytest's root directory |
| `--earful-workers N` | Worker processes for a search that does not name a number; 0 runs it in the test's process. Default `auto` |

With `auto`, `earful.explore` gives a search one worker process per processor if its
scenario can be imported (section 6), and runs it in the test's process otherwise. The
README's search of 736 cases takes 33 s in the test's process and 13 s with workers, on a
15-core machine. Three things keep a search in the test's process under `auto`: a
scenario built inline, a test run that pytest-xdist has already spread over the
processors, and a machine where worker processes cannot be started. A number, on the
command line or in the call, is always taken as given.

`EARFUL_WORKERS=N` in the environment gives `auto`, and `earful search`, a number to use
in place of one per processor. Set it where several test runs share a machine: each would
otherwise start a worker for every processor, which makes the machine hot and nothing
faster. `EARFUL_WORKERS=0` keeps every search in its own process.

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
earful replay failures/lk-1.8.12.booking.A3-rule --expect reproduced
earful report failures/lk-1.8.12.booking.A3-rule -o report.html
```

| Command | Does |
| --- | --- |
| `earful init AGENT` | Writes a first test for an agent, named as `module:Class`, into `test_<agent>.py` (`-o FILE` names another). The test runs as it is: the scripted model calls the agent's first tool, and every tool is declared an effect, so no tool body runs. The file marks what to change. An agent that cannot be built without arguments is built with `None` for each. Adds `.earful/` to `.gitignore` |
| `earful check LEDGER...` | Prints the verdicts for recorded ledgers. `--measures` adds what each verdict measured, `--json` prints one report per line |
| `earful run SCENARIO` | Runs a scenario once and prints its verdicts. `--case FILE` runs a saved case, `--ledger FILE` writes the ledger |
| `earful search SCENARIO` | Searches a scenario, in one worker process per processor unless `--workers` says otherwise. Where worker processes cannot be started, as in some sandboxes and containers, it says so and searches in its own process; with `--workers N` given it refuses. `--keep CONTRACT/RULE --out DIR` writes a bundle for a rule that broke. `--variables`, `--faults`, `--pairs`, `--profiles`, `--no-words` and `--no-windows` narrow or widen the search |
| `earful replay BUNDLE_DIR` | Runs a bundle's reduced case again. `--expect reproduced` or `--expect clean` makes the outcome the exit status |
| `earful crosscheck SCENARIO` or `BUNDLE_DIR` | Runs a case once on the virtual clock and `--runs` times on the real clock, and says whether the real-clock runs agree with the virtual one (section 10). `--tolerance-ms` and `--words` set how far they may differ, `--out DIR` writes every run's ledger |
| `earful report LEDGER` or `BUNDLE_DIR` | With `-o FILE.html`, draws the run as one HTML page: a timeline, the verdicts and every event (`docs/report.md`). Without it, writes the same out as text. `--format` names one or the other |

A scenario is named `module:function` on the command line, so it has to be importable
from where the command runs. `check` and `report` read files and work without the
framework installed. `--allow CONTRACT/RULE` on `check`, `run` and `search` lets a named
rule break.

| Exit status | Meaning |
| --- | --- |
| 0 | Nothing broke, or the replay did what `--expect` names. For `report`: the report was written |
| 1 | A contract rule broke, the replay did not do what `--expect` names, or a real-clock run did not agree with the virtual one |
| 2 | The command could not be carried out: bad arguments, a file that is not what it should be, a scenario that does not exist |
| 3 | No verdict: a run was not valid evidence, did not repeat, or could not be compared |

A rule that broke outranks a run without a verdict.

## 10. On the real clock

Every run above is on a virtual clock: time moves only when the harness moves it, which
is what makes a run repeat. A production system has no such clock, so a finding is worth
one more check before it is reported. `cross_check` runs the same case on the real clock,
with nothing patched, and compares each of those runs with the virtual one.

```python
from earful.livekit import cross_check

checked = cross_check(book_then_interrupt, runs=3)
print("\n".join(checked.lines()))
assert checked.agrees
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
