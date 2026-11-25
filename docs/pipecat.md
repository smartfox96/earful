# Writing a Pipecat scenario

Install with Python 3.11 or later and pytest available.

```bash
pip install "earful[pipecat]"
```

`[pipecat]` installs pipecat-ai alongside, in the release Earful is tested with. From a
checkout,
`pip install -e ".[pipecat]"` does the same.

## What runs, and what does not

Earful runs a Pipecat pipeline on a virtual clock. Five of its processors are Earful's:
the input, which scripts the VAD, the speech-to-text, the model, the voice and the
output. The context aggregators, the turn strategies, function dispatch and every
processor of yours between them are Pipecat's and yours, and run as they are.

What a scenario cannot do:

- Run a real service or transport. A pipeline that holds one is refused, with the
  processor's name.
- Hear audio. The input sends the VAD's start and stop and the speech-to-text sends the
  transcripts. No audio frame flows from the user, so a `vad_analyzer`, a `VADProcessor`
  and a turn model are refused. `TurnAnalyzerUserTurnStopStrategy`, which is Pipecat's
  default stop strategy, is one.
- Use other turn strategies than these. A turn starts on `VADUserTurnStartStrategy`,
  with `TranscriptionUserTurnStartStrategy` beside it if you want it. A turn stops on one
  `SpeechTimeoutUserTurnStopStrategy` that waits for the transcript. The run's header
  states a turn's timing as two delays, and these are the strategies those two delays
  describe. Minimum words, wake phrases, external and eager turns, turn completion by
  the model or a classifier, a strategy class of your own and
  `user_turn_strategies_params` other than the defaults are refused.
- Follow words a processor rewrites. Earful matches what the voice speaks to the model's
  reply word for word. Speech whose words a processor of yours changed on the way
  belongs to no reply, and M1 and L1 lose the evidence for it (`audio-unattributed`),
  which fails the run unless allowed.
- Run a realtime speech-to-speech model, or summarize the context with a model call.
- Use a thread. A run in which a thread starts is invalid, and says so.
- Queue frames from outside the pipeline. The pipeline worker is Earful's: your
  transport event handlers, observers and RTVI do not run, so a bot that speaks first
  from `on_client_connected` does not speak first here. A processor can do it instead
  (A bot that speaks first, below).

Each refusal is a `ValueError` of one line, raised before the run starts.

## A first scenario

A `Case` takes a `bot(llm)` function. Use it to register your application's function
handlers on the supplied LLM service and return a fresh `LLMContext`.

Save this as `test_greeting.py`.

```python
from pipecat.processors.aggregators.llm_context import LLMContext

from earful.pipecat import Case, ScriptedModel, UserSpeech


def greeting():
    return Case(
        name="greeting",
        bot=lambda llm: LLMContext(),
        speeches=[UserSpeech(0.5, 1.0, "Hello")],
        policy=ScriptedModel().otherwise(say="Good morning."),
    )


def test_greeting(earful):
    earful.run(greeting).assert_contracts()
```

```bash
pytest -q test_greeting.py
earful run test_greeting:greeting --ledger greeting.jsonl
earful report greeting.jsonl
```

The CLI and fixture select the adapter from the case. The same scenario works with
`earful search` and `earful crosscheck`. A bundle records the framework for replay.
A case object or local factory can also run in the test process. Worker searches and
saved bundles need an importable factory such as `test_greeting:greeting`.

## A first test of your bot

`earful init` writes a first test from the functions your bot registers. Name the
function that registers them and returns the context, as `module:function`:

```bash
earful init mybot.bot:booking_bot    # writes test_booking_bot.py
```

It calls that function with a model service of its own, and `None` for each argument
after it, to see which functions it registers and which tools its context advertises.
Setup can only list the functions visible at that point. The first test asks for a reply
and requires L1. Pass `--write create_reservation` to model a specific durable write and
its retry. The generated test then requires A2, A3 and L1. A bot must synchronously return
an LLMContext. Give the command an importable dotted module name.

## Your own pipeline

Without `pipeline`, a case runs seven processors: the input, the speech-to-text, a user
context aggregator, the model, the voice, the output and an assistant context
aggregator. To run yours, give the case a `pipeline(ends)` function that returns the
processors in order, or a `Pipeline` of them. `ends` holds Earful's five processors and
the context `bot` returned. The function is called once per run and builds every other
processor afresh.

