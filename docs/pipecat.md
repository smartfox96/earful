# Writing a Pipecat scenario

Install from this checkout with Python 3.11 or later and pytest available.

```bash
pip install -e ".[pipecat]"
```

## What runs, and what does not

Earful runs a Pipecat pipeline on a virtual clock. Five of its processors are Earful's:
the input, which scripts the VAD, the speech-to-text, the model, the voice and the
output. The context aggregators, the turn strategies, function dispatch and every
processor of yours between them are Pipecat's and yours, and run as they are.

What a scenario cannot do today:

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
  belongs to no reply, and M1 and L1 are unobservable for it.
- Run a realtime speech-to-speech model, or summarize the context with a model call.
- Use a thread. A run in which a thread starts is invalid, and says so.
- Queue frames from outside the pipeline. The pipeline worker is Earful's: your
  transport event handlers, observers and RTVI do not run, so a bot that speaks first
  from `on_client_connected` does not speak first here.

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
`user_mute_strategies`. Pipecat answers a turn without words that cut the bot off;
Earful's seven turn that off, and a pipeline of yours has it unless you pass
`empty_user_turn=None`.

A setting changes what the contracts find. With a mute strategy or with
`VADUserTurnStartStrategy(enable_interruptions=False)` the bot talks on while the user
speaks, and L2 reports it. Declare `contracts={"L2": {"interruptions_allowed": False}}`
on the case if that is what the application intends.

## Tools and effects

Earful records every call of a function the bot registers, however it registers it:
`llm.register_function(name, handler)`, a direct function listed in the context's tools,
or a `FunctionSchema` that carries its handler. A handler delivers its result with
`await params.result_callback(value)`. To stand in for a service, pass
`effects={"create_reservation": Effect("book-table")}` on the case. Earful makes the
declared write in place of that function and gives its receipt to the callback. A handler
can also call `earful.effect` itself.

## Logs, settings and hangups

A search is quiet: Pipecat's logging is off while it runs. A single run logs as Pipecat
does, and `LOGURU_LEVEL=WARNING` in the environment turns that down. An error Pipecat
reports about itself, such as an audio context that got no audio in time, is recorded as
an `fw.error` event and counted in `run.end`. The run is still judged.

All case times are in seconds. `vad_start_secs` and `vad_stop_secs` set the scripted
VAD timing. `user_speech_timeout` and `user_turn_stop_timeout` configure Pipecat's
turn-stop strategy and aggregator. `tts_ttfb` is the voice's time to first audio, and a
reply waits it once: a reply is one utterance, also where Pipecat ends an audio context
between two of its sentences because the model was slow. Set `tts_word_timestamps=True`
to send word times alongside the stamped audio. A hangup cancels the pipeline. For a
real-clock control run, use `cross_check` with the same scenario.
