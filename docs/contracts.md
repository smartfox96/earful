# Contracts

This document defines what Earful checks, as contracts version `earful.contracts/2`. The
checker (`src/earful/core/checker/`), the ledger schema and the deliberate breaks that
prove the checker works are all built against it. Section 10 records what the
checker measured on the reference ledgers. The default policy values rest on those
measurements.

```python
from earful.core.checker import check
report = check(events)          # events: the lines of one ledger
report.outcomes()               # {"A1": "not_exercised", "A2": "fail", ...}
report.failures                 # every failed verdict, with the events it rests on
```

`earful check LEDGER.jsonl` prints the same from a file. The checker reads only
the ledger: it imports neither the scenario nor the framework under test.

`earful.contracts/1` was the version of Earful 0.1.1. Version 2 differs in these rules:

- M2 has rule f, and rule e also holds tool items and replies to their place among the
  user turns. Rule d owes a call that handed the session to another agent in the final
  history and in the model call that follows it up.
- L2 rule b does not count a gap in a streaming buffer as a reply stopping or starting.
- A1, A2 and A3 are unobservable for a tool call that leaves nothing in the backend
  (section 2.6).
- L1 has nothing to judge for a milestone that came due at the instant the scenario
  ended, and gives `silenced_by_a_tool` only to a turn that got no spoken reply.
- A verdict that cannot be judged carries a code (section 5). With nothing to judge it is
  not exercised; with evidence lost it is unobservable, and a run fails on it unless the
  code is allowed.
- L2 has nothing to judge for a speech with no reply under way or waiting to play, and
  rule a holds a reply that starts at the interruption point itself to the bound.
- M1 rule 2 fails a text that played to its end and is absent from memory, whatever
  `behind_us` is.
- The history the agent starts with is left out of every context (section 4), and a
  text could be a reply altered on its way to the voice only if the two share a word.
- M2 rule d loses its evidence for a tool that ran without a call id, where the model
  asked for a call of that tool that nothing else answered.

A bundle kept under version 1 is still replayed. It is judged by version 2, and the
replay says so.

## 1. What a contract is

A contract is a statement about one run that can be decided from the run's ledger alone.
The test author declares which contracts apply and with which policy. A violation means
the run contradicted the declared contract. Whether the framework has a defect is a
separate question, covered in section 7.

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

- The reference record is always something Earful did or measured itself: `world`, `sink`
  or `backend`, or what the scripted model answered (`llm.response`).
- The judged record is the system's behaviour as seen from outside it: the context
  presented to the model (`llm.request`), the audio that reached the output (`sink`), or
  the writes that reached the store (`backend`).
- `framework` events never decide a verdict. They are attached to a verdict as
  explanation, and they may supply correlation identifiers.
- The final chat history (`harness`) is framework state read back by Earful. M2 judges
  it as a context of its own: it is what the next model call would have been shown, and
  the last turn of a run is in no other context. M1 judges a text against it when no
  model call followed the text's playout, and always for a text of which no audio reached
  the sink. A verdict that rests on it says so, in its instance or in a flag.
- The checker does not use the framework's own estimate of what was played.

### 2.3 Order and time

- `seq` is the total order of a run. "Before" means lower `seq`.
- `t_us` is virtual time in microseconds. A deadline is met if the event's `t_us` is at or
  before the deadline.
- Where a rule compares an event with a derived time (for example the instant a word
  finished playing), equal times satisfy "at or before".
- Two things that happen at the same instant are ordered by `seq`, but nothing the
  scenario declares decides that order. Two rules would otherwise turn on it, and in
  those the framework is not held to either reading: a stop that reaches the sink as the
  audio ends (M1, rule 5), and a tool that returns as a call for a later turn is being
  made (M2, rule d). On a virtual clock these ties are exact and repeatable. On a real
  clock either order can occur. Every other rule goes by `seq`.

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

### 2.5 When the ledger allows more than one reading

Two things in a ledger have no identity beyond their words.

- **A synthesized text.** The scripted voice is handed a text with no reference to the
  model call it came from. A framework that generates a reply again for the same turn
  gets the same words from a scripted model, and voices one of the copies. The ledger
  names every reply the text could be (`tts.synthesized`: `candidates`).
- **A remembered message.** An assistant message in a context carries its words and its
  place. When two things the agent said have the same words and one message remains, it
  could be either.

Which copy was voiced, or kept, is not observable, and it makes no difference to what
the user heard or to what the model is shown. The checker reads the ledger every way the
candidates allow and reports the reading most favourable to the system under test: the
fewest failed verdicts, then the fewest unobservable ones. A run fails only if it fails
under every reading.

A reading has to be possible in time. The ledger names candidates by their words, and a
text cannot be a reply whose first token did not exist when the text was synthesized.
The checker drops a candidate when `tts.synthesized`'s `started_us` is earlier than that
reply's `llm.response` plus its `ttft_us`. Streaming metadata is finalized later, so its
recording time cannot be used for attribution. A first token out at the instant the
text is synthesized is in time. A text left with one candidate is that reply in every
reading.

One reading is chosen for the whole run, so no verdict rests on a reading that another
verdict contradicts. The report names the reading taken
(`reading`: utterance to model call) and how many there were (`readings`), and every
verdict that rests on it carries the flag `most_favourable_reading`. Within M1, each
context's messages are matched to what was said the same way: of the matchings that keep
order and words, the one that agrees best with what was played.

A verdict of the reading taken carries the flag `reading_split` when another reading
gives the same contract and instance a worse outcome: a failure where this one passes or
is unobservable, or unobservable where this one passes. A result that holds under every
reading can then be told from one that depends on the reading taken. Another reading
can also judge an instance the reading taken has no verdict for. There is then no
verdict to flag, so if the instance does not pass there, a note on its contract says so.

A ledger with more than 512 readings, or in which the ambiguous texts cannot each be
given a reply of their own, is not read every way: its ambiguous texts stay unattributed,
and what depends on them is unobservable. A text left with no candidate is the simplest
such ledger. The checker's work is bounded by the readings it lists: it follows a choice
only if the texts that remain can still each be given a reply.

### 2.6 A tool that leaves nothing in the backend

