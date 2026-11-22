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
- L1 does not put a tool's own running time on a turn's budget. A turn whose tool takes
  long by design needs a larger `slack_us`.
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
| pipecat-ai | 1.16.0 |
| Python | 3.10 to 3.14 with livekit-agents, 3.11 to 3.13 with pipecat-ai |
| Systems | Linux and macOS. Windows is not tested |

A release inside an extra's range that is not in the table is accepted with a warning.
A release outside the range is refused. One pipecat-ai release is supported at a time
([`compatibility.md`](compatibility.md)).

A search of your own agent on a release that has one of the two known defects can meet
it. [`compatibility.md`](compatibility.md#livekit-agents) says which releases have which.

## The report

The report draws one run. Its framework lane explains the run and decides no verdict.
The page is tested in Chromium only
([`report.md`](report.md#what-the-report-does-not-claim)).
