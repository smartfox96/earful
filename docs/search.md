# Search, reduction and replay

Status: **implemented**, 16 November 2026, in `src/earful/core/` (`case.py`, `search.py`,
`shrink.py`, `bundle.py`, `predicate.py`).

The checker says whether one run broke a contract (`docs/contracts.md`). This document
covers what surrounds it: how Earful looks for the timings and faults under which a
scenario fails, how wide the failure is, how a failing case is reduced to the smallest one
that fails the same way, and what is kept so that anyone can run that case again.

```python
from earful.core import bundle, search
from earful.core.predicate import Predicate
from earful.core.shrink import nearest
from earful.livekit import scenario

author = scenario.default("earful.livekit.cases:book_then_interrupt")   # earful.case/1
found = search.explore(author, scenario.run)       # every time to every boundary, every fault on every write
found.predicates()                                 # the rules some run broke
found.windows(Predicate("A2", "rule"))             # how wide each failure is
found.coverage()                                   # which boundaries were reached, and which were not
case = nearest(author, [t.candidate.case for t in found.breaking(Predicate("A2", "rule"))])
bundle.capture(directory, case, scenario.run, Predicate("A2", "rule"), author=author)
```

```bash
earful search earful.livekit.cases:book_then_interrupt --keep A2/rule --out out
earful replay out/lk-1.5.6.book_then_interrupt.A2-rule
```

All of it is framework-independent. The one thing an adapter supplies is `run`: a
function from a case to the ledger of running it. For LiveKit that is
`earful.livekit.scenario.run`.

A test calls these through `earful.livekit.api`: `run`, `explore`, `keep` and `replay`
(`docs/testing.md`).

## 1. A case is data: `earful.case/1`

Cases and bundles follow the format policy in `docs/ledger.md`: optional fields can be
added within `/1`; execution settings and contract policy values remain checked.

A scenario has two parts. Its code, the agent and the model policy, stays in the module it
is written in and is named by a reference. Everything else is data. Because that part is
plain JSON, a case can be moved in time, reduced, written to a file and run again later
without touching code.

| Field | Contents |
| --- | --- |
| `schema` | `earful.case/1` |
| `schema_minor` | Additive format revision; absent means 0 |
| `scenario` | Where the code lives, as `module:function`. The function takes no arguments and returns the scenario with its default speeches and settings. A scenario built in the process that runs it has no name and is given as `__inline__:case`: its data cannot be run anywhere else (`docs/testing.md`, section 6) |
| `seed` | Seed for every random choice in the run |
| `speeches` | The scripted user's speeches, in order of their start. Per speech: `start_us`, `end_us`, `transcript` (empty for noise), `stt_delay_us`, `expects_reply` (null: yes, unless noise or a backchannel), `backchannel`, `retires` |
| `hangup_us` | When the user hangs up, as an offset from the origin, or null if they stay. The session is closed at that time |
| `delays` | Provider delays declared as data, each in place of what the scenario's code would ask for. Per delay: `of` (`llm.ttft`, `llm.duration`, `tts.ttfb` or `backend.latency`), `nth` (which model call, utterance or backend request, from 1) and `us` |
| `faults` | Faults declared as data, each for one backend write. Per fault: `of` (`backend.error`, `backend.timeout` or `backend.drop`), `nth` (which backend request, from 1) and `us` (how long the caller waits; null for `backend.error`) |
| `settings` | Timing and provider settings, by name. Times are integer microseconds |
| `contracts` | Contract id to the policy values declared for it, as in the ledger header |

The shape is a JSON Schema document shipped with the package
(`src/earful/core/schemas/earful.case-1.schema.json`). Speeches must be in order, end
after they start and not overlap; a hangup comes no earlier than the end of the last
speech; a delay is declared at most once, and a write has at most one fault.