A1, A2 and A3 know an intent's writes from the backend alone. A tool that writes some
other way leaves nothing there, and one rule covers it in all three. If the backend
received no write request for an intent in the whole run, and a tool of the intent was
called and made no write request for any intent, what that call did cannot be known. The
intent is then unobservable wherever a write of that call could change the verdict: in A1
for a call after the retirement, in A2 for a call at any time, in A3 for a call made at
or before the instant the claim was heard.

A call that wrote for another intent is accounted for. Once the backend has received a
write request for an intent, its record of that intent is taken as complete: a call that
made no request made no write.

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

If the user hangs up, nothing after that was heard. A word whose last sample was consumed
after the hangup is unplayed, the word under way at the hangup is the cut word, and audio
that starts at or after the hangup was never audible.

This is modeled playout at the output sink. It says nothing about a network, a client
buffer or a caller's device, and every report states that boundary.

## 4. What the test author declares

The checker reads these from the ledger header; it never imports the scenario.

| Declaration | Meaning | Used by |
| --- | --- | --- |
| User speech: start, end, transcript | A scripted utterance. An empty transcript is noise | all |
| `expects_reply` on a speech | The agent owes this turn a response. Default: true when the transcript is non-empty and the speech is not a backchannel | L1 |
| `backchannel` on a speech | The user is acknowledging and does not take the floor | L1, L2, M2 |
| `retires` on a speech | This turn corrects or cancels the named intent | A1 |
| Intent | A logical action the user wants done once, with its authorized effect count (default 1) and the tools that carry it out | A1, A2, A3 |
| Effect on a tool | A call of the named tool is one durable write for an intent, made in place of the tool's body. The tool then counts among the tools that carry out the intent | A1, A2, A3 |
| Claim on a scripted reply | The reply, or a span of its words, tells the user an intent succeeded | A3 |
| Hangup | When the user leaves. The session is closed at that time | A3, M1, L1 |
| Starting history | What the agent's context holds before the run, such as an earlier call's turns. The adapter reads it from the agent and records it as the header's `history`; the checker leaves those items out of every context and of the final history, so an earlier turn is neither a stray user text nor an unscripted reply. Each of its items takes out the first item that is the same, in order | M1, M2, L1 |
| Provider delay | How long a model call, a first audio or a backend write takes, declared as data in place of what the scenario's code would ask for | L1; and it moves what every contract judges |
| Policy values | The tolerances, bounds and budgets named in section 6, in `run.start`'s `contracts`. Times are integer microseconds, as everywhere in Earful. A value a contract does not define is an error | all |

## 5. Verdicts

A contract is checked once per **instance**: each intent, each reply, each user turn, as
section 6 defines. An instance gets one of four verdicts.

- **pass**: the evidence required is present and the rule holds.
- **fail**: the evidence required is present and the rule does not hold.
- **not exercised**: the instance had nothing to judge, such as a deadline after the run
  ended. The verdict carries a code that says why.
- **unobservable**: evidence the rule needs was lost, so neither can be shown. The verdict
  carries a code that says which.

| Code | Kind | Contracts | What happened |
| --- | --- | --- | --- |
| `run-ended` | nothing to judge | A3, M1, L1 | The run ended before the playout finished, or before a deadline passed while the rule could still be met |
| `no-memory-after` | nothing to judge | M1 | No model context or final history follows the reply |
| `no-reply-under-way` | nothing to judge | L2 | No reply was under way, or waiting to play, while the user held the floor |
| `no-interruption-point` | nothing to judge | L2 | The speech was too short to interrupt, and no reply was in flight |
| `no-origin` | nothing to judge | L2 | The ledger has no true speech boundaries |
| `outside-backend` | evidence lost | A1, A2, A3 | A tool for the intent was called and nothing reached the backend (section 2.6) |
| `audio-unattributed` | evidence lost | A3, M1, L1 | A reply's audio cannot be told apart from a text that was not attributed and shares a word with it (`docs/ledger.md`, attribution) |
| `readings-over-cap` | evidence lost | A3, M1, L1 | The same, because the ledger has more readings than the checker tries (section 2.5) |
| `word-start-unknown` | evidence lost | A3 | The start of the claim's first word cannot be derived |
| `memory-unaligned` | evidence lost | M1 | No context lines up with what was said (M2, rule c) |
| `history-before-playout` | evidence lost | M1 | The final history was read before the playout finished |
| `no-stable-text` | evidence lost | M2 | A tool's result has no stable text form to compare (rule d) |
| `tool-unwitnessed` | evidence lost | M2, L1 | A tool behind a call or a result was not witnessed, or cannot be linked to the call |

A contract's result for a run is `fail` if any instance failed, otherwise `unobservable`
if any lost its evidence, otherwise `pass` if any passed, otherwise **not exercised**,
which it also is with no instances at all.

Evidence lost counts against a run as a broken rule does. `assert_contracts` fails on it
and names it as `CONTRACT/CODE`, such as `A2/outside-backend`, and `allow` takes that
name. A search reports each as a rule of its own, with the cases it was lost in and what
was changed (`docs/search.md`, section 3). Nothing to judge does not fail a run. A test may
require that a contract was judged, so a scenario that never reaches the behaviour does
not pass: `assert_contracts(require=["A2"])` fails unless A2 ended with a pass or a fail
(`docs/testing.md`, section 4).

Every verdict carries: the contract, the instance, a one-sentence summary, the rule each
finding broke, the `seq` of each event it rests on, the witnesses those events came from,
the measured numbers, and any qualifying flags. A finding carries the rule it broke, or for a
verdict that was not judged its `code`. The contract's result carries the policy values
used. A report (`earful.report/2`) holds the run id, whether the run was valid, the
reading taken (section 2.5) and one result per contract; it is plain JSON. An invalid run
has no results at all.

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
| In-flight calls | A write requested before the retirement point may still commit after it. This is reported as a late commit and does not fail |
| Policy | `in_flight`: `may_finish` (default) or `must_not_commit`. `reissue_allowed`: intents the correction is permitted to start |
| Evidence lost | `outside-backend`: the backend received no write request for the intent, and its tool was called after the retirement and made no write request at all (section 2.6) |
| Deliberate break | A model policy that calls the tool again after the correction is in its context |
| Clean control | The same scenario with a policy that honours the correction |

