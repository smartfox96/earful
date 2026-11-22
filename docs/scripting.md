# Scripting the model

A scripted model gives the same reply to the same context, every time. The contracts
judge the framework against what the model asked for, so a test shows a defect only in
behaviour its script models: the quickstart finds a double booking because its scripted
model tries a failed call again. This page shows the habits of real models worth
scripting, and how. [`testing.md`](testing.md#2-the-model) defines `ScriptedModel` and
its conditions.

## Read the context, not the clock

A rule tests the context the framework shows the model: the user's turns, the tool
results, the assistant's earlier turns. It never counts model calls. A search moves the
user's speeches and adds faults, so call 3 of one run is call 2 or call 4 of another,
and a rule keyed on the call's number replies to the wrong question.

```python
from earful.livekit import ScriptedModel, tool_succeeded, user_ever_said

BOOK = ("create_reservation", {"party_size": 4})
model = (
    ScriptedModel()
    .when(tool_succeeded("create_reservation"), say="Your table is booked.", claims=["book-table"])
    .when(user_ever_said("table"), say="One moment.", call=BOOK)
    .otherwise(say="How can I help?")
)
```

Rules are tried in order and the first that holds replies, so the rule for a later state
of the conversation comes first. Here a context with the booking's result is answered
before the request for a table is looked at again, and the model does not book twice
because the user's request is still in its context.

## A reply that only calls a tool

Many models call a tool without saying anything first. Give such a reply `call=` and
no `say=`. The user then hears nothing until the result is back and the model answers,
and L1 measures that wait: the first audio of the turn comes after the tool has run.

A model that is shown a tool's result can call another tool, and the framework asks it
again after each round. livekit-agents stops after `max_tool_steps` rounds and asks with
`tool_choice="none"`. The scripted model keeps to that: a scripted call in such a request
makes the run invalid ([`troubleshooting.md`](troubleshooting.md#the-scripted-model-called-a-tool-where-the-request-allowed-none)).
Give the context in which the tool has already run a reply in words.

## Trying again after a failure

A model that is shown an error usually tries once more, then apologizes. Script both,
and count the results so the second failure ends it:

```python
from earful.livekit import ScriptedModel, tool_failed, tool_succeeded, user_ever_said

BOOK = ("create_reservation", {"party_size": 4})
model = (
    ScriptedModel()
    .when(tool_succeeded(), say="Your table is booked.", claims=["book-table"])
    .when(
        tool_failed() & (lambda context: len(context.tool_results) >= 2),
        say="Sorry, I could not book that. Please call the restaurant.",
    )
    .when(tool_failed(), say="One moment, let me try that again.", call=BOOK)
    .when(user_ever_said("table"), say="Let me book that for you.", call=BOOK)
    .otherwise(say="How can I help?")
)
```

This is the habit that lets a search find a write made twice. A write that commits while
its caller is told it timed out is an error to the model, the model tries again, and the
second call writes again unless it carries an idempotency key the backend honours (A2).
A model scripted never to retry hides that.

`tool_failed()` holds when every result of the tool is an error, as the framework marked
it. `context.tool_results` gives each result with its `name`, its `output` and whether it
`failed`, for a rule that has to read what the error said.

## A user who cancels

"Actually, cancel that." A real model stops: it does not call the tool again. Declare the
cancellation on the speech, with `retires=("book-table",)`, and give the model a rule for
it before the rule that books:

```python
from earful.livekit import ScriptedModel, UserSpeech, tool_succeeded, user_ever_said, user_said

BOOK = ("create_reservation", {"party_size": 4})
speeches = [
    UserSpeech(0.5, 2.5, "A table for four tonight, please."),
    UserSpeech(4.0, 5.5, "Actually, cancel that.", retires=("book-table",)),
]
model = (
    ScriptedModel()
    .when(user_said("cancel"), say="Understood.")
    .when(tool_succeeded(), say="Your table is booked.", claims=["book-table"])
    .when(user_ever_said("table"), say="One moment.", call=BOOK)
    .otherwise(say="How can I help?")
)
```

`user_said` tests the latest user turn, so the cancellation is answered as soon as it is
in the context, and the request for a table, which is still there, books nothing more.
A1 then holds the framework to it: a write for the intent requested after the
cancellation reached the agent fails, however the framework came to make it. A
correction that is meant to start a new write, such as a changed party size, is declared
with `contracts={"A1": {"reissue_allowed": ["book-table"]}}` on the case, and the intent
allowed two effects with `Intent("book-table", authorized=2)`.

## A reply the user cut short

After an interruption the framework shows the model the reply as far as it was heard,
marked interrupted, and M1 checks that against what played. A rule should not need the
whole of an earlier reply to be in the context. Key the reply to the interruption on
what the user said, with `user_said`, which reads only the latest turn.

## The same words twice

When a framework generates a reply again and the script gives the same words, the voice
cannot tell which copy it spoke. The checker then reads the run every way the ledger
allows and reports the reading that fails least
([`contracts.md`](contracts.md#25-when-the-ledger-allows-more-than-one-reading)). Where
which copy was spoken matters to the test, give the two replies different words.

## How long the model takes

A real model takes time to its first token and time to finish, and how long a reply
takes decides what a user can talk over. Give a reply `ttft=` and `duration=` in seconds,
or declare a delay for one call on the case, such as `Delay("llm.ttft", 2, 1.5)`. Use what
your provider's metrics show. A search also stretches every generation it saw, so the
values you give are where it starts.

## What a script cannot stand for

A scripted model does not improvise, misunderstand or hallucinate, and it never hears
audio. A test shows what the framework does with the model you scripted. What a real
model would have done in that context is outside it
([`limits.md`](limits.md#what-is-tested)).
