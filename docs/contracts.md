# Contracts

Status: **agreed**, 16 November 2026. Nothing here is implemented yet. The queries in
`src/earful/core/observations.py` are the seed, not the checker.

This document defines what Earful checks. The checker, the ledger schema and the deliberate
breaks that prove the checker works are all built against it. Defaults marked
*provisional* are confirmed or changed when the checker first runs over the recorded
ledgers.

## 1. What a contract is

A contract is a statement about one run that can be decided from the run's ledger alone.
The test author declares which contracts apply and with which policy. A violation means
the run contradicted the declared contract. It does not by itself mean the framework has
a defect: section 7 covers attribution.

Earful does not judge answer quality, speech recognition accuracy, voice quality or
whether a conversation achieved its goal.

## 2. Evidence

### 2.1 Witnesses

Every ledger event names the witness that observed it.

| Witness | What it knows | How it knows |
| --- | --- | --- |
| `world` | What the scripted user said and when; what text was synthesized; when each tool was called and what it returned | Earful's own scripted providers and tool witness did these things |
| `sink` | Which audio samples were consumed and when; every pause, resume and clear it was told to perform | It is the audio output, and reads word stamps out of the samples |
| `backend` | Every write requested, committed, deduplicated or failed | It is the store the tools write to |
| `llm` | The exact context shown to the model on each call, and what the model answered | It is the scripted model |
| `framework` | The framework's public events: state changes, items added, speech handles | The framework reported them |
| `harness` | Run boundaries and the final chat history read back from the session | Earful's runner |

### 2.2 Which evidence may decide a verdict

Each contract compares a **judged record** with a **reference record**.

- The reference record is always `world`, `sink` or `backend`: something Earful did or
  measured itself.
- The judged record is the system's behaviour as seen from outside it: the context
  presented to the model (`llm`), the audio that reached the output (`sink`), or the writes
  that reached the store (`backend`).
- `framework` events never decide a verdict. They are attached to a verdict as
  explanation, and they may supply correlation identifiers.
- The final chat history (`harness`) is framework state read back by Earful. It is used
  as the judged record only when no later model call exists, and a verdict that rests on
  it says so.
- The checker does not use the framework's own estimate of what was played.

### 2.3 Order and time

- `seq` is the total order of a run. "Before" means lower `seq`.
- `t_us` is virtual time in microseconds. A deadline is met if the event's `t_us` is at or
  before the deadline.
- Where a rule compares an event with a time that is derived rather than recorded (for
  example the instant a word finished playing), equal times satisfy "at or before".

### 2.4 Run validity

A run produces verdicts only if all of these hold. Otherwise it is **invalid** and yields
no verdict for any contract.

1. The ledger validates against its schema and ends with `run.end`.
2. The run was on the virtual clock, and no work left it for a thread.
3. No audio sample reached the sink altered.
4. Playout never ran ahead of the audio it had been given.
5. The harness recorded no error of its own.

All five are decided from the ledger alone, by one function
(`earful.core.schema.validity`); `docs/ledger.md` lists what each reads.

## 3. Played audio

The scripted TTS gives every synthesized word a stamp and writes that stamp into each
sample of the word. The sink reads the stamps back from the samples it consumes.

- A word is **played** when its last sample has been consumed.
- A word is **cut** when at least one but not all of its samples were consumed. A playout
  segment has at most one cut word: the one the playout position stopped inside.
- Every other synthesized word is **unplayed**.
- A word's **completion time** is the virtual time at which its last sample was consumed.
- The sink is **audible** from the first consumed sample of a segment until the segment
  finishes, excluding any time it is paused.

This is modeled playout at the output sink. It says nothing about a network, a client
buffer or a caller's device, and every report states that boundary.

## 4. What the test author declares

The checker reads these from the ledger header; it never imports the scenario.