The adapter defines which settings exist. The LiveKit adapter has
`seconds_per_letter_us`, `tts_ttfb_us`, `vad_min_speech_us`, `vad_min_silence_us`,
`min_interruption_duration_us`, `false_interruption_timeout_us`,
`min_endpointing_delay_us`, `max_endpointing_delay_us`, `drain_delay_us`,
`drain_timeout_us`, `backend_mode` and `sink_can_pause`. It refuses any other name and
any value of the wrong kind. `scenario.default(reference)` gives the case as its author
wrote it; `scenario.describe(case, reference)` gives the data of a `Case` built in Python;
and running a case from its data gives the same ledger, byte for byte, as running it
directly.

The Pipecat adapter shares `seconds_per_letter_us`, `tts_ttfb_us`, `drain_delay_us`,
`drain_timeout_us` and `backend_mode`. Its turn settings are `vad_start_us`, `vad_stop_us`,
`user_speech_timeout_us` and `user_turn_stop_timeout_us`. Its voice accepts
`tts_word_timestamps` as a boolean. [Pipecat scenarios](pipecat.md)

### Variables

Every time in a case is a **variable**: something that can be moved on its own.

| Variable | What moving it does |
| --- | --- |
| `speech.position` | Moves a whole speech, its length kept |
| `speech.end` | Moves the end of a speech, so its length |
| `speech.stt_delay` | Changes how long its transcript takes |
| `user.hangup` | Moves the moment the user hangs up. For a user who stays, moving it declares a hangup |
| `llm.ttft`, `llm.duration` | The time to first token, or the total generation time, of one model call |
| `tts.ttfb` | The time to first audio of one synthesized utterance |
| `backend.latency` | The time one backend write takes to commit |
| `backend.wait` | How long the caller of one backend write waits before it is told the write timed out. Exists only for a write with a `backend.timeout` or `backend.drop` fault |

A speech variable is named with the speech's index, a delay or a wait with its
occurrence, and the hangup with 0: `speech.position 1`, `backend.latency 2`,
`user.hangup 0`. A delay the case does not declare is
still a variable. Its value is whatever the scenario's code asked for, which the ledger
records, and moving it declares it.

### Faults

A fault applies to one backend write, named by its request number.

| Fault | What the backend does | What the caller is told |
| --- | --- | --- |
| `backend.error` | Rejects the write at its latency. Nothing commits | `BackendError` |
| `backend.timeout` | Commits the write at its latency, as it would without the fault | `BackendTimeout`, after `us` |
| `backend.drop` | Loses the write. Nothing commits | `BackendTimeout`, after `us` |

A timeout has two orders. With a latency shorter than the wait, the write commits and the
caller is then told it timed out. With a longer latency, the caller is told first and the
write commits afterwards. The caller cannot tell these apart, or tell either from a drop.

The exceptions are in `earful.core.backend`. A tool that does not catch them fails, and
the framework reports the failure to the model as an error result.

A write's outcome does not depend on its caller. It is scheduled when the write is
requested, and a tool that is cancelled while it waits has still written. A backend that
abandons a write when its caller goes away is the `backend.drop` fault on that write.

### Hangup

`hangup_us` is when the user leaves. The adapter then closes the session as a
participant's disconnect closes it. For LiveKit that is the public `AgentSession.aclose`.
Writes already requested still have their outcome, and the final history is read after
the close. The ledger records the moment as `user.hangup`.

Nothing after the hangup was heard. A word the output finishes after it does not count as
played, and audio that starts after it never became audible (`docs/contracts.md`, M1 and
A3). A turn whose response was not yet due when the user left has no L1 verdict.

Some values give no case: a negative time, a speech that would end before it starts or
overlap another, a hangup before the last speech is over. Speeches may be as close
together as they like. A VAD hears speech that resumes within its silence window as one
segment, and so does the scripted one (`docs/ledger.md`).

## 2. The same failure: a predicate

A failure is named by the contract and the rule of it that broke: `A2/rule`,
`L2/rule b`, `L1/milestone 2`. The instance (which speech, which reply, which context) is
left out: a search moves speeches and a reduction removes them, so instances are
renumbered while the failure stays the same.

A run **breaks a predicate** when its report has a failed verdict of that contract with a
finding of that rule. Three kinds of run break nothing, because nothing can be concluded
from them: a run that is not valid evidence, a case the adapter refuses, and a run that
raises. Each is counted and reported; none ends a search. A search that could judge none
of the cases it tried has no result, and its assertion fails.

