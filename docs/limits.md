# Limits

What Earful does not do and what it cannot see, in one place. Each entry names the page
that explains it.

## What is tested

A run is one conversation against a scripted user, a scripted model, a scripted voice and
a fake backend. A test shows a failure only in behaviour its script models: the
quickstart finds a double booking because its scripted model tries a failed call again.

Earful does not grade answers, prompts, recognition accuracy or voice quality, and it
does not record production calls ([`contracts.md`](contracts.md#1-what-a-contract-is)).

A failed contract is evidence about one run against what the test declared. Whether it is
a defect of the framework, of the application or of the test is decided by a person
([`contracts.md`](contracts.md#7-attributing-a-failure)).

## What is simulated

- **Providers.** The STT, the model, the voice and the VAD are Earful's scripted ones. No
  run says how a real provider behaves
  ([`compatibility.md`](compatibility.md#what-is-outside)).
- **Playout.** The output is a sink that consumes audio frames on the virtual clock and
  counts samples. It says nothing about a network, a client buffer or what a caller
  heard. With the standard sink the output stops the instant it is told to, so L2
  measures when the framework gives the command
  ([`contracts.md`](contracts.md#3-played-audio)).
- **The voice.** Each word's audio is as long as its letters times `seconds_per_letter`.
  A real voice drifts from the framework's estimate of what was spoken by other amounts,
  which is why M1's tolerance is declared and its measured drift is on every verdict
  ([`contracts.md`](contracts.md#m1-played-vs-remembered)).
- **Turns.** A turn ends on the scripted VAD's silence. A turn detector that is a model,
  and interruption detection that is a model, are outside.
  LiveKit results identify this substitute. In the selected agent's module, Earful traces
  typed and untyped `AgentSession` constructors, local variables, and module functions
  or methods that return a model or session. It checks positional and keyword starts,
  including guarded starts, starts in loops, assigned results and calls wrapped in
  `asyncio.create_task`.
  If every branch selects a realtime model, the refusal names each possible model.
  If a branch allows a cascade, the case can run with scripted turns.
  Setup in another module or selected dynamically can escape this check. The author
  must verify that the application uses separate recognition, model and voice services.
- **The backend.** A write can fail, time out for its caller or be lost. A provider that
  stalls and a caller that sends one write twice cannot be declared
  ([`search.md`](search.md#7-limits)).

## What cannot be run

- Realtime speech-to-speech models, and any pipeline other than STT, LLM and TTS.
- A room or a network transport.
- A hangup in the middle of a speech. The user can leave once their last speech is over.
- Two speeches of the user that overlap. A case or a ledger that declares them is refused.
- On LiveKit: an `AgentSession` or `Agent` option outside the table in
  [`compatibility.md`](compatibility.md#the-options-earful-models-on-livekit), such as an
  agent with its own STT, VAD, model or voice, or a turn detector other than the
  framework's default on the VAD. A run refuses each in one line before it starts, and a
  run whose agent hands over to an agent that breaks the table is invalid.
- On Pipecat: a real service or transport, a `vad_analyzer`, a `VADProcessor`, a turn
  model, turn strategies other than the three the page below names, context
  summarization, and frames queued from outside the pipeline. The pipeline worker is
  Earful's, so transport event handlers, observers and RTVI do not run
  ([`pipecat.md`](pipecat.md#what-runs-and-what-does-not)). A function the bot registers
  after `bot()` returns, and audio a processor plays of its own, make the run invalid. A
  pipecat-ai release that keeps the private names the scripted model reads
  (`LLMService._functions` and two others) elsewhere is refused before the run.
- Audio repeated on its way to the output. A word that reaches the output with more audio
  than it was synthesized with makes the run invalid.
- `earful init` writes a test for a livekit-agents agent class or a Pipecat bot function,
  and reads the functions a Pipecat bot registers by calling it with `None` for each
  argument after the model service. A bot that cannot be called so is refused.

## The virtual clock

A run that leaves the clock is invalid: a thread, `asyncio.to_thread`, a socket or a pipe
the loop waits on, a child process. So an agent whose tools reach a real service cannot
be run as it is. The tool is declared an effect, or the agent is handed a stand-in.

Some things are not seen, and the run stays valid:

- A call that blocks the loop, such as `time.sleep` or a synchronous HTTP client. It
  takes no virtual time.
- `datetime.now()`, and a clock function taken from the `time` module before the run.
  They return real time, so code that measures a wait with one sees no time pass.
- A thread that was running before the run began, until it wakes the loop.

On Python 3.11 a Pipecat run uses `asyncio.wait_for` as Python 3.12 has it. A Pipecat bot
deployed on 3.11 uses the one from `wait_for2`, which pipecat-ai installs there, and tasks
due at one instant can run in another order than in the run
([`compatibility.md`](compatibility.md#what-ci-runs-for-pipecat)).

[`compatibility.md`](compatibility.md#the-virtual-clock) lists every read that is virtual
and every read that is not.

A run that deadlocks with nothing scheduled is ended after 60 real seconds. A run whose
code blocks the thread is not ended.

## Tools and side effects

- A1, A2 and A3 see a write only if it reaches Earful's backend, through `effect` or a
  declared `Effect`. A tool that writes some other way leaves the three unobservable for
  its intent ([`contracts.md`](contracts.md#26-a-tool-that-leaves-nothing-in-the-backend)).
- A tool declared an effect does not run its body, so nothing the body does is tested:
  its argument handling, its errors, the idempotency key it would send.
- The scripted model's tool calls are checked by argument name. Argument values are not
  checked ([`testing.md`](testing.md#2-the-model)).
- On LiveKit a scripted reply keeps to what the request allows, such as no tool call under
  `tool_choice="none"`, and a run whose reply breaks it is invalid
  ([`compatibility.md`](compatibility.md#the-options-earful-models-on-livekit)). A rule
  cannot see the request's `tool_choice`: give the context of such a request a rule that
  answers without a call.
- On LiveKit a tool whose calls cannot be recorded, such as one inside a toolset, makes
  the run invalid.
- On Pipecat, speech whose words a processor changed on the way to the voice belongs to
  no reply, and M1 and L1 are unobservable for it.

## What each contract cannot decide

A verdict that was not judged carries a code ([`contracts.md`](contracts.md#5-verdicts)).
With nothing to judge, such as a deadline after the run ended, it is not exercised, and it
fails an assertion only if the test names the contract in `require`. With evidence lost
it is unobservable, and it fails an assertion and a search unless `allow` names its
`CONTRACT/CODE`.

| Contract | Evidence lost when |
| --- | --- |
| [A1](contracts.md#a1-retired-intent) | The intent's tool was called after the retirement and nothing reached the backend for the intent |
| [A2](contracts.md#a2-one-logical-effect) | A tool for the intent was called and nothing reached the backend for the intent |
| [A3](contracts.md#a3-truthful-confirmation) | The reply's audio cannot be attributed to the scripted reply, the start of the claim's first word cannot be derived, or nothing reached the backend for the intent |
| [M1](contracts.md#m1-played-vs-remembered) | The audio cannot be attributed to anything the agent was scripted to say, the final history was read before the playout finished, or no context lines up with what was said |
| [M2](contracts.md#m2-turn-integrity) | For a tool call: the tool was not witnessed or ran without a call id. For its value: the result has no stable text form |
| [L1](contracts.md#l1-progress) | A tool the model called was not witnessed, or a reply's audio cannot be attributed |
| [L2](contracts.md#l2-interruption) | Never. A speech with no reply under way or waiting to play has nothing to judge |

What the contracts leave alone:

- A2 does not judge too few effects.
- M2 does not judge whether a reply is in the context at all, or the order of tool calls
  among themselves.
- L1 uses the time budget the scenario declares. A tool that waits by design can receive
  an allowance in `L1.tool_wait_us`. The allowance applies only when that tool runs.
- Across a handoff between agents, M2 does not judge what the agent handed over to is
  told of the handoff itself: the application builds that agent's context. The rest of
  the conversation is owed there as anywhere
  ([`contracts.md`](contracts.md#m2-turn-integrity)).
- Two events at the same instant have no order the scenario decides. A stop that reaches
  the sink as the audio ends, and a tool that returns as a call for a later turn is made,
  are held to neither order ([`contracts.md`](contracts.md#23-order-and-time)).
- A ledger with more than 512 readings is not read every way, and what depends on its
  ambiguous texts is unobservable
  ([`contracts.md`](contracts.md#25-when-the-ledger-allows-more-than-one-reading)).
- L2 computes the interruption point from the declared threshold. If a release counts
  speech another way, the point is wrong. The `reference_in_doubt` flag and the decision
  lag on every verdict are there to show it.

## The search

[`search.md`](search.md#7-limits) has the full list. In short:

- A candidate changes one thing: one time moved, or one fault added. `pairs` and
  `profiles` add some combinations of two. Two times each moved to boundaries are not
  explored together.
- A time is put 10 ms before a transition, at it and 10 ms after it. A failure that
  holds none of those values has no candidate inside it.
- The boundaries are read from one run. The search chooses where to look and does not
  prove that every ordering was tried.
- No delay is made longer than 10 s.
- The reduced case is 1-minimal. A smaller failing case may exist, and a reduction cannot
  reduce the scenario's code.
- A search that found nothing is read with its coverage list: what was reached, and what
  could not be judged.

## Bundles and replays

- A bundle names its scenario's code and does not contain it. A replay imports and runs
  that code, so replay only bundles you trust.
- The scenario digest covers one file, the module that defines the scenario's function.
  An agent in another module can change without a replay saying so.
- The files of a bundle are checked against its manifest, and nothing checks the manifest.
  A manifest whose rule no contract has, or whose recorded ledger does not break its rule,
  is refused.
- A replay gives the recorded ledger only on the release, the Earful version and the
  scenario code the bundle was made with
  ([`search.md`](search.md#6-the-bundle-earfulbundle1)).

## The real clock

A real-clock run is never evidence on its own. `cross_check` reads it against the virtual
run, within a declared tolerance. It takes as long as the conversation it scripts, and
other work on the machine shows as drift
([`testing.md`](testing.md#10-on-the-real-clock)).

## Releases and platforms

| | Tested |
| --- | --- |
| livekit-agents | 1.5.6, 1.5.7, 1.6.5, 1.6.6, 1.8.12, 1.8.13 |
| pipecat-ai | 1.16.0, 1.17.0 |
| Python | 3.10 to 3.14 with livekit-agents, 3.11 to 3.13 with pipecat-ai |
| Systems | Linux and macOS. Windows is not tested |

A release inside an extra's range that is not in the table is accepted with a warning.
A release outside the range is refused. Supported pipecat-ai series: 1.16, 1.17. See the
release table ([`compatibility.md`](compatibility.md)).

A search of your own agent on a release that has one of the two known defects can meet
it. [`compatibility.md`](compatibility.md#livekit-agents) says which releases have which.

## The report

The report draws one run. Its framework lane explains the run and decides no verdict.
The page is tested in Chromium only
([`report.md`](report.md#what-the-report-does-not-claim)).

A witnessed handoff may switch to a fresh agent context. M1 and M2 report `handoff-context` where ownership of earlier conversation is unknown. L1 still checks later turns.

## Known defects of Earful

Earful reports a wrong result in the situations below. Each one is in the repository as a
small application under `tests/shapes/`, or as a test, that is expected to fail until
Earful is right. A test holds this list to them.

### LiveKit applications

| Application | Earful reports | A correct result |
| --- | --- | --- |
| [An agent that gives itself a tool with `update_tools` when it enters](../tests/shapes/livekit/adds_a_tool_on_enter.py) | The run is invalid: the added tool runs without a witness | Every contract is judged and none fails |
| [An agent whose `llm_node` answers some questions itself, without calling the model](../tests/shapes/livekit/answers_passwords_itself.py) | L1 fails milestone 1 | None fails |
| [A tool that hands its write to a background task, which writes twice](../tests/shapes/livekit/books_in_background_twice.py) | No contract fails or loses evidence | A2 is unobservable |
| [An agent that awaits an `AgentTask` in `on_enter`](../tests/shapes/livekit/consent_task_on_enter.py) | The run does not end | The run ends and none fails |
| [A tool that speaks a fixed line with `session.say`, sharing a word with a reply the user talked over before it played](../tests/shapes/livekit/filler_after_discarded_reply.py) | L1 and M1 are unobservable | L1 and M1 pass |
| [A tool that starts a filler line with `session.say` and returns without waiting for it](../tests/shapes/livekit/filler_from_tool.py) | M1 is unobservable and M2 fails rule c | None fails |
| [A tool that moves the call to another agent with `session.update_agent`](../tests/shapes/livekit/hands_off_with_update_agent.py) | M1 fails rule 2 and M2 rule d | None fails |
| [A tool that awaits `session.aclose()`](../tests/shapes/livekit/hangs_up_with_aclose.py) | From livekit-agents 1.6 the run raises `RecursionError`. On 1.5 M2 fails rule d and L1 milestones 2 and 3 | None fails |
| [A reply in a language written without spaces, interrupted part way](../tests/shapes/livekit/menu_in_chinese_interrupted.py) | M1 is unobservable and M2 fails rule c | M1 and M2 pass |
| [An audio output that reports playback before it starts, so the agent remembers whole a reply the user heard a fragment of](../tests/shapes/livekit/output_reports_playback_early.py) | M1 passes | M1 fails |
| [An agent that changes the session's endpointing delay when it enters](../tests/shapes/livekit/patient_endpointing.py) | L1 fails milestone 3 | None fails |
| [A write declared for a tool of an agent the call is handed to](../tests/shapes/livekit/pays_after_handoff.py) | The case is refused: the tool is not one of the agent the run starts with | A2 is judged and passes |
| [A greeting from recorded audio: `session.say` with the text and its audio frames](../tests/shapes/livekit/recorded_greeting.py) | The run is invalid: the audio reached the sink altered | None fails |
| [A write that times out for the agent, commits eight seconds later, and is tried again with no idempotency key](../tests/shapes/livekit/retries_after_a_timeout.py) | No contract fails | A2 fails |
| [An agent that adds what it looked up to the turn's context in `on_user_turn_completed`](../tests/shapes/livekit/retrieval.py) | M2 fails rule c | L1, M1 and M2 pass |
| [A tool that returns a price that is not a number](../tests/shapes/livekit/returns_not_a_number.py) | L1 fails milestone 2 | A2 passes and none fails |
| [A scripted model with a rule for none of five user turns](../tests/shapes/livekit/runs_out_of_script.py) | The run does not end | The run is invalid within seconds and names `NoRule` |
| [A model that takes four seconds over each call and then says nothing](../tests/shapes/livekit/says_nothing_slowly.py) | L1 has nothing to judge | L1 fails |
| [A tool that asks the session's model a side question with a context of its own](../tests/shapes/livekit/side_question_to_the_model.py) | M2 fails rule a | M2 passes |
| [A front desk that passes its context to billing](../tests/shapes/livekit/hands_off_to_billing.py) | M1 reports `handoff-context` | M1 passes when the receiving agent retains the shared context |
| [A handoff to a fresh billing agent](../tests/shapes/livekit/hands_off_to_a_fresh_agent.py) | M1 and M2 report `handoff-context` | Both pass when the independent context is identified |
| [A tool that awaits an `AgentTask`](../tests/shapes/livekit/sign_up_task_in_tool.py) | The run is invalid: a tool runs without a witness | None fails |
| [A voice activity detector that needs 0.35 s of speech before it reports any](../tests/shapes/livekit/slow_voice_detection.py) | L2 fails rule a | L2 passes |
| [A user who takes a question back before the answer plays, after which the agent rightly says nothing](../tests/shapes/livekit/takes_it_back.py) | L1 fails milestone 3 | L1 passes |
| [An agent the user cannot interrupt, which talks through the user's turn](../tests/shapes/livekit/talks_through_the_user.py) | L2 passes | L2 has nothing to judge |
| [A tool that swallows cancellation in a loop](../tests/shapes/livekit/tool_ignores_cancellation.py) | The run does not end | The run is invalid within seconds and names the tool |
| [A tool that asks for a spoken update with `generate_reply`, then returns](../tests/shapes/livekit/transfer_update_from_tool.py) | M2 fails rule d | None fails |
| [An agent that shows its model only the last two items of the conversation](../tests/shapes/livekit/windowed_context.py) | L1 fails milestone 1, M1 rule 2, M2 rules a and b | L1, M1 and M2 pass |

### Pipecat applications

| Application | Earful reports | A correct result |
| --- | --- | --- |
| [A scripted model that answers every call at once with another function call](../tests/shapes/pipecat/answers_with_no_delay_forever.py) | The run does not end | The run ends within seconds |
| [A function Pipecat cancels at its timeout, after which the model tries once more and apologises](../tests/shapes/pipecat/booking_service_times_out.py) | A3 fails and L1 fails milestone 2 | None fails |
| [A bot that speaks first by appending a user message at start-up and running the model](../tests/shapes/pipecat/greets_with_a_user_message.py) | L1 fails milestone 1, M2 rules a and b | None fails |
| [A function that speaks its own answer and reports no result](../tests/shapes/pipecat/speaks_from_tool.py) | L1 fails milestone 2 | L1 passes |
| [A user who speaks up inside the last 40 ms of a reply, with a voice that says when each word starts](../tests/shapes/pipecat/stops_in_the_last_word.py) | M1 fails rule 5 | M1 passes |
| [A bot that registers a function it does not offer its model, which the scripted model calls](../tests/shapes/pipecat/unadvertised_function.py) | No contract fails | The run is invalid and names the function |

### A scenario changed in a way that should not matter

Each of these changes should leave every verdict as it was. The tests apply them to every
application under `tests/shapes/`. They change a verdict on LiveKit, and none on Pipecat.

| Change | Earful reports | A correct result |
| --- | --- | --- |
| A starting history | With words that come up again in the run, M2 fails rule b, and for some agents rule a or M1 rule 2 | The verdicts of the run without the history |
| A reply voiced in two parts | M1 fails rule 2, 3 or 5 | The verdicts of the reply voiced whole |
| A declared delay longer | An L1 that failed has nothing to judge once the delay outlasts the run | An L1 that failed still fails |
| The same case run again | Two ledgers, when a tool raises an error whose message holds an object's memory address | One ledger |

### What every adapter should do

| Behaviour | LiveKit | Pipecat |
| --- | --- | --- |
| A write in flight at the end settles | The write has no outcome in the ledger | Holds |
| An exception in application code is recorded | A callback can raise without a recorded error. Witnessed tool errors appear in the result and in `tool.raised` | A processor callback can raise without a recorded error. Witnessed function errors appear in the result and in `tool.raised` |
| A closed session ends the run | The run does not end | The run is invalid, where it should be judged |
| A stuck pipeline hits the ceiling | The run is judged, and L1 fails milestone 2 | The run does not end |
| `tool_choice` is honoured | Holds | A call the request did not allow is judged as any other |
| Two runs give one ledger | Holds | Holds |

### Elsewhere

| What happens | What is right |
| --- | --- |
| A replay whose kept fault never applies is clean | The replay names the fault that did not apply and is not clean |
| The scripted VAD reports a sound shorter than its start time | On Pipecat a sound shorter than the VAD's start time is no speech |
| An enumeration is recorded by its type | A tool argument from an enumeration is recorded by its value |
