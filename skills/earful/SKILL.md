---
name: earful
description: Write and debug Earful tests for a voice agent built on LiveKit Agents or Pipecat (Python). Use when asked to test a voice agent's timing, interruptions, tool calls or bookings, to write a test with ScriptedModel, Case or effect(), or to read an Earful failure. Earful is new; do not guess its API, use what is written here.
---

# Earful tests for a voice agent

Earful runs the real framework on a virtual clock against a scripted user, model, voice
and backend, checks seven contracts on what happened, and searches nearby timings and
backend faults for a run that breaks one. No network, no API keys, no audio devices.
On LiveKit it runs the real `AgentSession`; on Pipecat, the bot's pipeline. A Pipecat bot
is written as the LiveKit agent is, with the differences in "A Pipecat bot" below.

Install: `pip install "earful[livekit]"`. Add `.earful/` to `.gitignore`.

## The shape of a test

```python
from earful.livekit import Case, Effect, ScriptedModel, UserSpeech
from earful.livekit import tool_failed, tool_succeeded, user_ever_said

from myapp.agent import SupportAgent  # the agent as the application ships it

model = (
    ScriptedModel()
    .when(tool_succeeded(), say="Your refund is on its way.", claims=["refund"])
    .when(tool_failed(), say="Sorry, I could not do that.")
    .when(user_ever_said("refund"), say="One moment.", call=("issue_refund", {"order": "A1"}))
    .otherwise(say="How can I help?")
)


def refund() -> Case:  # top level of the module, so a search can use worker processes
    return Case(
        name="refund",
        agent=SupportAgent,  # a class or a zero-argument function; built anew for each run
        effects={"issue_refund": Effect("refund")},
        speeches=[UserSpeech(start=0.5, end=2.5, transcript="I want a refund for order A1.")],
        policy=model,
    )


def test_as_written(earful):
    earful.run(refund).assert_contracts()


def test_every_timing_and_fault_near_it(earful):
    earful.explore(refund).assert_contracts()
```

Run it with `pytest`. `earful` is a pytest fixture the package installs. Without pytest,
`from earful.livekit import run, explore` are the same calls.

`earful init module:Class` writes a file of this shape from the agent's own tools, with
placeholder words and arguments. Start from it when there is no test yet, then replace
the placeholders with a conversation the agent is for. Its search fails on A2 until the
file declares the idempotency key a write is sent with: no tool body runs, so the key has
to be declared.

## Rules for writing one

1. **Read the agent first.** Use its real tool names and argument names in `call=` and
   `effects=`. A scenario that names a tool the agent does not have is refused.
2. **The model is scripted.** Rules are tried in order and the first that holds replies,
   so put the rule for a later state (a tool result) before the rule for an earlier one
   (what the user said). Always end with `.otherwise(...)`: a context no rule matches
   makes the run invalid. Conditions: `user_said`, `user_ever_said`, `tool_returned`,
   `tool_succeeded`, `tool_failed`, combined with `&`, `|`, `~`, or any function of the
   `Context`.
3. **Every durable write must reach Earful's backend**, or contracts A1 to A3 have
   nothing to judge. Pick one way for each tool that writes:
   - The tool calls its service directly: declare `effects={"tool_name": Effect("intent")}`.
     In a run the tool's body is replaced by one write. Do this only for tools that
     write, never for a lookup.
   - The agent is handed its service: pass a stand-in whose method does
     `return await effect("intent", payload)` (`from earful import effect`), and build the
     agent with `agent=lambda: SupportAgent(FakeService())`. The tool's own code runs.
4. **`claims=["intent"]`** goes on a reply that tells the user the write succeeded.
   Leave it off a reply that only says the agent is trying.
5. **Times are seconds** from the first audio frame. Speeches must not overlap. A second
   `UserSpeech` that starts while the agent is talking is an interruption.
6. **Never call a real service, model or network** from a scenario. Tool bodies that do
   must be declared as effects or handed a stand-in.

`Effect("intent", key=lambda args: f"refund-{args['order']}")` sends an idempotency
key; with `Case(backend_mode="keyed")` the backend then honours it.

## A Pipecat bot

Install: `pip install "earful[pipecat]"`, on Python 3.11 or later.