## 3. The boundary scheduler

Timing defects occur at boundaries: the user interrupts as the first audio arrives, or a
tool returns as the turn is being committed. Stepping a time in fixed increments finds
one only if a step lands on it. The scheduler reads the boundaries from the ledger and
places events at them.

**Transitions.** Run the case. Every instant at which the run recorded an event is a
transition. So are the instants the ledger states and no event marks: the model's first
token and its last, the voice's first audio, the middle of a word, the end of every played
word, and the moment a user turn can be committed.

**Handles.** Moving a variable moves one or more handles, the moments at which it acts on
the system. They are computed from what the run declared. What the framework did is not
used.

| Handle | When, and which variables move it |
| --- | --- |
| `vad_start` | The VAD tells the framework the user is speaking: the speech's start plus `min_speech_us`. Moved by `speech.position` |
| `interrupt` | The speech has lasted long enough to interrupt: its start plus `min_interruption_duration_us`. Absent for a shorter speech. Moved by `speech.position` |
| `transcript` | The final transcript arrives: its end plus `stt_delay_us`. Absent for noise. Moved by `speech.position`, `speech.end` and `speech.stt_delay` |
| `vad_end` | The VAD tells the framework the user stopped: its end plus `min_silence_us`. Moved by `speech.position` and `speech.end` |
| `turn_end` | The turn can be committed: its end plus the longer of `min_silence_us` and `min_endpointing_delay_us`. Moved by `speech.position` and `speech.end` |
| `done` | A provider delay runs out: the model's first token or last, the first audio, the commit. Moved by that delay |
| `hangup` | The user leaves and the session is closed. Moved by `user.hangup` |

**Candidates.** For each handle, each transition and each offset, change the handle's
variable so that the handle falls there. The offsets are 10 ms before, exactly at, and
10 ms after: one audio frame, the finest regular step at which a framework is given audio.
Cases that coincide are one candidate. Each candidate is run and checked.

A delay is placed against the transitions of the case as it is. A speech is placed against
the transitions of the run without that speech: the timeline it would interrupt. A hangup
is placed against the transitions of the run in which the user stays. No delay
is made longer than a limit, 10 s by default. A model that answers after a minute is a
different scenario and is outside the search.

By default the search moves where every speech sits, every provider delay and the
hangup: the user leaves at every transition from the end of their last speech on.
`speech.end` and `speech.stt_delay` can be added, and the search can be limited to some
variables, some speeches, some handles, or the boundaries other than word ends.

**Faults.** The search adds each fault to each backend write the run made, one fault per
candidate. The caller waits 1 s unless told otherwise (`wait_us`). A timeout is run in
both orders. One is the scenario's own: its write commits before that wait is over, or
after. The other is made by letting the write take 1 s longer than the wait, or the caller
wait 1 s longer than the write. Two more handles follow from a fault:

| Handle | When, and which variable moves it |
| --- | --- |
| `gave_up` | The caller of a timed-out or dropped write is told so. Moved by `backend.wait` |
| `done` | A write with a timeout commits. Moved by `backend.latency` |

Each is placed against the transitions of the run with the fault in place.

**Two things at a time.** Both are off by default.

- `pairs=True` moves every speech again with each fault in place, and the hangup if it
  is among the variables, and adds faults to two different writes at once. The speech is
  placed against the transitions of the faulted run without that speech.

  It also makes a provider slow and moves every speech and the hangup against that. A
  write that commits at once has no moment at which it is under way, so a speech or a
  hangup moved alone never falls inside it. Each backend write and each model generation
  the run made is stretched to `stretch_us`, 1.5 s by default and at most the delay
  limit, one per version of the case. The version is run and checked, and every speech
  and the hangup are moved against its run. 1.5 s is longer than the default
  interruption threshold, so a speech can interrupt while the write or the generation is
  under way. A delay already that long is left as it is. To keep these candidates few, a
  handle is put at each transition itself, with no offset, and the ends of played words
  are left out. Such a candidate is reported as
  `backend.latency 1, stretched + user.hangup 0`.
