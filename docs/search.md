# Search, reduction and replay

Status: **implemented**, 16 November 2026, in `src/earful/core/` (`case.py`, `search.py`,
`shrink.py`, `bundle.py`, `predicate.py`).

The checker says whether one run broke a contract (`docs/contracts.md`). This document
covers what surrounds it: how Earful looks for the timing at which a scenario fails, how
wide the failure is, how a failing case is reduced to the smallest one that fails the same
way, and what is kept so that anyone can run that case again.

```python
from earful.core import bundle, search
from earful.core.predicate import Predicate
from earful.core.shrink import nearest
from earful.livekit import scenario

author = scenario.default("earful.livekit.cases:book_then_interrupt")   # earful.case/1
found = search.explore(author, scenario.run)       # every time, to every boundary
found.predicates()                                 # the rules some run broke
found.windows(Predicate("A2", "rule"))             # how wide each failure is
found.coverage()                                   # which boundaries were reached, and which were not
case = nearest(author, [t.candidate.case for t in found.breaking(Predicate("A2", "rule"))])
bundle.capture(directory, case, scenario.run, Predicate("A2", "rule"), author=author)
```

```bash
python scripts/search.py earful.livekit.cases:book_then_interrupt --keep A2/rule --out out
python scripts/replay.py out/lk-1.5.6.book_then_interrupt.A2-rule
```

All of it is framework-independent. The one thing an adapter supplies is `run`: a
function from a case to the ledger of running it. For LiveKit that is
`earful.livekit.scenario.run`.

## 1. A case is data: `earful.case/1`

A scenario has two parts. Its code, the agent and the model policy, stays in the module it
is written in and is named by a reference. Everything else is data. Because that part is
plain JSON, a case can be moved in time, reduced, written to a file and run again later
without touching code.

| Field | Contents |
| --- | --- |
| `schema` | `earful.case/1` |
| `scenario` | Where the code lives, as `module:function`. The function takes no arguments and returns the scenario with its default speeches and settings |
| `seed` | Seed for every random choice in the run |
| `speeches` | The scripted user's speeches, in order of their start. Per speech: `start_us`, `end_us`, `transcript` (empty for noise), `stt_delay_us`, `expects_reply` (null: yes, unless noise or a backchannel), `backchannel`, `retires` |
| `delays` | Provider delays declared as data, each in place of what the scenario's code would ask for. Per delay: `of` (`llm.ttft`, `llm.duration`, `tts.ttfb` or `backend.latency`), `nth` (which model call, utterance or backend request, from 1) and `us` |
| `settings` | Timing and provider settings, by name. Times are integer microseconds |
| `contracts` | Contract id to the policy values declared for it, as in the ledger header |

The shape is a JSON Schema document shipped with the package
(`src/earful/core/schemas/earful.case-1.schema.json`). Speeches must be in order, end
after they start and not overlap; a delay is declared at most once.

Which settings exist is the adapter's business. The LiveKit adapter has
`seconds_per_letter_us`, `tts_ttfb_us`, `vad_min_speech_us`, `vad_min_silence_us`,
`min_interruption_duration_us`, `false_interruption_timeout_us`,
`min_endpointing_delay_us`, `max_endpointing_delay_us`, `drain_delay_us`,
`drain_timeout_us`, `backend_mode` and `sink_can_pause`. It refuses any other name and
any value of the wrong kind. `scenario.default(reference)` gives the case as its author
wrote it; `scenario.describe(case, reference)` gives the data of a `Case` built in Python;
and running a case from its data gives the same ledger, byte for byte, as running it
directly.

### Variables

Every time in a case is a **variable**: something that can be moved on its own.

| Variable | What moving it does |
| --- | --- |
| `speech.position` | Moves a whole speech, its length kept |
| `speech.end` | Moves the end of a speech, so its length |
| `speech.stt_delay` | Changes how long its transcript takes |
| `llm.ttft`, `llm.duration` | The time to first token, or the total generation time, of one model call |
| `tts.ttfb` | The time to first audio of one synthesized utterance |
| `backend.latency` | The time one backend write takes to commit |

A speech variable is named with the speech's index, a delay with its occurrence:
`speech.position 1`, `backend.latency 2`. A delay the case does not declare is still a
variable. Its value is whatever the scenario's code asked for, which the ledger records,
and moving it declares it.

Some values give no case: a negative time, a speech that would end before it starts or
overlap another. Speeches may be as close together as they like. A VAD hears speech that
resumes within its silence window as one segment, and so does the scripted one
(`docs/ledger.md`).

## 2. The same failure: a predicate

A failure is named by the contract and the rule of it that broke: `A2/rule`,
`L2/rule b`, `L1/milestone 2`. The instance (which speech, which reply, which context) is
left out on purpose. A search moves speeches and a reduction removes them, so instances
are renumbered while the failure stays the same failure.