| Declaration | Meaning | Used by |
| --- | --- | --- |
| User speech: start, end, transcript | A scripted utterance. An empty transcript is noise | all |
| `expects_reply` on a speech | The agent owes this turn a response. Default: true when the transcript is non-empty and the speech is not a backchannel | L1 |
| `backchannel` on a speech | The user is acknowledging, not taking the floor | L1, L2, M2 |
| `retires` on a speech | This turn corrects or cancels the named intent | A1 |
| Intent | A logical action the user wants done once, with its authorized effect count (default 1) and the tools that carry it out | A1, A2, A3 |
| Claim on a scripted reply | The reply, or a span of its words, tells the user an intent succeeded | A3 |
| Policy values | The tolerances, bounds and budgets named in section 6 | all |

## 5. Verdicts

A contract is checked once per **instance**: each intent, each reply, each user turn, as
section 6 defines. An instance gets one of three verdicts.

- **pass**: the evidence required is present and the rule holds.
- **fail**: the evidence required is present and the rule does not hold.
- **unobservable**: evidence the rule needs is absent, so neither can be shown.

A contract's result for a run is `fail` if any instance failed, otherwise `unobservable`
if any instance was unobservable, otherwise `pass`.

A contract with no instances in a run is reported as **not exercised**, never as a pass.
A test may require that a contract is exercised, so that a scenario which never reaches
the behaviour cannot pass silently.

Every verdict carries: the contract and policy values used, the instance, the `seq` of
each event it rests on, the witnesses those events came from, and the measured numbers.

## 6. The seven contracts

Each contract lists a deliberate break and a clean control. The break is reached through
seams any application has: the model policy, the tool, the backend, a public agent hook,
a session option or the sink. Nothing inside the framework is patched. Each contract's
tests also include hand-edited ledgers, which test the checker without a run.

### A1. Retired intent

After the user corrects or cancels an intent, no new side-effecting call for it starts.

| | |
| --- | --- |
| Instance | Each (speech, intent) where the speech `retires` the intent and its final transcript was delivered |
| Retirement point | The `world` event delivering that final transcript. This is the earliest instant any system could know |
| Judged record | `backend`: write requests for the intent |
| Rule | Fail if a write request for the intent comes after the retirement point |
| In-flight calls | A write requested before the retirement point may still commit after it. Reported as a late commit, not a failure |
| Policy | `in_flight`: `may_finish` (default) or `must_not_commit`. `reissue_allowed`: intents the correction is permitted to start |
| Unobservable | The intent's tool does not write through the backend |
| Deliberate break | A model policy that calls the tool again after the correction is in its context |
| Clean control | The same scenario with a policy that honours the correction |

### A2. One logical effect

Durable effects for an intent do not exceed the count the scenario authorized.

| | |
| --- | --- |
| Instance | Each declared intent for which the backend received at least one write request |
| Judged record | `backend`: committed effects |
| Rule | Fail if committed effects for the intent exceed its authorized count |
| Policy | Authorized count (default 1). Backend mode: `plain` (every write commits) or `keyed` (a repeated idempotency key is deduplicated and does not count) |
| Unobservable | A tool for the intent was called but nothing reached the backend |
| Not judged | Too few effects. That is A3 when the agent claimed success, and otherwise a scenario expectation |
| Deliberate break | A tool that writes twice for one call, on a `plain` backend |
| Clean control | The same tool on a `keyed` backend |

### A3. Truthful confirmation

The user does not hear that an action succeeded before the backend has recorded it.

| | |
| --- | --- |
| Instance | Each claim whose last word was played. A claim whose last word was cut or unplayed was not made, and is reported as not delivered |
| Reference record | `backend`: committed effects for the claimed intent |
| Judged record | `sink`: completion time of the claim's last word |
| Rule | Pass if an effect for the intent was committed at or before that completion time. Fail otherwise |
| Policy | `claim_span`: which words of the reply make the claim (default: all). `claim_point`: `span_end` (default) or `span_start` |
| Unobservable | The reply's audio cannot be attributed to the scripted reply, or the segment never finished |
| Deliberate break | A model policy that says the booking is done in the same reply that calls the tool, with a backend that takes longer to commit than the sentence takes to play |
| Clean control | The same backend, with the claim made only after the tool result is in context |