- `profiles=N` draws N provider-timing profiles from the case's seed and moves every
  speech under each. A profile redraws every delay the run applied, uniformly between 0
  and twice its value (at least 1 s, at most the delay limit), in steps of 10 ms. The
  same case and N give the same profiles.

Two times each moved to boundaries are not explored together.

The search uses everything in the ledger to decide where to look, the framework's own
events included. What it finds is decided by the checker alone.

### What a search reports

- **What broke.** Per predicate: how many candidates break it and what they changed, for
  example `speech.position 1`, `backend.error 1` or `backend.drop 1 + backend.wait 1`. A
  rule that breaks in the case as given and in every candidate that ran is marked
  `everywhere`: it does not depend on timing.
- **How wide.** Section 4.
- **What was reached.** For each handle, the kinds of transition it was put beside in a
  run that could be judged, and the kinds it never was, with the reason: no such case
  exists (`impossible`), the adapter refused it, the run raised, or the run was not valid
  evidence. A search that found nothing is read together with this list.
- **What was not judged.** The cases that were refused, raised or were not valid evidence,
  grouped by reason. Reasons that differ only in a number are one reason. Each has its
  count and one case as an example: what was changed, and what that put beside what.

  ```
  not judged: 2 of 78 cases
      2 error: RuntimeError: boom
          such as speech.position 1 at 2.500 s (as written 20.000 s), to put interrupt at llm.done
  ```

## 4. Windows

A failing candidate shows that a failure exists near a boundary. A **window** measures
how much room the failure has: along one variable, every value from `lo_us` to `hi_us`
breaks the predicate, with no passing run between.

Each end is found by doubling the step away from a breaking case until a run does not
break the rule, then halving back, to within a millisecond. A window narrower than a
millisecond is located again to the microsecond. Each end says why the search stopped:

| End | Meaning |
| --- | --- |
| `passes` | A run just beyond it does not break the rule. This is an edge of the failure |
| `blocked` | No case exists beyond it (a delay at zero, a speech against another), or none could be judged. The failure holds as far as the scenario can go |
| `reach` | The search gave up, 30 s out by default. The failure may hold further |

A window with no width, located to the microsecond, with a passing run on each side is an
**instant**. The run breaks the rule at one value and passes a microsecond either side.
The outcome depends on which of two same-instant events ran first, which nothing in the
scenario decides (`docs/contracts.md`, section 2.3). An instant is repeatable on a virtual
clock and says nothing about the system. A window of any width is a real failure.

## 5. The shrinker

A case found by a search is the author's scenario with changes. The shrinker finds which
of the changes the failure needs. After every step it runs the real system again, and
keeps the step only if the run still breaks the same predicate.