A run **breaks a predicate** when its report has a failed verdict of that contract with a
finding of that rule. Three kinds of run break nothing, because nothing can be concluded
from them: a run that is not valid evidence, a case the adapter refuses, and a run that
raises. Each is counted and reported; none ends a search.

## 3. The boundary scheduler

A timing defect lives at a boundary: the user interrupts as the first audio arrives, a
tool returns as the turn is being committed. Stepping one thing through time in fixed
increments finds such a place only if a step happens to land on it. The scheduler asks
the ledger where the boundaries are and goes to them.

**Transitions.** Run the case. Every instant at which the run recorded an event is a
transition. So are the instants the ledger states and no event marks: the model's first
token and its last, the voice's first audio, the middle of a word, the end of every played
word, and the moment a user turn can be committed.

**Handles.** Moving a variable moves one or more handles, the moments at which it acts on
the system. They follow from what the run declared, never from what the framework did.

| Handle | When, and which variables move it |
| --- | --- |
| `vad_start` | The VAD tells the framework the user is speaking: the speech's start plus `min_speech_us`. Moved by `speech.position` |
| `interrupt` | The speech has lasted long enough to interrupt: its start plus `min_interruption_duration_us`. Absent for a shorter speech. Moved by `speech.position` |
| `transcript` | The final transcript arrives: its end plus `stt_delay_us`. Absent for noise. Moved by `speech.position`, `speech.end` and `speech.stt_delay` |
| `vad_end` | The VAD tells the framework the user stopped: its end plus `min_silence_us`. Moved by `speech.position` and `speech.end` |
| `turn_end` | The turn can be committed: its end plus the longer of `min_silence_us` and `min_endpointing_delay_us`. Moved by `speech.position` and `speech.end` |
| `done` | A provider delay runs out: the model's first token or last, the first audio, the commit. Moved by that delay |

**Candidates.** For each handle, each transition and each offset, change the handle's
variable so that the handle falls there. The offsets are 10 ms before, exactly at, and
10 ms after: one audio frame, the finest regular step at which a framework is given audio.
Cases that coincide are one candidate. Each candidate is run and checked.

A delay is placed against the transitions of the case as it is. A speech is placed against
the transitions of the run without that speech: the timeline it would interrupt. No delay
is made longer than a limit, 10 s by default. A model that answers after a minute breaks
things too, but that is another scenario, not a nearby ordering of this one.

By default the search moves where every speech sits and every provider delay.
`speech.end` and `speech.stt_delay` can be added, and the search can be limited to some
speeches, some handles, or the boundaries other than word ends.

The search uses everything in the ledger to decide where to look, the framework's own
events included. What it finds is decided by the checker alone.

### What a search reports

- **What broke.** Per predicate: how many candidates break it and which variables they
  moved. A rule that breaks in the case as given and in every candidate that ran is
  marked `everywhere`: it is not about timing.
- **How wide.** Section 4.
- **What was reached.** For each handle, the kinds of transition it was put beside in a
  run that could be judged, and the kinds it never was, with the reason: no such case
  exists (`impossible`), the adapter refused it, the run raised, or the run was not valid
  evidence. "Nothing found" means something only next to this.

## 4. Windows

A candidate that fails says a failure exists near a boundary. It does not say how much
room the failure has. A **window** does: along one variable, every value from `lo_us` to
`hi_us` breaks the predicate, with no passing run between.

Each end is found by doubling the step away from a breaking case until a run does not
break the rule, then halving back, to within a millisecond. A window narrower than a
millisecond is located again to the microsecond. Each end says why the search stopped:

| End | Meaning |
| --- | --- |
| `passes` | A run just beyond it does not break the rule. This is an edge of the failure |
| `blocked` | No case exists beyond it (a delay at zero, a speech against another), or none could be judged. The failure holds as far as the scenario can go |
| `reach` | The search gave up, 30 s out by default. The failure may hold further |

A window with no width, located to the microsecond, with a passing run on each side is an
**instant**. The run breaks the rule at one value and not a microsecond either side: it
turns on which of two same-instant events ran first, which nothing in the scenario decides
(`docs/contracts.md`, section 2.3). It is repeatable on a virtual clock and says nothing
about the system. Any wider window, however narrow, is a real one.

## 5. The shrinker

A case found by a search is the scenario its author wrote, changed. The shrinker asks
which of the changes the failure needs. After every step it runs the real system again,
and keeps the step only if the run still breaks the same predicate.