### A2. One logical effect

Durable effects for an intent do not exceed the count the scenario authorized.

| | |
| --- | --- |
| Instance | Each declared intent for which the backend received at least one write request, and each for which it received none although a tool for the intent was called (unobservable, below) |
| Judged record | `backend`: committed effects |
| Rule | Fail if committed effects for the intent exceed its authorized count |
| Policy | None of its own. The authorized count is declared on the intent (default 1). The backend mode is declared on the scenario: `plain` (every write commits) or `keyed` (a repeated idempotency key is deduplicated and does not count) |
| Evidence lost | `outside-backend`: the backend received no write request for the intent, and a tool for it was called and made no write request at all (section 2.6). A call that wrote for another intent is accounted for: the intent was not acted on, and has no instance |
| Faults | A write counts if it committed, whatever its caller was told. A write the backend rejected or lost never counts |
| Caller gone | A write counts if it committed, although the tool that asked was cancelled or the user had hung up by then |
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
| Which commit | Any commit for the intent makes the claim true. A claim names an intent and no particular write, so a claim heard after one write committed passes although a second authorized write for the same intent had failed, or was still under way, when it was heard |
| Policy | `claim_point`: `span_end` (default) or `span_start`. Which words make the claim is declared on the claim itself |
| Faults | Only a commit makes a claim true. A write that was rejected, lost, or had not committed when the claim was heard does not, whatever the tool returned |
| Hangup | A claim whose last word finished playing after the user hung up was not delivered |
| Nothing to judge | `run-ended`: the reply's playout never finished (as M1 defines it below), even if another segment played part of it |
| Evidence lost | `audio-unattributed`: the reply's audio cannot be attributed to the scripted reply (`readings-over-cap` where that is because the ledger has more readings than the checker tries, section 2.5). `word-start-unknown`: the start of the claim's first word cannot be derived. `outside-backend`: the backend received no write request for the intent, and a tool for it was called at or before the claim was heard and made no write request at all (section 2.6) |
| Deliberate break | A model policy that says the booking is done in the same reply that calls the tool, with a backend that takes longer to commit than the sentence takes to play |
| Clean control | The same backend, with the claim made only after the tool result is in context |

### M1. Played vs. remembered

What the agent remembers having said matches what was played.

M1 judges everything the agent was scripted to say: each reply the model gave, and each
text the application spoke itself (a greeting, a fixed phrase). For one such text with
words `W`, let `P` be the number of its words played, `C` be 1 if one of its words was cut
and 0 otherwise, and `R` be the words of the assistant message attributed to it in the
judged record.

Memory and playout **agree exactly** when `len(R)` is `P` or `P + C`: the word the playout
stopped inside may be remembered or not. Otherwise the **drift** is the audio between
where memory stops and where it would agree: positive when memory is ahead (the audio of
the words remembered beyond `P + C`), negative when it is behind (the audio of the played
words that are forgotten). Drift is measured in audio time because words differ in
length: two words can be 0.2 s or 1.2 s.