### M1. Played vs. remembered

What the agent remembers having said matches what was played.

For a scripted reply with words `W`, let `P` be the number of its words played, `C` be 1
if one of its words was cut and 0 otherwise, and `R` be the words of the assistant message
attributed to it in the judged record.

| | |
| --- | --- |
| Instance | Each scripted reply with text, once its playout has finished or it has been abandoned |
| Reference record | `sink`: `P` and `C` |
| Judged record | Every model context requested after the playout finished; the final chat history if there is none |
| Rule 1 | `R` is a prefix of `W` |
| Rule 2 | `len(R) >= P - missing_words` |
| Rule 3 | `len(R) <= P + C + extra_words`. The cut word may be remembered or not |
| Rule 4 | If no sample of the reply was consumed, the reply is absent from memory. No tolerance applies |
| Rule 5 | The message is marked interrupted exactly when `P < len(W)` |
| Policy | `missing_words` (default 0). `extra_words` (default 1). `check_interrupted_flag` (default true) |
| Always reported | `len(R) - P` and `len(R) - (P + C)`, on every instance, pass or fail. A release that drifts inside the tolerance stays visible |
| Unobservable | The audio cannot be attributed to a scripted reply; or the run ended before the playout finished |
| Deliberate break | A sink that reports a playback position 1.5 seconds later than the true one to the framework, while recording the true one |
| Clean control | The unmodified sink |

Why `extra_words` defaults to 1. The framework estimates how much text was spoken; the
sink counts samples. `scripts/contracts_m1_sweep.py` interrupts one reply at every 50 ms
and compares the two:

| livekit-agents | Seconds per word | Interruption points | Remembered fewer than played | Nothing played, nothing remembered | Cut word dropped | Exactly played + cut | One word beyond the cut word | More |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1.5.6 | 0.30 | 171 | 0 | 0 | 22 | 111 | 38 | 0 |
| 1.6.5 | 0.30 | 171 | 0 | 6 | 22 | 106 | 37 | 0 |
| 1.8.12 | 0.30 | 171 | 0 | 6 | 22 | 106 | 37 | 0 |
| 1.8.12 | 0.45 | 258 | 0 | 6 | 16 | 141 | 95 | 0 |

The framework never remembered fewer words than were played and never more than one word
past the cut word. With `extra_words = 0`, between a fifth and a third of ordinary
interruptions would fail on every release, which would bury real failures. With
`extra_words = 1` a reply remembered in full after two seconds of playout still fails. A
scenario that needs exact agreement sets `extra_words = 0`.

### M2. Turn integrity

Each model call is shown the conversation that happened: every user turn, every reply
and every completed tool call, once each, in order.

| | |
| --- | --- |
| Instance | Each model context, and the final chat history |
| Reference record | `world`: final transcripts delivered, tool calls returned. `llm`: replies the model gave |
| Judged record | `llm`: the items in the context |
| Rule a, user turns | The user text in the context, read in order, is a prefix of the delivered transcripts read in order. Nothing invented, duplicated or reordered |
| Rule b, no loss | A delivered transcript is present in every context requested more than `user_commit_lag` after its delivery, and never disappears once it has appeared |
| Rule c, replies | The assistant messages, read in order, match scripted replies one to one in the order the model gave them, each message a prefix of its reply. No reply is matched twice. Order decides the match, so two replies with the same text stay distinct |
| Rule d, tool calls | For each tool call that returned before the context was requested, the context holds exactly one call item and exactly one result item, the result after the call and carrying the returned value: its text equals the `text` the tool witness recorded. A result with no witnessed return fails |
| Rule e, causal order | Each reply comes after the user turns that were in the context it was generated from |
| Discard policies | `noise`: speech with no transcript leaves no turn. `backchannel`: its transcript may be present or absent. `cancelled_tool`: a call cancelled before it returned needs no result |
| Policy | `user_commit_lag` (default 1.0 s, *provisional*; in every recorded ledger the lag is zero) |
| Unobservable | Rule d for a tool that was not witnessed. The value comparison in rule d, for a result with no stable text form (`text` is null); presence and order are still checked |
| Not judged | How much of an interrupted reply is kept. That is M1 |
| Deliberate break | A public agent hook that removes tool results from the context before the model call; a second variant that repeats a user turn |
| Clean control | The unmodified agent |