1. **Isolate.** List every difference between the found case and the author's scenario:
   a speech moved, lengthened, added or dropped; a delay declared; a setting, a contract
   policy or the seed changed. Delta debugging (Zeller and Hildebrandt's *ddmin*) over
   that list gives a smallest set of differences that still fails. The result is the
   author's scenario with only those changes. If the author's scenario fails as written,
   it is the author's scenario.
2. **Remove.** ddmin again, over the speeches and then the declared delays that remain,
   the author's own included.
3. **Settle.** For each time that still differs from the author's, measure its window. If
   the failure has an edge along that time, put the time on the roundest value in the
   middle half of the window: a whole second if one is there, else half a second, a
   tenth, a twentieth, 10 ms, 1 ms. A time the failure does not depend on is left alone.

The result is 1-minimal: undoing any one remaining difference, or removing any one
remaining speech or declared delay, makes it pass. It is not necessarily the smallest
failing case there is. A reduction can only undo differences, so of several failing
candidates the one to reduce is the one nearest the author's scenario
(`earful.core.shrink.nearest`).

Settling is what makes the result worth keeping. A search finds failures at boundaries,
which is to say at the edges of their windows, and a case left on an edge stops failing at
the smallest change to the system or to Earful, for no reason anyone could read from it.

Every run is remembered, so no case is run twice. A budget (`max_runs`, 600 by default)
bounds the work; if it is spent, the case reduced so far is returned and marked
incomplete.

Two things a reduction cannot promise. It cannot reduce the scenario's code. And it
follows a rule, not a mechanism: if the same rule can be broken two ways, a reduction may
end at the other one. The bundle keeps what the checker said of the case as found and of
the case as reduced, side by side, so that this can be seen.

## 6. The bundle: `earful.bundle/1`

What is kept when a failing case has been reduced. The file names are fixed.

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
| `predicate` | `contract` and `rule`: the failure |
| `framework` | `name` and `version` of the release it was found on |
| `clock` | The clock the reduced case ran on |
| `formats` | The versions of the `ledger`, `contracts` and `case` formats the bundle is written in |
| `environment` | What it was made with, by name. The LiveKit adapter gives the framework, Earful, the interpreter, the platform, and a digest of every installed distribution and its version. For the record; a replay does not depend on it |
| `scenario_digest` | A digest of the code of the scenario the cases name |
| `original`, `minimal` | Per case: the ledger `hash`, how many `speeches` and declared `delays` it has, and the checker's `findings` for the frozen rule, in its own words |
| `reduction` | `runs`: how many times the system was run. `complete`: false if the budget was spent first. `steps`: what was done, in order. `differences`: every way the reduced case differs from the author's scenario |
| `windows` | For each speech of the reduced case, and each time the reduction settled: the `variable`, `lo_us`, `hi_us`, `width_us`, `lo_end`, `hi_end`, `resolution_us`, and whether it is an `instant` |
| `replay` | `repeats`: how many times the reduced case was run when the bundle was made. `distinct_hashes`: how many different ledgers that gave, always 1 |

The seed and the contract policy are in the case files. A bundle holds no timestamp and
no path. A reduced case that does not give the same ledger every time is not written at
all.

**Replaying** runs the reduced case again and says:

- **reproduced**: every run broke the frozen predicate.
- **repeatable**: every run gave the same ledger.
- **identical**: that ledger is the one recorded. Expected only on the release, and with
  the Earful version and scenario code, the bundle was made with.
- **scenario code changed**: the digest of the scenario's code differs from the one
  recorded. The bundle then tests something other than what was found.

On the release it was found on, a bundle is reproduced, repeatable and identical, on any
platform. On a release that repaired the defect it is repeatable and not reproduced. That
pair is the regression test.

**Trust.** A bundle names the code of its scenario; it does not contain it. Replaying one
imports and runs that code. Replay only bundles you trust, as you would only run test code
you trust.

`tests/bundles/` holds two reference bundles, made on livekit-agents 1.5.6: the lost tool
result (`A2/rule`) and the reply that starts over a new turn (`L2/rule b`). Every
supported release replays both, in the tests and in CI. `scripts/check_all.sh --update` is
the one way they change.

## 7. Limits

- **One variable at a time.** A candidate moves one variable of the case as given. Pairs
  of moved variables are not explored.
- **The boundaries are those of one run.** Once a moved event falls somewhere new it
  changes what follows, so a handle aimed at a later transition may meet a timeline that
  has moved. The search is a way of choosing where to look, not a proof that every
  ordering was tried.
- **A failure with no boundary beside it is not looked for.** A window that opens and
  closes between two transitions, more than 10 ms from each, has no candidate inside it.
  Stepping a variable in fixed increments (`scripts/contracts_sweep.py`) is the
  complement, and found nothing the boundary search missed on the supported releases
  (`docs/gate1-core.md`).
- **A window is one stretch.** It is measured outwards from one breaking case until a run
  passes. A failure that stops and starts again is several windows, found from several
  candidates.
- **The reduced case is 1-minimal**, not the global minimum.
- **No faults.** Delays move; nothing fails, times out or disconnects.