```python
from pipecat.frames.frames import LLMTextFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.turns.user_mute.function_call_user_mute_strategy import FunctionCallUserMuteStrategy
from pipecat.turns.user_start.transcription_user_turn_start_strategy import (
    TranscriptionUserTurnStartStrategy,
)
from pipecat.turns.user_start.vad_user_turn_start_strategy import VADUserTurnStartStrategy
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from earful.pipecat import Case, Ends, ScriptedModel, UserSpeech


class CountsWords(FrameProcessor):
    """A processor of the application: it passes every frame on."""

    words = 0

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMTextFrame):
            CountsWords.words += frame.text.count(" ")
        await self.push_frame(frame, direction)


def pipeline(ends: Ends):
    pair = LLMContextAggregatorPair(
        ends.context,
        user_params=LLMUserAggregatorParams(
            user_turn_strategies=UserTurnStrategies(
                start=[VADUserTurnStartStrategy(), TranscriptionUserTurnStartStrategy()],
                stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.8)],
            ),
            user_mute_strategies=[FunctionCallUserMuteStrategy()],
        ),
    )
    return [
        ends.input,
        ends.stt,
        pair.user(),
        ends.llm,
        CountsWords(),
        ends.tts,
        ends.output,
        pair.assistant(),
    ]


def greeting():
    return Case(
        name="greeting",
        bot=lambda llm: LLMContext(),
        speeches=[UserSpeech(0.5, 1.0, "Hello")],
        policy=ScriptedModel().otherwise(say="Good morning."),
        pipeline=pipeline,
        user_speech_timeout=0.8,
    )


def test_greeting(earful):
    earful.run(greeting).assert_contracts()
    assert CountsWords.words == 1
```

Earful's five go at the top level of the pipeline in the order input, speech-to-text,
model, voice, output, with one user context aggregator between the speech-to-text and
the model and one assistant context aggregator after the output. Both aggregators are
built on `ends.context`. Your processors go anywhere between them, alone or inside a
`ParallelPipeline`.

The case declares the two timeouts its pipeline uses: `user_speech_timeout` for the
stop strategy and `user_turn_stop_timeout` for the user aggregator. They are part of the
case's data, a search and a bundle carry them, and a pipeline built with another value
is refused. The run's header records the rest under `session.extra_options`: the start
and stop strategies by name, the pipeline's processors by class name in order, and each
setting of the aggregators that is not Earful's own default, such as
`user_mute_strategies`. Pipecat can answer when the user interrupts without saying any words. Earful's
standard pipeline disables that reply by passing `empty_user_turn=None` to the user
aggregator. Set the same option in your own pipeline to reproduce that behaviour.

A setting changes what the contracts find. With a mute strategy or with
`VADUserTurnStartStrategy(enable_interruptions=False)` the bot talks on while the user
speaks, and L2 reports it. Declare `contracts={"L2": {"interruptions_allowed": False}}`
on the case if that is what the application intends.

## A bot that speaks first

A bot that greets the caller usually queues `LLMRunFrame` from its transport's
`on_client_connected` handler, which does not run here. Ask from inside the pipeline
instead: a processor that pushes `LLMRunFrame` as the pipeline starts. Put it before the
user context aggregator, which turns the frame into a model call.

```python
from pipecat.frames.frames import LLMRunFrame, StartFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.turns.user_start.vad_user_turn_start_strategy import VADUserTurnStartStrategy
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from earful.pipecat import Case, Ends, ScriptedModel, UserSpeech, user_said


class SpeaksFirst(FrameProcessor):
    """Asks the model for a reply as the pipeline starts."""

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if isinstance(frame, StartFrame):
            await self.push_frame(LLMRunFrame())


def pipeline(ends: Ends):
    turns = UserTurnStrategies(
        start=[VADUserTurnStartStrategy()],
        stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.6)],
    )
    pair = LLMContextAggregatorPair(
        ends.context, user_params=LLMUserAggregatorParams(user_turn_strategies=turns)
    )
    return [
        ends.input,
        ends.stt,
        SpeaksFirst(),
        pair.user(),
        ends.llm,
        ends.tts,
        ends.output,
        pair.assistant(),
    ]


def greets():
    return Case(
        name="greets",
        bot=lambda llm: LLMContext(),
        speeches=[UserSpeech(4.0, 5.5, "A table for two, please.")],
        policy=ScriptedModel()
        .when(user_said("table"), say="Of course, for two.")
        .otherwise(say="Welcome to the restaurant. How can I help?"),
        pipeline=pipeline,
        user_speech_timeout=0.6,
    )


def test_the_bot_greets_the_caller(earful):
    earful.run(greets).assert_contracts()
```

The greeting is a reply like any other. It is model call 1, made before the user has
said anything, so the scripted model's `otherwise` gives it. M1 holds it to what played,
and a user who speaks over it is an interruption L2 judges.

Three things a bot can do that do not fit a scenario:

- **Pipecat Flows.** A flow manager is built around the pipeline's task, which is
  Earful's, and moves the conversation between nodes outside the model service. Test
  the functions and the prompts of one node as a bot of its own.