1. **Isolate.** List every difference between the found case and the author's scenario:
   a speech moved, lengthened, added or dropped; a hangup; a delay declared; a fault
   added; a setting, a contract policy or the seed changed. Delta debugging (Zeller and
   Hildebrandt's *ddmin*) over that list gives a smallest set of differences that still
   fails. The result is the author's scenario with only those changes. If the author's
   scenario fails as written, it is the author's scenario.
2. **Remove.** ddmin again, over the speeches, then the declared delays, then the faults
   that remain, the author's own included. Then the hangup, if the case still fails with
   a user who stays.
3. **Settle.** For each time that still differs from the author's, measure its window. If
   the failure has an edge along that time, put the time on the roundest value in the
   middle half of the window: a whole second if one is there, else half a second, a
   tenth, a twentieth, 10 ms, 1 ms. A time the failure does not depend on is left alone.
   A delay or a wait is not settled longer than the search's limit, 10 s by default: a
   window that runs past the limit is cut off there, and the time is put just inside the
   edge the window has below it. The window is still reported whole.

The result is 1-minimal: undoing any one remaining difference, or removing any one
remaining speech, declared delay or fault, makes it pass. It is not necessarily the smallest
failing case there is. A reduction can only undo differences, so of several failing
candidates the one to reduce is the one nearest the author's scenario
(`earful.core.shrink.nearest`).

Settling keeps the reduced case stable. A search finds failures at boundaries, which are
the edges of their windows. A case left on an edge stops failing after a small change to
the system or to Earful.

Every run is remembered, so no case is run twice. A budget (`max_runs`, 600 by default)
bounds the work; if it is spent, the case reduced so far is returned and marked
incomplete.

A reduction has two limits. It cannot reduce the scenario's code. It follows a rule and
knows nothing of the mechanism: if the same rule can be broken two ways, a reduction may
end at the other one. The bundle keeps what the checker said of the case as found and of
the case as reduced, so the two can be compared.

## 6. The bundle: `earful.bundle/1`

A bundle is what is kept when a failing case has been reduced. The file names are fixed.

| File | Contents |
| --- | --- |
| `manifest.json` | What failed, on what, and how the case was reduced |
| `original.case.json` | The case as it was found (`earful.case/1`) |
| `original.ledger.jsonl` | The normalized ledger of running it |
| `minimal.case.json` | The reduced case |
| `minimal.ledger.jsonl` | The normalized ledger of running it |

The manifest freezes the failure. Its shape is
`src/earful/core/schemas/earful.bundle-1.schema.json`.

| Field | Contents |
| --- | --- |
| `schema` | `earful.bundle/1` |
| `schema_minor` | Additive format revision; absent means 0 |
| `predicate` | `contract` and `rule`: the failure |
| `framework` | `name` and `version` of the release it was found on |
| `clock` | The clock the reduced case ran on |
| `formats` | The versions of the `ledger`, `contracts` and `case` formats the bundle is written in |
| `environment` | What it was made with, by name. The LiveKit adapter gives the framework, Earful, the interpreter, the platform, and a digest of every installed distribution and its version. For the record; a replay does not depend on it |
| `scenario_digest` | SHA-256 of the source of the one module that defines the scenario's function. Empty if that source could not be read. What it covers is set out under Replaying |
| `original`, `minimal` | Per case: the ledger `hash`, the `case_digest` of its case file, how many `speeches`, declared `delays` and `faults` it has, and the checker's `findings` for the frozen rule, in its own words |
| `reduction` | `runs`: how many times the system was run. `complete`: false if the budget was spent first. `steps`: what was done, in order. `differences`: every way the reduced case differs from the author's scenario |
| `windows` | For each speech of the reduced case, and each time the reduction settled: the `variable`, `lo_us`, `hi_us`, `width_us`, `lo_end`, `hi_end`, `resolution_us`, and whether it is an `instant` |
| `replay` | `repeats`: how many times the reduced case was run again when the bundle was made. `distinct_hashes`: how many different ledgers that gave, always 1 |

The seed and the contract policy are in the case files. A bundle holds no timestamp and
no path.

A bundle is written only for a failure that repeats. The reduction judged one run of the
reduced case, and that run broke the rule. The reduced case is then run `repeats` more
times, three by default, and each run has to give the ledger of the run that was judged.
The ledger written for the case as found has to be the one it was judged by, too. If any
run gives another ledger, nothing is written and the error says how many runs agreed.

**Reading** a bundle checks all five files before anything is done with it:

- the manifest is an `earful.bundle/1` manifest, and its `formats` name the formats this
  Earful reads;
- each ledger validates as `earful.ledger/1` and hashes to the `hash` the manifest
  records for it;
- each case file validates as `earful.case/1` and has the `case_digest` the manifest
  records for it.

`case_digest` is SHA-256 of the case as one line of JSON: keys sorted, `,` and `:` as the
separators, characters outside ASCII escaped. It is computed from the parsed case, so a
case file laid out another way still matches.

A bundle that fails a check is refused with one line that names the file. `earful replay`,
`earful crosscheck` and `earful report` print it and exit 2:

```text
earful: failures/lk-1.8.12.booking.A2-rule/minimal.case.json is not the case the manifest records: its digest is 5d0c…, and the manifest has e41a…
```

`case_digest` was added in `schema_minor` 1. A bundle written before it is still read. Its
case files cannot be checked, and a replay says so.

The files are checked against the manifest, and nothing checks the manifest. The checks
find a file that was edited, damaged or left out. They do not make a bundle from an
unknown source safe to replay.

**Replaying** runs the reduced case again, three times by default, and says:

- **judged**: every run was valid evidence (`docs/ledger.md`, Validity). A replay with a
  run that was not shows nothing about the rule, and `invalid` gives the reasons.
- **reproduced**: every run was judged and broke the frozen predicate.
- **clean**: every run was judged and none broke it.
- **repeatable**: every run gave the same ledger.
- **identical**: that ledger is the one recorded. Expected only on the release, and with
  the Earful version and scenario code, the bundle was made with.
- **scenario code changed**: the digest of the scenario's module differs from the one
  recorded. The bundle then tests something other than what was found.
- **unverified**: the case files the manifest records no digest of. Empty unless the
  bundle was written before `case_digest`.

On the release it was found on, a bundle is reproduced, repeatable and identical, on any
platform. On a release that repaired the defect it is clean and repeatable. That pair is
the regression test.

A replay can be neither reproduced nor clean: a run was not valid evidence, or some runs
broke the rule and others did not. "Not reproduced" therefore does not mean the defect is
gone. A test of a fix asserts `clean`, and `earful replay --expect clean` exits 0 only for
a clean replay. A replay that was not judged, or whose runs differ, exits 3 whatever was
expected. A replay makes at least one run: `repeats` below 1 is a `ValueError`.

`earful replay` also prints the Earful version that kept the bundle, which the manifest
records in `environment`, when it is not the version replaying it. It names any case file
it could not verify.

**What the scenario's digest covers.** A case names its code as `module:function`.
`scenario_digest` is SHA-256 of the source text of one file: the module in which that
function is defined. Any change to that file changes it, a comment included. It does not
cover:

- a module the scenario imports, such as the agent, its tools or a stand-in for a
  service;
- the framework, its dependencies and Earful. The manifest's `environment` records those,
  and a replay prints the framework release and compares the Earful version;
- anything the code reads when it runs: files, environment variables.

"Scenario code changed: no" therefore means that one file is as it was when the bundle
was kept. An agent defined in another module can change without the replay saying so. To
have it covered, define the agent in the scenario's module. A digest that is empty, because
the source could not be read when the bundle was kept, is reported as `not known`.

**Trust.** A bundle names the code of its scenario and does not contain it. Replaying one
imports and runs that code, so replay only bundles you trust.

`tests/bundles/` holds two reference bundles, made on livekit-agents 1.5.6: the lost tool
result (`A2/rule`) and the reply that starts over a new turn (`L2/rule b`). Every
supported release replays both, in the tests and in CI. `scripts/check_all.sh --update` is
the one way they change.

## 7. Limits

- **Mostly one thing at a time.** A candidate moves one variable or adds one fault.
  With `pairs` it combines a fault or a stretched delay with a moved speech or hangup, or
  two faults. With `profiles` it moves a speech under redrawn delays. A stretched delay
  has one value, and only writes and generations are stretched. Two times each moved to
  boundaries are not explored together, and neither are three changes of any kind.
- **The boundaries are those of one run.** Once a moved event falls somewhere new it
  changes what follows, so a handle aimed at a later transition may meet a timeline that
  has moved. The search chooses where to look. It does not prove that every ordering was
  tried.
- **A failure with no boundary beside it is not looked for.** A window that opens and
  closes between two transitions, more than 10 ms from each, has no candidate inside it.
  Stepping a variable in fixed increments (`scripts/contracts_sweep.py`) covers those,
  and found nothing the boundary search missed on the supported releases
  (`docs/gate1-core.md`).
- **A window is one stretch.** It is measured outwards from one breaking case until a run
  passes. A failure that stops and starts again is several windows, found from several
  candidates.
- **The reduced case is 1-minimal.** A smaller failing case may exist.
- **Faults are the backend's, and the user can leave.** A write can fail, time out or be
  lost, and the user can hang up once their last speech is over. A hangup in the middle
  of a speech, a provider that stalls and a caller that sends a write twice cannot be
  declared yet.