| | |
| --- | --- |
| Instance | Each scripted reply with text and each text the application spoke, once its playout has finished or it has been abandoned |
| Reference record | `sink`: `P`, `C`, and whether the segment was cleared before its audio ended; `world`: the length of each word's audio |
| Judged record | Every model context requested after the playout finished; the final chat history if there is none and it was read after the playout finished. For a text of which no audio reached the sink: every model context requested after the text came to be (the model's response, or the synthesis of a text the application spoke), and the final history as well |
| Rule 1 | `R` is a prefix of `W`. This is how a message is attributed at all (M2, rule c), so a message that is not a prefix of what was said fails M2 and leaves M1 unobservable in that context |
| Rule 2 | Memory is not more than `behind_us` of audio behind what was played. A text that played to its end without being cut short is in memory, whatever `behind_us`: the framework knew all of it was said. The tolerance covers its estimate of how much of a cut-off text was heard, which can come out as none of it |
| Rule 3 | Memory is not more than `ahead_us` of audio ahead of what was played |
| Rule 4 | If it never became audible, it is absent from memory. No tolerance applies. A reply that became audible and was stopped before one whole sample had played is judged by rules 2 and 3 instead: the framework was told playback had begun |
| Rule 5 | The message is marked interrupted exactly when the playout was cut short: a word was left unplayed, or the sink was cleared before the audio ended. The second case covers an interruption in the silence after the last word, however long after. If every word played and a pause or clear reached the sink at the instant the audio ended, either mark is accepted (section 2.3). The audio ended when its last word finished playing. For a segment that played out, the instant the sink reported it finished counts too: that is when the framework learns of it |
| Policy | `ahead_us` (default 1 000 000). `behind_us` (default 1 000 000). `check_interrupted_flag` (default true). A scenario that needs exact agreement sets both to 0 |
| Flags | `judged_on_final_history`: the final history is among the contexts judged, because no model call followed the playout or because no audio reached the sink. `never_reached_the_output`: no audio of it reached the sink, so it is judged from the moment it came to be. `stopped_as_it_ended`: every word played and a stop reached the sink as the audio ended, so rule 5 accepts either mark. `most_favourable_reading`: which copy of identical words was voiced or remembered is a reading (section 2.5). `played_on_after_the_hangup`: the sink consumed audio of it after the user had left, and `P` and `C` are what the user heard |
| Hangup | `P` and `C` count what had played when the user hung up (section 3). The judged record is the final history, read after the session has closed. An output that plays on after the hangup, with a framework that remembers what it played, fails rule 3 |
| Always reported | The drift, `len(R) - P` and `len(R) - (P + C)`, on every instance, pass or fail. A release that drifts inside the tolerance stays visible |
| Nothing to judge | `run-ended`: the run ended before the playout finished. A playout has not finished while a segment that began with its audio is open, even if another segment played part of it; or, where no finished segment holds any of its audio, while any segment is open, since that one may hold it. What an open segment holds after the audio it began with is not known until it finishes. `no-memory-after`: no model context or final history follows it |
| Evidence lost | `audio-unattributed` (or `readings-over-cap`): the audio cannot be attributed to anything the agent was scripted to say. `history-before-playout`: the final history was read before the playout finished. `memory-unaligned`: no context in which it could be compared lines up with what was said (M2, rule c) |
| Deliberate break | A sink that tells the framework playback has started 2.5 seconds before it begins to play, as an output with an unannounced buffer would, while recording the truth |
| Clean control | The unmodified sink |

The one-second defaults come from a measurement. The framework estimates how much text
was spoken, and the sink counts samples. `scripts/contracts_m1_sweep.py` interrupts a
reply at every 50 ms, for four sentences, and reports the drift:

| livekit-agents | Seconds per letter | Sentence | Interruption points | Exact | Most behind | Most ahead |
| --- | --- | --- | --- | --- | --- | --- |
| 1.5.6 | 0.06 | menu | 143 | 113 | 0.42 s | 0.36 s |
| 1.5.6 | 0.06 | booking | 98 | 87 | 0.24 s | 0 |
| 1.5.6 | 0.06 | long words | 128 | 71 | 0.48 s | 0 |
| 1.5.6 | 0.06 | short words | 66 | 43 | 0.18 s | 0 |
| 1.8.12 | 0.06 | menu | 143 | 116 | 0.42 s | 0.36 s |
| 1.8.12 | 0.06 | booking | 98 | 87 | 0.24 s | 0 |
| 1.8.12 | 0.06 | long words | 128 | 71 | 0.48 s | 0 |
| 1.8.12 | 0.06 | short words | 66 | 43 | 0.18 s | 0 |
| 1.8.12 | 0.045 | menu | 106 | 89 | 0.315 s | 0.225 s |
| 1.8.12 | 0.045 | long words | 94 | 15 | 0.675 s | 0 |
| 1.8.12 | 0.09 | menu | 218 | 158 | 0 | 0.63 s |
| 1.8.12 | 0.09 | long words | 194 | 189 | 0.27 s | 0.27 s |

1.6.5 gives the same figures as 1.8.12. The framework's memory of an interrupted reply
is exact at most interruption points and otherwise within about 0.7 s of what was played,
in either direction, depending on the sentence and on how fast the voice speaks. Counted
in words that is anywhere from four words behind to two ahead, so the tolerance is in
audio time. With one second each way an ordinary interruption passes. A reply remembered
in full after two seconds of playout fails, and so does one that is not remembered at
all. A scenario that needs exact agreement sets both tolerances to 0.

The scripted voice gives each word audio in proportion to its letters. A voice that gave
every word the same length, as an earlier version of the harness did, is unlike any real
one, and made the framework's estimate look two words too eager on sentences with short
words. With real voices the drift will differ again; the tolerance is a declared bound,
and the measured drift is on every verdict.

### M2. Turn integrity

Each model call is shown the conversation that happened. Every user turn is there, whole,
once and in order. The history the agent started with is left out (section 4). Every assistant message is something the agent was scripted to say, in
the order it was said. Every tool call that returned is there once, as the model made it,
with the result the tool gave. A reply and a tool call come after the turns that led to
them.

Rules a to f are everything M2 checks. The row "Not judged" lists what a reader could
expect of it and it leaves alone.

| | |
| --- | --- |
| Instance | Each model context, and the final chat history |
| Reference record | `world`: final transcripts delivered, the true start of each speech, tool calls returned. `llm`: the replies the model gave and the tool calls it made. `sink`: when a reply's playout finished |
| Judged record | `llm`: the items in the context |
| Rule a, user turns | The user text in the context, read in order as one stream of words, is the delivered transcripts, each whole and in order, up to some point. Nothing invented, duplicated or reordered. Reading it as one stream means a framework that merges two transcripts into one message is not penalized |
| Rule b, no loss | A delivered transcript is present in every context requested more than `user_commit_lag_us` after the user's turn can be over. A turn can be over once the end-of-turn delay (L1) has passed since the user stopped and the transcript arrived; speech that begins before then extends it. Until that instant the framework may still be holding the transcript, and a call it makes meanwhile for an earlier turn need not show it. A transcript left out while a later one is shown is rule a |
| Rule c, replies | The assistant messages, read in order, match what the agent was scripted to say (the model's replies, and text the application spoke itself) one to one in the order it came to be, each message a prefix of its text. Nothing is matched twice. Fails if no such matching exists. Order decides the match, so two replies with the same text stay distinct when both are remembered; when only one is, either could be the one kept. Rule c needs only that some matching exists; M1 takes the one that agrees best with what was played (section 2.5) |
| Rule d, tool calls | For each tool call that returned before the context was requested, the context holds exactly one call item and exactly one result item, the result after the call, not marked as an error, and carrying the returned value: its text equals the `text` the tool witness recorded. A call that has not returned is owed nothing, whether it raised, was cancelled or is still running: whether and what the model is told is the framework's choice (livekit-agents 1.8 shows a call in progress with the result "The tool call is still in progress."), but it may not be shown twice or out of order. A result marked as an error for a call no tool received is the framework's own account of a call it did not run, and is not judged. A result not marked as an error for such a call fails |
| Rule d, same instant | A context requested at the very instant a tool returned, for a later user turn than the one the tool was called in, is not held to that return (section 2.3). The call that follows up the tool's own turn is |
| Rule d, handoff | A tool that returns an agent hands the session to it (`handoff`). That call is owed its call item and its result item in the final history and, if the tool returned a value beside the agent, in the first model call requested after it returned, which follows it up. The contexts of the agent handed over to are built by the application and need not show the call. One that shows it is held to the rest of rule d. A result marked as an error for such a call is the framework's own account of a handoff it did not make, and is not judged (livekit-agents 1.8 gives one when the reply that called the tool was interrupted). Every other rule holds in those contexts as in any other: an agent that starts from an empty context has lost the user's turns (rule b) |
| Rule e, order | Each reply, each call item and each result item comes after the user turns that were in the context the model answered from. A reply, or a text the application spoke, also comes before every user turn that began after its playout had finished. Its playout finished when the sink finished the last segment holding its audio. A turn began when the VAD segment holding its speech started (L2), so speech that resumes inside a segment begun earlier is not held to this. Equal instants are not judged. Where a backchannel with words was read as absent, a later turn could be that backchannel, and this second half is not applied to the turns after it |
| Rule f, tool items | A call item or a result item belongs to a call the model had made, found by its call id, and shows that call as the model made it. A call item carries the model's tool name and arguments. A result item carries the model's tool name; one whose name the ledger does not give (null) is not compared. Arguments are equal as text, or as JSON where both parse, so a framework that writes the same arguments out again is not penalized. A call item whose id the model had not given when the context was requested fails. A result item with such an id is rule d |
| Discard policies | `noise`: speech with no transcript leaves no turn. `backchannel`: its transcript may be present or absent; where its words could equally be the start of the next turn, the reading that explains the most user text is taken. `cancelled_tool`: a call cancelled before it returned needs no result |
| Policy | `user_commit_lag_us` (default 1 000 000): how long after the turn can be over a transcript may still be missing. In every reference ledger a transcript is in the first context requested after its delivery |
| Evidence lost | `tool-unwitnessed`: rule d for a tool that was not witnessed, or that ran without a call id to link it by. `no-stable-text`: the value comparison in rule d, for a result with no stable text form (`text` is null); presence and order are still checked |
| Always reported | `stand_in_results`: results shown for calls that had not returned, and error results shown for a handoff. `refused_calls`: error results for calls no tool received |
| Not judged | Whether a reply is in the context at all, and how much of an interrupted reply is kept. That is M1. The order of tool calls among themselves, and of a tool call against a reply. Where a reply sits among the user turns, beyond rule e. How often a call item appears for a call no tool received |
| Deliberate break | A public agent hook that removes tool results from the context before the model call. Further variants repeat a user turn, show a tool call with other arguments, and show the tool calls before the turn that led to them |
| Clean control | The unmodified agent |

### L1. Progress

A turn that is owed a response gets one within its budget, and nothing blocks the turns
after it.

| | |
| --- | --- |
| Instance | Each delivered speech with `expects_reply`, unless the user speaks again first. Then the later speech carries the obligation |
| Start of the clock | The later of the speech's end and the delivery of its final transcript, both `world` |
| Milestone 1 | A model call whose context contains the turn |
| Milestone 2 | If the model answered with tool calls: every call is made, every call finishes, and a further model call follows. Repeats up to `max_steps`. A tool cut off when the scenario ended did not finish. If every tool of a round asked for silence (`expects_reply` false), no further call is owed. A tool that returns only an agent hands the session over and asks for none; with a value beside the agent a further call is owed as for any tool. A call the framework answered itself without running the tool (arguments the tool cannot take, a tool that does not exist) counts as made and finished when the model finished asking: the context that first shows its error result is the further call. A call to a witnessed tool that neither ran nor was answered was never made |
| Milestone 3 | The first word of a reply to the turn starts playing. Any reply will do, including one the framework generated again for the same turn (after a backchannel, or because what it had generated ahead of time was out of date); the one heard first counts. However the chain ends (the rounds run out, a call is never answered, the model says nothing), the turn is still owed this, unless its tools asked for silence |
| Rule | Fail if a milestone has not occurred by its deadline. The deadline is the start of the clock, plus the declared end-of-turn delay, plus the scripted provider delays on the chain so far, plus `slack_us`. A finding names the milestone that was missed (`milestone 1`, `milestone 2`, `milestone 3`), so that a failure to call the model and a reply that never plays are different failures |
| End-of-turn delay | The scripted VAD's `min_silence_us` plus the session's `min_endpointing_delay_us`: the latest the framework can decide the turn is over |
| Delays on the chain | For each step already taken: the model's `duration_us`; if it spoke, the `ttfb_us` of its own utterance plus the length of its audio, since the framework finishes speaking before the next step; and, for each write its tools made, the backend `latency_us`, or the wait before the caller was told the write timed out. A tool's own running time is not on the chain: the scenario's code decides it and nothing declares it. A turn whose tool takes long by design needs a larger `slack_us` |
| Excused | The user began speaking again before the milestone was due. If that speech expects a reply it carries the obligation, and this turn has no verdict. If it is noise or a backchannel, the turn keeps the obligation and its clock restarts when that speech ends plus the false-interruption timeout. A header whose `false_interruption_timeout_us` is null adds nothing: Pipecat declares none, because it does not resume a reply. The user hung up before the milestone was due: the turn has no verdict. A milestone that was already overdue when the user left still fails |
| Policy | `slack_us` (default 1 000 000). `max_steps` (default 4) |
| Nothing to judge | `run-ended`: the scenario ended (the final history was read) at or before the deadline, where the milestone could still have been met (section 2.3) |
| Evidence lost | `tool-unwitnessed`: a tool the model called was not witnessed. `audio-unattributed` (or `readings-over-cap`): a reply's audio cannot be attributed, and it may have come first |
| Flags | `silenced_by_a_tool`: the turn got no spoken reply because its tools asked for none. A turn that was spoken to all the same, by the reply that called the tool or by the agent handed over to, does not carry it. `most_favourable_reading`: which of the turn's replies was voiced is a reading (section 2.5) |
| Reported separately | Time to the first model call and time to first audio |
| Deliberate break | A tool that never returns and has no timeout; a second variant where the model returns nothing at all |
| Clean control | The same turn declared `expects_reply = false`; the same tool returning |

### L2. Interruption

While the user holds the floor the agent is silent, within a declared bound.

The **floor window** of a speech runs from the instant the scripted VAD reported speech
to the framework until the true end of the speech. The **interruption point** is the
speech's true start plus the declared minimum interruption duration; a speech shorter than
that has none. A framework can still interrupt for a shorter speech, because a VAD
counts a segment as speech until it reports the end (`docs/ledger.md`). L2 asks nothing
of the framework there.

A VAD hears speech that resumes within its silence window as one **segment**
(`docs/ledger.md`), and a framework counts speech from the start of the segment. So for a
speech that resumes inside a segment the VAD has already reported, the floor window opens
at the speech's own true start, and its interruption point is the later of that start and
the segment's start plus the minimum interruption duration. It has one only if that
moment falls while the speech is under way, its last instant included. The pause
between two speeches of a segment is in neither floor window: the user is not speaking.

A reply is **under way** from the start of its segment, or from a resume the framework
asked for, until the segment finishes or the output is asked to pause. A streaming buffer
that runs empty leaves a gap in the audio and does not end this: nobody stopped the
reply, and it plays on when more audio arrives. The sink records such a gap as
`playout.paused` and `playout.resumed` with no `sink.pause` in force. A reply with a gap
is held to what the same reply without it is held to.

| | |
| --- | --- |
| Instance | Each speech that is neither noise nor a backchannel, and that the scripted VAD reported. A speech it never reported held no floor, whether it was too short for the VAD or the run ended first: it is named in a note and has no verdict |
| Reference record | `world`: the floor window and interruption point |
| Judged record | `sink`: when it was audible, when segments started, and when it was asked to pause and to resume |
| Rule a, stop | If a reply is under way at the interruption point, having begun at or before it, the sink stops being audible by that point plus `stop_bound_us`. A reply that starts at the point itself may start there, since the two are the same instant (section 2.3), and is held to the bound like any other. The reply stopped at the end of its last audio that began before the floor window closed. So audio that returns after a gap, while the user still speaks, counts, and a gap that lasts from before the point until the user has stopped leaves the agent silent. Rule b keeps it silent from then until the floor window closes |
| Rule b, no start | No segment starts, and none is resumed after a pause the output was asked for, inside the floor window later than `start_grace_us` after it opens. Both ends of that span are excluded. Excusing a start at the instant the window opens excuses the start only: where that instant is the interruption point, rule a holds what plays on. A reply that plays on after a gap in its buffer does not start: it was under way, and rule a judges it. A pause of zero length silences nothing: it is no resume and does not end the reply |
| Policy | `stop_bound_us` (default 100 000). `start_grace_us` (default 0). `interruptions_allowed` (default true; if false, rule a does not apply) |
| Reference in doubt | A rule a failure in which a pause or clear command did reach the sink, but later than the interruption point plus `stop_bound_us`, is marked `reference_in_doubt`. It stays a failure. It is not published or attributed until someone confirms that the computed interruption point matches the framework's documented threshold on that release. A failure with no command at all, or with a timely command the sink did not obey, is not marked |
| Nothing to judge | `no-reply-under-way`: no reply was under way at the interruption point, none was waiting to play during the floor window (its synthesis began before the window closed and it had not finished playing when the window opened), and no rule broke. `no-interruption-point`: the speech was too short to have an interruption point, and no reply was in flight. `no-origin`: the ledger has no true speech boundaries. So `require=["L2"]` means an interruption, or a reply held back, was judged |
| Reported separately | Recognition delay: from the true start of speech to the first pause or clear command the sink received. Decision lag: from the interruption point to that command; it can be negative. Stop latency: from that command to silence |
| Deliberate break | A sink that keeps consuming for 400 ms after it is told to pause or clear; a second variant with interruptions disabled on the agent while the policy still expects them |
| Clean control | The unmodified sink; interruptions disabled with `interruptions_allowed = false` |

The interruption point is computed from the declared threshold. The moment the framework
says it decided is not used, which keeps the reference record independent of the
framework. In return Earful must model the framework's threshold correctly: if a release
changes how it counts speech, the computed point is wrong and failures would be false.
The `reference_in_doubt` mark exists for that case, and the decision lag on every
instance shows the drift before it causes a failure.

Stop latency is zero in every run with the standard sink, because the sink stops the
instant it is told to. Rule a therefore tests whether and when the framework issues the
command. How fast an output device obeys it is outside the rule.

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
| Which speeches the VAD heard as one segment | L2 | `run.start`: the speeches and `providers.vad.min_silence_us`; `user.vad_start` and `user.vad_end` name the segment's first and last speech |
| The provider delays the scenario declared | all | `run.start`: `scenario.delays` |
| When the user hung up, if they did | A3, M1, L1 | `run.start`: `scenario.hangup_us`; `user.hangup` |
| Completion time of each played word | A3, M1 | `playout.finished`: `words_completed_us` |
| Which scripted reply each synthesized utterance belongs to | A3, M1, M2, L1 | `tts.synthesized`: `call`, `attribution` |
| Tool witness events: called, returned, raised, cancelled, with the returned value as the model should be shown it, and whether the outcome asks for a further model call | M2, L1 | `tool.called`, `tool.returned` (`output`, `text`, `expects_reply`), `tool.raised` (`expects_reply`), `tool.cancelled` |
| The length of each synthesized word's audio | M1, L1 | `tts.synthesized`: `word_samples`, `sample_rate` |
| Which tools the tool witness records, so that a call that was never made can be told from one that could not be seen | M2, L1 | `run.start`: `scenario.witnessed_tools` |
| Backend events: requested, committed, deduplicated, failed, with idempotency key | A1, A2, A3 | `effect.requested`, `effect.committed`, `effect.deduplicated`, `effect.failed`, `effect.timed_out` |
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
2. **M1 has a default tolerance.** The alternative is exact agreement by default, which
   fails ordinary interruptions on every release. The tolerance was measured on the
   framework under test, so the measured drift is reported on every instance. The
   tolerance was first one word; decision 11 replaced it.
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

Taken while building the checker, 16 November 2026:

7. **The checker never guesses which reply a remembered message is.** When two replies
   have the same text and only one is remembered, order cannot say which. Both were left
   ambiguous and M1 reported them unobservable. The alternative considered then, taking
   the earlier one, reports two false failures where there is one unknown. Decision 22
   replaced this. The checker still does not guess, and it no longer reports the reply as
   unobservable.
8. **M1's deliberate break is an output that starts late.** A misreported playback
   position would break nothing: the framework does not use the position an output
   reports to decide what it remembers. It runs its own estimate from the moment the
   output says playback started. An output that says so 2.5 seconds early makes memory
   run ahead of what was played. Any buffer the framework does not know about has the
   same effect.
9. **L1's budget is built only from what the scenario declared.** Each deadline is the
   declared end-of-turn delay plus the scripted delays of the steps taken so far, plus one
   allowance of slack. The alternative, measuring each step from when the previous one
   happened, forgives a turn that falls behind a little at every step.
10. **Policy values are versioned with the contracts.** The ledger carries them as
    declared, and `earful.contracts/2` defines which exist. A scenario that
    declares one that does not exist is refused, by the harness before the run and by the
    checker.

11. **M1's tolerance is audio time.** Measured over four sentences and three
    speaking rates, the framework's memory is anywhere from four words behind to two
    ahead of what was played, and always within about 0.7 s. A tolerance in words is too
    loose for short words and too tight for long ones. The first tolerance, one word, had
    been measured on a single sentence spoken by a voice that gave every word the same
    length.
12. **Text the application speaks itself is judged like a reply.** A greeting spoken with
    the framework's `say` is played and remembered like anything else. The alternative,
    ignoring it, would fail M2 on every agent that greets, or leave what was said outside
    the contracts.
13. **Speeches closer together than the VAD's silence window are refused.** A VAD hears
    them as one speech; reporting an end and a start at the same instant, as the harness
    would, is something no VAD does. Decision 23 replaced the refusal with a model of
    what a VAD does.
14. **A tool that raised is not held to rule d's presence.** Frameworks and releases
    differ in what they show the model for one; a raise that asks for silence leaves no
    trace at all on older releases.

Taken after sweeping 22 scenario shapes through the checker (section 10), 16 November
2026. Each replaced a rule that failed an ordinary agent, or could not see what the
ledger was able to show.

15. **Only a tool call that returned is owed its result.** While a call is still running,
    livekit-agents 1.8 shows the model a stand-in result; earlier releases show nothing.
    The first rule d failed any result with no witnessed return, and so failed every slow
    tool that the user interrupted on 1.8. What a framework shows for a call that has not
    returned is its choice. The stand-ins are counted on every verdict.
16. **A call the framework refuses is its own account, and counts as progress.** Given
    arguments the tool cannot take, or a tool that does not exist, every supported
    release answers the model with an error result and never runs the tool. M2 does not
    judge that result, and L1 counts the call as made and finished. A result that is not
    marked as an error, for a call no tool received, still fails.
17. **The header names the tools the witness records.** Without it, a tool that was never
    called looked the same as a tool Earful could not see, and a turn whose tool call the
    framework dropped was unobservable. With it, the absence is evidence.
18. **A transcript is owed once its turn can be over.** Rule b used to add that a
    transcript never disappears once it has appeared. A framework that answers ahead of
    time shows the model a transcript it has not yet committed; a call it then makes for
    the turn before does not hold it. The lag is now measured from the instant the turn
    can be over, so a long endpointing delay, or a user who goes on speaking, does not
    fail.
19. **Any reply to a turn is its response.** L1 followed a turn's replies only through
    its tool rounds, and so missed a reply the framework generated again after a
    backchannel: the turn was answered and the verdict said it never was.
20. **Rule 4 turns on whether a reply became audible.** It turned on whether one whole
    sample had played. A reply that started 3 us before the user's speech reached the
    interruption threshold was stopped at once; the framework remembered its first word,
    and rule 4 failed it with no tolerance, where one more sample would have made the
    same memory an exact match.
21. **An order nothing in the scenario decides is not held against the framework**
    (section 2.3). Two such instants turned up: a stop arriving as the audio ends, and a
    tool returning as a call for a later turn is made. The alternative, deciding by
    `seq`, is repeatable on a virtual clock and tells a user nothing about their agent.
    The exception is kept narrow. In rule d it covers only a call for a later turn, so a
    follow-up call that drops its tool's result is still caught.
22. **Identical words are read the kindest way** (section 2.5). Decision 7 left a reply
    unobservable whenever the same sentence was said twice and one copy was voiced or
    kept. A framework does that by itself: it generates a reply again for the same turn,
    and a scripted model gives the same words. On livekit-agents 1.8.12 that left M1
    unobservable in 134 of 3 168 swept runs, L1 in 51 and A3 in 10. Which copy it was
    cannot be observed, in the ledger or in production, and nothing a user hears depends
    on it. The ledger names every candidate, the checker checks every reading, and a run
    fails only if every reading does. The alternative, choosing the earliest or the
    latest copy, would be a guess.

Taken while building the search (`docs/search.md`), 16 November 2026:

23. **Speech that resumes within the VAD's silence window is one segment.** Refusing such
    scenarios (decision 13) put the moments around a turn's commit out of reach: a user
    who speaks again half a second after stopping does so as the
    turn is committed, the model is called and the tool runs. The scripted VAD now does
    what a VAD does, reporting one segment, and L2 takes the floor window and the
    interruption point from the segment.
24. **A provider delay can be declared as data.** How long a model call or a backend
    write takes was a matter for the scenario's code, so a search could move the user's
    speech and could not move a tool's completion. A declared delay takes the place of
    the code's, is stated in the header and is recorded where it is applied. L1 budgets
    each reply with the time to first audio its own utterance had.
25. **L1 names the milestone a turn missed.** Its findings all carried one rule name, so
    a search or a reduction that follows "the same failure" could slide from a model that
    was never called to a reply that never played without noticing.

Taken while adding backend faults, agreed 17 November 2026:

26. **A backend fault is declared as data, for one write.** The caller is told what a
    real client is told: an error, or a timeout that says nothing about whether the write
    committed. The ledger records the outcome of a write and what its caller was told as
    separate events. A2 and A3 judge the outcome. The alternative, a tool written to fail
    in the scenario's code, cannot be added by a search or removed by a reduction.

Taken while adding the hangup, agreed 17 November 2026:

27. **A write's outcome does not depend on its caller.** It is scheduled when the write
    is requested, so a tool that is cancelled while it waits has still written. Before,
    the write was part of the tool's own task and was cancelled with it, which let the
    framework under test change the backend's record, and made a cancelled call that
    later commits impossible to reach. A service that has received a request carries it
    out whether or not the client is still listening. A backend that abandons the write
    is the `backend.drop` fault. On the five supported releases the framework lets a
    running tool finish, both when its reply is interrupted and when the session closes.
28. **A user who hangs up is owed nothing that was not yet due, and heard nothing
    after.** The scenario declares when the user leaves, and the session is closed then
    as a participant's disconnect closes it. L1 gives no verdict to a turn whose next
    milestone was not yet due. M1 and A3 count what had played by the hangup. The
    alternative for M1, counting everything the sink consumed, passes an output that
    plays a confirmation to nobody and a history that records it as said.

Taken at an architecture review, 17 November 2026:

29. **A reading has to be possible in time, and a verdict says when the reading decides
    it** (section 2.5). The scripted voice named a reply as a candidate from the moment
    the model decided its answer, which is before the first token. A reading could then
    have the voice speak a reply the model had not begun to give, and a run could pass
    on it. The scripted voice now names a reply from its first token on. The checker
    also drops such a candidate itself, because a ledger may come from an adapter that
    names candidates some other way. A pass under one reading of two also
    looked the same as a pass under both, so a verdict that another reading makes worse
    now carries `reading_split`. The alternative, reporting the verdicts of every
    reading, leaves the comparison to whoever reads the report.

Taken while reading each rule against the code, 19 November 2026:

30. **M2 says what it checks, and holds a tool item to the call the model made.** Its
    summary promised every reply and every tool call, once each and in order. The rules
    checked less. A call item renamed, given other arguments and moved ahead of the turn
    that led to it passed in every context, and so did a reply recorded after a turn
    that began once it had played. Rule f now compares a tool item with the model's
    call. Rule e covers tool items, and holds a reply to come before a turn that began
    after its playout finished. The summary lists what is checked and the "Not judged"
    row what is left alone. The alternative for order, one total order over every item,
    needs a time for each item, and no context states one.
31. **A gap in a streaming buffer neither stops a reply nor starts it.** Rule b counted
    every resume as a start. A reply already playing, with a gap of one microsecond
    between the VAD's report and the interruption point, failed, and the same reply
    without the gap could play on to the bound. L2 now follows what the output was
    asked to do. Playing on after a gap is the reply continuing, held by rule a to the
    bound. A resume after `sink.pause` inside the floor window is still rule b: the
    framework chose to speak while the user held the floor. The alternative, excusing
    every resume of a reply that was audible before the floor opened, would pass a
    framework that pauses for the user and then resumes over them.

## 10. What the reference ledgers measure

`earful check tests/golden/lk-*.jsonl --measures` prints these. The 18 LiveKit reference
ledgers are three cases on six releases. The three Pipecat reference ledgers
(`tests/golden/pc-*`) pass every contract they exercise.

| Measure | Observed | Default it supports |
| --- | --- | --- |
| L2 decision lag: declared interruption point to the command | 0 in every interruption | `stop_bound_us` 100 000, two ticks of the scripted VAD |
| L2 stop latency: command to silence | 0 in every interruption | the same |
| L2 recognition delay: true start of speech to the command | 500 000 when the agent was speaking; 50 000 when the repaired releases pause before a stale reply starts | reported only |
| L1 time to the first model call | 0 to 300 000 | `slack_us` 1 000 000 |
| L1 time to first audio | 500 000 to 800 000 | the same |
| L1 smallest margin before a deadline | 1 690 000 | the same. Every milestone is on time even with `slack_us` 0 |
| M2 lag from a transcript's delivery to its appearance in a context | 0 | `user_commit_lag_us` 1 000 000 |
| M1 drift | 0 in every reference ledger | `ahead_us`, `behind_us` 1 000 000; the sweep in section 6 has the full distribution |

Outcomes on the reference ledgers: every contract passes or is not exercised, except
where a release has a known defect. On `book_then_interrupt`, releases before 1.6.6 fail
A2 (two durable bookings, one authorized) and M2 rule d (the first booking finished
before the next model call, which was shown neither the call nor its result). On
`speak_over_new_turn`, 1.5.6 fails L2 rule b (a segment started 250 000 µs after the user
took the floor). The same checker, policies and cases produce both the failures and the
passes.

### Sweeps

`scripts/contracts_sweep.py` moves one user speech across 23 scenario shapes in 50 ms
steps and checks every run: the three reference cases, and twenty things ordinary agents
do. Among them are a slow tool interrupted while it runs, a tool that raises, a call the
framework refuses, two rounds of tools, a backchannel, noise, a speech shorter than the
interruption threshold, an output that cannot pause, a slow recognizer, a long
endpointing delay, a greeting, a user who pauses mid-sentence and a user who goes on
speaking. The moving speech starts as close to the one before it as it can, so the
sweeps cover speech that resumes inside the VAD's silence window. That is 3 441 runs on
each release. L2 has nothing to judge for at least one speech in 3 371 of them: the first
speech of a shape, with no reply under way, and the moving speech where it lands while
nothing plays.

What fails:

- **The first known defect**, on the three releases before 1.6.6: A2 in 107 runs, and M2
  rule d in 612 runs on 1.5.6 and 1.5.7 and 590 on 1.6.5. M2 shows it in nine shapes:
  wherever a reply that called a tool is interrupted.
- **The second known defect**, on 1.5.6: L2 rule b in 219 runs, across 21 of the 23
  shapes.
- **A reply that starts over the user in a narrow window**, on 1.6.5 and 1.6.6: L2 rule b
  in 27 runs across 12 shapes, two or three neighbouring steps in each. A reply starts
  270 to 400 ms after the user took the floor. 1.5.7, 1.8.12 and 1.8.13 do not show it.
  One of the 27, `book_then_interrupt` with the second speech at 8.40 s, breaks the rule
  on the real clock too, in two runs of two. It is read as a defect of those two
  releases that a later release repaired, of the same kind as the second known defect.
  It has no page under `docs/cases/` yet.
- **An output that cannot pause**, on every release: L2 rule b in the 3 runs of that
  shape in which the second speech starts at 3.00 to 3.10 s. A reply to the turn before
  starts 150 ms after the user took the floor, and is cleared once the interruption is
  recognized. The repaired releases keep such a reply from starting by pausing the
  output first, which this output cannot do. The real clock agrees on 1.8.12. It is read
  as a consequence of the setting, and as a question for the framework's maintainers
  more than a defect.

No other rule fails on any release, and no verdict on any release lost its evidence:
where a framework generates a reply again and the scripted model gives the same words,
the run is read as section 2.5 describes. These figures were measured again on 22
November 2026, after the changes listed at the top of this page, and none moved.

A failure that has not been classified (section 7) is not reported here. The sweep is
how such results are found; they are published once someone has reproduced them
deliberately and compared them with what the framework documents.