- **A handler that holds the worker or the transport**, such as one that queues frames
  on the task or reads the client's state. Neither is the application's in a run, so a
  scenario cannot hand it to such code.
- **A processor that plays its own audio**, such as a chime or hold music pushed as
  audio frames. Earful's output reads every sample as a word the voice spoke, so audio
  that comes from elsewhere is a stamp error and makes the run invalid.

## Tools and effects

Earful records every call of a function the bot registers, however it registers it:
`llm.register_function(name, handler)`, a direct function listed in the context's tools,
or a `FunctionSchema` that carries its handler. A handler delivers its result with
`await params.result_callback(value)`. To stand in for a service, pass
`effects={"create_reservation": Effect("book-table")}` on the case. Earful makes the
declared write in place of that function and gives its receipt to the callback. A handler
can also call `earful.effect` itself. Declare that write on the case with
`intents=[Intent("book-table", tools=("create_reservation",))]`.

## A booking bot, searched

[`examples/pipecat_booking`](https://github.com/smartfox96/earful/tree/main/examples/pipecat_booking)
is the README's booking example as a Pipecat bot. `bot.py` is the application. It
registers one function and is handed the service it books with:

```python
"""A restaurant booking bot for Pipecat, as an application would write it. Nothing here
knows of Earful: the bot is handed the service it books with."""

from typing import Protocol

from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.llm_service import FunctionCallParams, LLMService


class Reservations(Protocol):
    """The reservation service, as the bot uses it."""

    async def create(self, party_size: int, *, idempotency_key: str | None = None) -> str:
        """Book a table and return the confirmation number."""
        ...


def booking_bot(llm: LLMService, reservations: Reservations) -> LLMContext:
    """Registers the booking function on the model service and returns the bot's context."""

    async def create_reservation(params: FunctionCallParams) -> None:
        party_size = params.arguments["party_size"]
        confirmation = await reservations.create(party_size)
        await params.result_callback(f"Booked. Confirmation {confirmation}.")

    llm.register_function("create_reservation", create_reservation)
    booking = FunctionSchema(
        name="create_reservation",
        description="Book a table for tonight.",
        properties={"party_size": {"type": "integer"}},
        required=["party_size"],
    )
    return LLMContext(tools=ToolsSchema(standard_tools=[booking]))
```

`test_booking.py` scripts the model, stands in for the service, and holds two tests: the
conversation as written, and a search of every timing and fault near it.

```python
"""One conversation with the booking bot, run as written and then searched."""

from bot import booking_bot

from earful import effect
from earful.pipecat import (
    Case,
    Intent,
    ScriptedModel,
    UserSpeech,
    tool_failed,
    tool_succeeded,
    user_ever_said,
)


class FakeReservations:
    """Stands in for the reservation service. A booking is one write to Earful's backend,
    which a search makes slow, failing or lossy."""

    async def create(self, party_size: int, *, idempotency_key: str | None = None) -> str:
        return await effect("book-table", {"party_size": party_size}, key=idempotency_key)


# The scripted model. It books when asked, tries once more if the function fails, and
# tells the user the table is booked only after a call that succeeded.
BOOK = ("create_reservation", {"party_size": 4})
model = (
    ScriptedModel()
    .when(tool_succeeded(), say="Your table for four is booked.", claims=["book-table"])
    .when(
        tool_failed() & (lambda context: len(context.tool_results) >= 2),
        say="Sorry, I could not book that. Please call the restaurant.",
    )
    .when(tool_failed(), say="One moment, let me try that again.", call=BOOK)
    .when(user_ever_said("table"), say="Let me book that for you.", call=BOOK)
    .otherwise(say="How can I help?")
)


def booking() -> Case:
    return Case(
        name="booking",
        bot=lambda llm: booking_bot(llm, FakeReservations()),
        speeches=[UserSpeech(start=0.5, end=2.5, transcript="A table for four tonight, please.")],
        policy=model,
        intents=[Intent("book-table")],  # what the user wants done, once
        backend_mode="keyed",  # the service honours an idempotency key, if it is sent one
    )


def test_the_conversation_as_written(earful):
    earful.run(booking).assert_contracts()


def test_every_timing_and_fault_near_it(earful):
    earful.explore(booking).assert_contracts()
```

```bash
cd examples/pipecat_booking
pytest -q
```

The conversation as written passes. On Pipecat 1.17.0, the search reports three broken rules.

```text
ContractFailure: booking on pipecat-ai 1.17.0: 753 cases tried, 753 judged, 3 contract rule(s) broken
Earful ran the case's bot with Earful's standard pipeline, scripted recognition, model replies and voice. Application entry code was not run.
    886 run(s): 6 to find where to put things, 749 for the cases, 129 to measure the failures, 2 to read an example of each
    by contract: pass, fail, nothing to judge, evidence lost
    A1      0     0   753     0
    A2    465   278    10     0
    A3    687     0    66     0
    M1    748     3     2     0
    M2    727    26     0     0
    L1    717     0    36     0
    L2      0     0   753     0

A2/rule  in 278 case(s)
    2 durable effects for 'book-table'; the scenario authorized 1
    changed: backend.timeout 1, commit first
    also by changing: backend.timeout 1, commit first + backend.latency 1; backend.timeout 1, commit first + backend.wait 1; backend.timeout 1, commit later; backend.timeout 1, commit later + backend.wait 1
    breaks with backend.latency 1 from 0.000 s (no case exists beyond it) to 32.958 s (still breaks where the search stopped)
    breaks with backend.wait 1 from 0.000 s (no case exists beyond it) to 29.099 s (a run beyond it could not be judged, invalid: harness error: the bot was still busy 30.0 s after the scenario)

M1/rule 2  in 3 case(s)
    final history: 5 word(s) were played but only 0 are remembered: 1080000 us of played audio is forgotten
    changed: user.hangup 0 at 6.540 s (not declared as written)
    breaks with user.hangup 0 from 6.540 s (passes beyond it) to 6.899 s (passes beyond it)

M2/rule d  in 26 case(s)
    create_reservation (call-1-1) finished before this context, which holds 0 call item(s) and 0 result item(s) for it
    changed: user.hangup 0 at 3.600 s (not declared as written)
    breaks with user.hangup 0 from 3.600 s (passes beyond it) to 4.959 s (passes beyond it)
```

**A2 is the application's.** The service committed a booking and told the bot it had
timed out. The model tried again, and the bot sent no idempotency key, so the service
booked a second table. Send the key in `bot.py`:

```python
        confirmation = await reservations.create(party_size, idempotency_key=f"table-for-{party_size}")
```

A2 is gone, and the two other rules remain. Both need the user to hang up, which the
scenario does not declare: the search tried it.

**M2 rule d: a booking with no trace in the context.** Pipecat adds a function call and
its result to the context once the speech before them has played. The user hangs up
while "Let me book that for you." plays, between 3.6 and 4.96 s. The hangup cancels the
pipeline, as an application does when its client disconnects, and the context it leaves
has neither the call nor its result. The booking was made.

**M1 rule 2: a reply that was heard and is not remembered.** The scripted voice sends no
word times unless the case sets `tts_word_timestamps=True`. Without them Pipecat cannot
tell how much of a reply that was cut short had played, and keeps none of it. The user
hangs up five words into "Your table for four is booked.", and the context does not say
the bot told them so.

Whether either matters depends on what your application does with the context after a
call ends. If it stores the context as the record of the call, both are real: the record
leaves out a booking, or a confirmation the user heard. Write the record from the
function handler in that case. If the context is discarded at a hangup, say so in the
test, by rule:

```python
    earful.explore(booking).assert_contracts(allow=["M2/rule d", "M1/rule 2"])
```

With the key sent and the two rules allowed, both tests pass. A rule that is allowed
still appears in the report, and any other rule still fails the test.

Two more results of this kind can appear in a search of your own bot. With
`tts_word_timestamps=True`, Pipecat marks a reply complete once its last word starts, so
a cut inside that word leaves it marked complete (M1 rule 5). A function call that is
still running when the user speaks is cancelled, which is Pipecat's default
(`cancel_on_interruption`). A write it had started still commits, the model is shown no
result, and a model that books again makes a second booking unless the write carries an
idempotency key (A2).

## Logs, settings and hangups

A search is quiet: Pipecat's logging is off while it runs. A single run logs as Pipecat
does, and `LOGURU_LEVEL=WARNING` in the environment turns that down. An error Pipecat
reports about itself, such as an audio context that got no audio in time, is recorded as
an `fw.error` event and counted in `run.end`. The run is still judged. A processor of
yours that raises makes the run invalid, with its error: the pipeline broke, so nothing
after it can be judged.

All case times are in seconds. `vad_start_secs` and `vad_stop_secs` set the scripted
VAD timing. `user_speech_timeout` and `user_turn_stop_timeout` configure Pipecat's
turn-stop strategy and aggregator. `tts_ttfb` is the voice's time to first audio, and a
reply waits it once: a reply is one utterance, also where Pipecat ends an audio context
between two of its sentences because the model was slow. Set `tts_word_timestamps=True`
to send word times alongside the stamped audio. A hangup cancels the pipeline. For a
real-clock control run, use `cross_check` with the same scenario.