### L1. Progress

A turn that is owed a response gets one within its budget, and nothing blocks the turns
after it.

| | |
| --- | --- |
| Instance | Each delivered speech with `expects_reply`, unless the user speaks again first. Then the later speech carries the obligation |
| Start of the clock | The later of the speech's end and the delivery of its final transcript, both `world` |
| Milestone 1 | A model call whose context contains the turn |
| Milestone 2 | If the model answered with tool calls: every call is made, and after the last one returns a further model call follows. Repeats up to `max_steps` |
| Milestone 3 | The first word of a reply in that chain starts playing |
| Rule | Fail if a milestone has not occurred by its deadline. The deadline is the start of the clock, plus the declared end-of-turn delay, plus the scripted provider delays on the chain so far, plus `slack` |
| Excused | The user began speaking again; the scenario closed the session; the scenario declared a wait |
| Policy | `slack` (default 1.0 s, *provisional*). `max_steps` (default 4) |
| Unobservable | The run ended before the deadline passed |
| Reported separately | Time to the first model call and time to first audio |
| Deliberate break | A tool that never returns and has no timeout; a second variant where the model returns nothing at all |
| Clean control | The same turn declared `expects_reply = false`; the same tool returning |

### L2. Interruption

While the user holds the floor the agent is silent, within a declared bound.

The **floor window** of a speech runs from the instant the scripted VAD reported speech
to the framework until the true end of the speech. The **interruption point** is the
speech's true start plus the declared minimum interruption duration; a speech shorter than
that has none.

| | |
| --- | --- |
| Instance | Each speech that is neither noise nor a backchannel |
| Reference record | `world`: the floor window and interruption point |
| Judged record | `sink`: when it was audible, and when segments started or resumed |
| Rule a, stop | If the sink is audible at the interruption point, it stops being audible by that point plus `stop_bound`, and stays silent until the floor window closes |
| Rule b, no start | No segment starts or resumes inside the floor window, later than `start_grace` after it opens |
| Policy | `stop_bound` (default 100 ms, *provisional*). `start_grace` (default 0). `interruptions_allowed` (default true; if false, rule a does not apply) |
| Reference in doubt | A rule a failure in which a pause or clear command did reach the sink, but later than the interruption point plus `stop_bound`, is marked `reference_in_doubt`. It stays a failure. It is not published or attributed until someone confirms that the computed interruption point matches the framework's documented threshold on that release. A failure with no command at all, or with a timely command the sink did not obey, is not marked |
| Unobservable | The ledger has no true speech boundaries |
| Reported separately | Recognition delay: from the true start of speech to the first pause or clear command the sink received. Decision lag: from the interruption point to that command; it can be negative. Stop latency: from that command to silence |
| Deliberate break | A sink that keeps consuming for 400 ms after it is told to pause or clear; a second variant with interruptions disabled on the agent while the policy still expects them |
| Clean control | The unmodified sink; interruptions disabled with `interruptions_allowed = false` |

The interruption point is computed from the declared threshold, not from the moment the
framework says it decided. That keeps the reference record independent. The cost is that
Earful must model the framework's threshold correctly; if a release changes how it counts
speech, the computed point is wrong and failures would be false. The `reference_in_doubt`
mark exists for that case, and the decision lag on every instance shows the drift before
it causes a failure.

Stop latency is zero in every run with the standard sink, because the sink stops the
instant it is told to. Rule a therefore tests whether and when the framework issues the
command, not how fast an output device obeys it.

## 7. Attributing a failure