```python
from pipecat.processors.aggregators.llm_context import LLMContext

from earful import effect
from earful.pipecat import Case, Intent, ScriptedModel, UserSpeech, tool_succeeded, user_ever_said

from mybot.bot import register_functions  # how the application sets up its model service


class FakeReservations:  # stands in for the service a function books with
    async def create(self, party_size: int) -> str:
        return await effect("book-table", {"party_size": party_size})


def bot(llm):
    register_functions(llm, FakeReservations())  # llm.register_function(...), as the bot does
    return LLMContext()  # a fresh context for each run


model = (
    ScriptedModel()
    .when(tool_succeeded(), say="Your table is booked.", claims=["book-table"])
    .when(user_ever_said("table"), say="One moment.", call=("create_reservation", {"party_size": 4}))
    .otherwise(say="How can I help?")
)


def booking() -> Case:
    return Case(
        name="booking",
        bot=bot,
        speeches=[UserSpeech(start=0.5, end=2.5, transcript="A table for four, please.")],
        policy=model,
        intents=[Intent("book-table")],
    )


def test_as_written(earful):
    earful.run(booking).assert_contracts()


def test_every_timing_and_fault_near_it(earful):
    earful.explore(booking).assert_contracts()
```

1. **`bot(llm)` registers the bot's functions** on the model service Earful hands it, as
   the application does, and returns a fresh `LLMContext()`. Register every function
   there: one registered later, by a function or a processor, makes the run invalid.
   `effects={"create_reservation": Effect("book-table")}` stands in for a function's body,
   as on LiveKit.
2. **Your own processors** go in `pipeline=`, a function `pipeline(ends)` that returns
   the processors in order: `ends.input`, `ends.stt`, a user context aggregator built on
   `ends.context`, `ends.llm`, yours, `ends.tts`, `ends.output`, and an assistant context
   aggregator. Declare `user_speech_timeout` and `user_turn_stop_timeout` on the case as
   that pipeline uses them.
3. **Refused, in one line, before the run:** a real service or transport; a VAD analyzer,
   a VAD processor or a turn model, which includes Pipecat's default stop strategy; turn
   strategies other than a VAD start (a transcription start may sit beside it) and one
   speech-timeout stop; a thread. Transport event handlers, observers and RTVI do not
   run, so a bot that speaks first from its connect handler does not speak first here.
4. **Two results a search of a Pipecat bot can find after a hangup.** M2 rule d: the user
   hung up while the reply before a function call played, and the context has neither
   the call nor its result. M1 rule 2: without `tts_word_timestamps=True` on the case,
   Pipecat keeps none of a reply that was cut short. If the application keeps the
   context as the record of the call, both are real: write the record from the function
   instead. If it discards the context when the call ends, allow both by name:
   `assert_contracts(allow=["M2/rule d", "M1/rule 2"])`.

Guide: https://github.com/smartfox96/earful/blob/main/docs/pipecat.md

## Reading a failure

`assert_contracts()` raises `ContractFailure` naming the rule, such as `A2/rule`.

| | Fails when |
| --- | --- |
| A1 | The user cancels or corrects something and a new write for it starts anyway |
| A2 | One request leaves more durable effects than it was allowed |
| A3 | The user hears that something succeeded before the backend recorded it |
| M1 | What the agent remembers saying is not what was played |
| M2 | A model call is not shown the conversation that happened |
| L1 | A turn that is owed a reply does not get one in time |
| L2 | The agent is audible while the user holds the floor |

A failing test writes each run's ledger and an HTML report under `.earful/`, and the
failure output says where. Open the report to see the timeline. A search failure also
names what was changed to reach it, such as `backend.timeout 1`: the first backend write
timed out for its caller. The fix belongs in the agent, unless the scenario declared
something untrue. `assert_contracts(allow=["L2/rule b"])` lets one named rule break.

A contract the run never reached is reported as not exercised and does not fail. Name the contracts
a test is about, and it fails unless each got a pass or a fail:
`assert_contracts(require=["A2", "A3"])`. Evidence lost, such as a tool that writes outside
Earful's backend, fails the run as a broken rule does, and
`assert_contracts(allow=["A2/outside-backend"])` lets it.

`earful run module:function` and `earful search module:function` do the same from the
command line. An `InvalidRun` is a fault in the scenario, often a context the model has
no rule for. It says nothing about the agent.

Full guide: https://github.com/smartfox96/earful/blob/main/docs/testing.md
