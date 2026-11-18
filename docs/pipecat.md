# Writing a Pipecat scenario

Install from this checkout with Python 3.11 or later and pytest available.

```bash
pip install -e ".[pipecat]"
```

A Pipecat `Case` takes a `bot(llm)` function. Use it to register your application's
function handlers on the supplied LLM service and return a fresh `LLMContext`. Earful
runs Pipecat's context aggregators, function dispatch, turn strategies and output
transport with scripted input, model responses and synthesized audio.

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

Register callback-style tools with `llm.register_function(name, handler)`. The
handler receives Pipecat's `FunctionCallParams` and delivers its result with
`await params.result_callback(value)`. To stand in for a service, pass
`effects={"create_reservation": Effect("book-table")}` on the case. Earful makes the
declared write in place of that registered function and gives its receipt to the
callback. A handler can also call `earful.effect` itself.

All case times are in seconds. `vad_start_secs` and `vad_stop_secs` set the scripted
VAD timing. `user_speech_timeout` and `user_turn_stop_timeout` configure Pipecat's
turn-stop strategy and aggregator. Set `tts_word_timestamps=True` to send word times
alongside the stamped audio. A hangup cancels the pipeline. For a real-clock control
run, use `cross_check` with the same scenario.