The checker reports violations. It does not assign blame. Before a failure is published
or reported upstream, a person classifies it as one of:

- **framework defect**: the framework broke a guarantee it documents;
- **hazardous documented default**: the framework behaved as documented and the default is
  unsafe for this scenario;
- **application or backend error**: the agent, model policy, tool or store is at fault;
- **harness artifact**: Earful's simulation caused it. Fixed before anything is published.

The classification is recorded with the case, with the version it was observed on and the
documentation it was compared against.

## 8. What the ledger must record

These are requirements on the ledger. `docs/ledger.md` defines each event named here.

| Requirement | Needed by | Recorded in `earful.ledger/1` |
| --- | --- | --- |
| Scenario declarations and policy values in the run header | all | `run.start`: `scenario`, `contracts` |
| The framework release and the clock the run used | all | `run.start`: `framework`, `clock` |
| Session options the references are computed from, confirmed against the session | L1, L2 | `run.start`: `scenario.session`; a mismatch is a harness error |
| Everything that decides validity | all | `run.start`: `clock`; `run.end`: `stamp_errors`, `executor_calls`, `underruns`, `harness_errors` |
| True start and end of each user speech | L1, L2 | Speech offsets in `run.start`, plus `user.origin` |
| Completion time of each played word | A3, M1 | `playout.finished`: `words_completed_us` |
| Which scripted reply each synthesized utterance belongs to | A3, M1, M2, L1 | `tts.synthesized`: `call`, `attribution` |
| Tool witness events: called, returned, raised, cancelled, with the returned value as the model should be shown it | M2, L1 | `tool.called`, `tool.returned` (`output`, `text`), `tool.raised`, `tool.cancelled` |
| Backend events: requested, committed, deduplicated, failed, with idempotency key | A1, A2, A3 | `effect.requested`, `effect.committed`, `effect.deduplicated`. **Failed is not yet recorded**: it arrives with backend faults |
| Claims on a scripted reply | A3 | `llm.response`: `claims` |
| Delays each scripted provider applied | L1 | `llm.response`: `ttft_us`, `duration_us`; `tts.synthesized`: `ttfb_us`; `effect.requested`: `latency_us`; VAD and STT delays in `run.start` |
| Pause, resume and clear commands as the sink received them | L2 | `sink.pause`, `sink.resume`, `sink.clear`, `sink.flush` |
| Model contexts and the final chat history, item by item, the latter labelled as framework state | M1, M2 | `llm.request`: `items`; `history.final`: `read_from`, `items`. One item shape, defined in the schema |
| Identifiers: run, speech, model call, reply, utterance, segment, tool call, intent | all | `docs/ledger.md`, Identifiers. A reply is identified by its model `call`. Every intent an event names must be declared |

## 9. Decisions taken

Agreed 16 November 2026. Each is a choice with a reasonable alternative, recorded here so
it can be revisited with the reason in view.

1. **Talking over the user is part of L2.** Rule b is what catches a stale reply that
   starts after the user has begun a new turn. The alternative is an eighth contract, which
   names the failure more clearly but splits one measurement of who holds the floor across
   two contracts.
2. **M1 allows one word beyond the cut word by default.** The measurement is in section 6.
   The alternative is exact agreement by default, with a fifth to a third of ordinary
   interruptions failing on every release. The tolerance was measured on the framework
   under test, so the measured difference is reported on every instance.
3. **"Not exercised" is reported alongside the three verdicts.** The alternative is to
   report a contract with no instances as a pass.
4. **A1's retirement point is the delivery of the correcting transcript.** The alternative
   is the first model call that sees it, which would excuse a tool call started from a
   stale context after the user had already corrected themselves.
5. **A3 times a claim at the end of its last word.** The alternative is the start of its
   first word, which is stricter.
6. **L2's interruption point comes from the declared threshold.** The alternative is the
   framework's reported decision time, which would make the framework a witness in its own
   case and let a late decision pass. Failures that depend on Earful's model of the
   threshold are marked `reference_in_doubt`.
