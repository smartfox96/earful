# Gate 1: is the core complete?

Status: **passed.** 16 November 2026.

Gate 0 showed that a real LiveKit `AgentSession` can be run deterministically
(`docs/gate0-feasibility.md`). Gate 1 covers the core built on that: the ledger, the
checker, the search for the timings and faults at which a scenario fails, and the
reduction of a failing case to one that is kept and replayed.

## Exit criteria

Gate 1 is complete when each of these is demonstrated.

| Criterion | Where it is shown |
| --- | --- |
| The ledger is versioned and defined by a published schema | `docs/ledger.md`; `earful.ledger/5` |
| The checker reads only the ledger and imports no framework | `docs/contracts.md`; `tests/test_core_boundary.py` |
| Tools write to a deterministic backend that records each write apart from what the tool returns | `src/earful/core/backend.py`; `effect.*` events |
| Every contract detects a deliberate break, and passes the same scenario without it | Mutations and controls, below |
| A search places the events a scenario controls before, at and after the moments the system changes state, reaches the boundaries that matter, and says which it did not reach | The boundary scheduler, below |
| It finds a deliberate break from a scenario that passes as written, however narrow, and tells a real failure from a same-instant tie | The same |
| A failing case is reduced by re-running the real framework, and the reduced case replays to the same ledger every time | Reduction and replay, below |

`scripts/check_all.sh` builds the five pinned environments and runs the tests, the
repeatability check, the historical cases and the replay of the reference bundles on
each, side by side. The figures in the tables below are measured by
`scripts/gate1_figures.py`, which prints them for the release it is run with:

```bash
.venvs/lk-1.5.6/bin/python scripts/gate1_figures.py
```

Two sets of figures come from elsewhere. Those of the reference bundles are recorded in
their manifests (`tests/bundles/`). The comparison with stepping through time is made by
`scripts/search_vs_sweep.py`.

## Mutations and controls

Each contract has a deliberate break and a clean control
(`tests/test_contract_breaks.py`). The break is made in the application's own code: the
model policy, a tool, the backend, an agent hook, a session option or the sink. Nothing
inside the framework is patched, and the checker sees only the ledger. The same checker
fails the break and passes the control on all five releases.

| Contract | Break | Control |
| --- | --- | --- |
| A1 | The model calls the tool again after the user corrected the intent | A model that honours the correction |
| A2 | A tool that writes twice for one call, on a plain backend | The same tool on a keyed backend |
| A3 | Success is spoken while the backend is still committing | The claim made after the tool result |
| M1 | An output that begins playing 2.5 s after it says it has | The unmodified output |
| M2 | An agent hook that drops tool results from the context; one that repeats a user turn | The unmodified agent |
| L1 | A tool that never returns; a model that says nothing | The same tool returning; a turn that expects no reply |
| L2 | An output that keeps playing 400 ms after it is told to stop; an agent that cannot be interrupted | The unmodified output; a policy that says interruptions are not allowed |

`tests/test_checker.py` edits recorded ledgers by hand to reach the rules a live run does
not.

## The boundary scheduler

`docs/search.md` sections 3 and 4 describe it. Each time a scenario controls is moved so
that the moments at which it acts fall 10 ms before, exactly at and 10 ms after every
transition of the run. Where a rule breaks, the window in which it breaks is measured.

### A deliberate break, found from a scenario that passes as written

The scenario (`tests/breaks.py`) is a long reply and a user who says "Stop." only after it
is over. As written it passes every contract, with the break and without. The search moves
"Stop." so that its interruption point falls on the boundaries of the reply: the first
audio, the end of every word, the last word (`tests/test_search_live.py`).

| Break | Candidates run | Breaking `L2/rule a` | Window measured | Control: breaking |
| --- | --- | --- | --- | --- |
| An output slow to stop whenever it is told to | 99 | 84 (87 on 1.5.6) | 6.9 s wide (7.1 s on 1.5.6), a passing run met on each side | 0 |
| The same, only in the 30 ms after the seventh word ends | 99 | 2 | 29 ms wide, located to a millisecond | 0 |
| The same, only at the very microsecond that word ends | 99 | 1 | an instant: no width, a passing run a microsecond either side | 0 |

The figures are the same on all five releases except where noted. In the second row a
fixed step of 50 ms would land inside the 30 ms only by luck. The search puts "Stop." at
the end of the word and 10 ms after it, and both candidates fail. In the third row the one
failing candidate has a passing run a microsecond either side of it, so the search
reports an instant: a tie between two events, with no timing room.

### The boundaries that matter are reached

In the reference booking scenario the tool is called and commits half a second after the
user stops speaking, as the turn is committed. A user's next speech can be put there
because a VAD hears speech that resumes that soon as one segment, and so does the
scripted one. The commit can be put anywhere after its request because a backend latency
can be declared as data. `tests/test_search_live.py` asserts that the user's VAD report
and interruption point reach `tool.called`, `tool.returned`, `effect.requested` and
`effect.committed`, and that the commit reaches the playout, the sink's pause and the
user's transcript.

A search of that scenario with every default variable runs 700 to 1 330 candidates,
depending on the release, with none refused, none raising and none invalid. A further 946
to 1 496 placements are counted as impossible and not run: a delay cannot be negative, a
speech cannot overlap or pass another, and the user cannot hang up before their last
speech is over. It takes 4 to 12 seconds on fifteen cores.

### The two known defects are found on the releases that have them and on no other

Each reference case is searched from a starting point that passes on every release: its
second speech moved to long after everything. The search moves that speech back, with
every one of its handles, to every boundary.

| livekit-agents | `book_then_interrupt`: candidates | breaking `A2/rule` and `M2/rule d` | window of the second speech's start | `speak_over_new_turn`: candidates | breaking `L2/rule b` | window |
| --- | --- | --- | --- | --- | --- | --- |
| 1.5.6 | 291 | 189 | 3.000 to 7.869 s | 322 | 14 | 3.001 to 3.449 s |
| 1.5.7 | 291 | 189 | 3.000 to 7.869 s | 322 | 0 | |
| 1.6.5 | 291 | 189 | 3.000 to 7.869 s | 322 | 0 | |
| 1.6.6 | 291 | 0 | | 322 | 0 | |
| 1.8.12 | 291 | 0 | | 322 | 0 | |

The first defect has a window nearly five seconds wide: any interruption of the reply
that called the tool. The second has one of 448 ms: the user must begin speaking after
the previous turn is committed and before the reply to it becomes audible.

### Faults

`docs/search.md` section 3 describes them. The search adds an error, a timeout and a
dropped write to each backend write of a run, and moves the moment the caller gives up
and the commit of a timed-out write to every transition.

Four scenarios (`tests/breaks.py`) ask for one booking. Each passes as written, where
every write commits. Two are controls.

| Model and tool | Candidates run | Found | Under which fault |
| --- | --- | --- | --- |
| Confirms the booking whatever the tool returned | 360 | `A3/rule` in 92 | A rejected write; a dropped write; a timeout whose write commits after the claim is heard |
| Control: confirms only a result that is not an error | 396 | nothing | |
| Calls the tool again after an error; the tool sends no key | 488 | `A2/rule` in 366 | A timeout in either order |
| Control: the same model, a tool that sends a key, a keyed backend | 488 | nothing | |

The figures are the same on all five releases, and every candidate was judged. In the
first row the search also measures where the timeout starts to matter: the confirmation
is false once the write takes longer than 6.890 s, which is when its commit falls after
the last word of "Your table is booked." In the third row a rejected or dropped write is
not reported, because sending it again writes once.

A case found with a dropped write and a moved wait reduces to the author's scenario plus
the one fault, and replays to the same ledger (`tests/test_search_live.py`).

### Hangup

`docs/search.md` section 1 describes it. The search makes the user leave at every
transition from the end of their last speech on: `user.hangup` is one of the default
variables. The session is closed at that moment, as a participant's disconnect closes it.

On all five releases the framework stops the output at the instant of the hangup, lets a
tool that is running finish, and then closes. A write requested before the hangup commits
after it.

Two scenarios (`tests/breaks.py`) ask for one booking. The agent books again when the
session ends unless it had told the user the table is booked. Each passes as written,
where the user stays to hear it.

| Agent | Candidates run | Found | Window of the hangup |
| --- | --- | --- | --- |
| Books again on exit; its writes carry no key | 98 | `A2/rule` in 86 | 3.001 to 9.900 s |
| Control: the same agent sends a key, to a keyed backend | 98 | no `A2/rule` | |

The figures are the same on all five releases, and every candidate was judged. The window
runs from the tool being called to the end of "Your table is booked." The found case
reduces in 38 runs to the author's scenario with the user hanging up at 6.000 s, and
replays to the same ledger (`tests/test_search_live.py`).

A hangup interrupts the reply that called the tool, so on the releases before 1.6.6 these
searches also report the first known defect as `M2/rule d`. A failure of a rule that has
not been classified is not reported here (`docs/contracts.md`, section 10).

### The search against stepping through time

`scripts/contracts_sweep.py` steps one speech through 23 scenario shapes in 50 ms
increments, 3 441 runs per release (`docs/contracts.md`, section 10).
`scripts/search_vs_sweep.py` makes those runs and then searches each shape. The search
starts with the moving speech at the last position of its sweep and moves where each
speech sits and every provider delay: 18 827 candidates on 1.5.6 and 18 716 on each later
release.

```bash
.venvs/lk-1.5.6/bin/python scripts/search_vs_sweep.py
```

On all five releases, every rule the sweep found broken in a shape, the search found
broken in that shape. This compares recall only. What neither method looks for is listed
in `docs/search.md` section 7.

As in `docs/contracts.md` section 10, a failure of a rule that has not been classified is
not reported here.

## Reduction and replay

`docs/search.md` sections 5 and 6 describe the shrinker and the bundle.

**A found case comes back to the author's scenario with the one change it needs.** The
case the search found for the first break above, with a noise, a late remark, two changed
settings, a looser contract policy, a declared delay and another seed added, differs from
the scenario as written in eight ways. The shrinker keeps one: "Stop." moved. It then
measures the window of that move and leaves "Stop." on a whole second in the middle of
it, where the case still fails if "Stop." shifts by 50 ms either way. Removing either of
the two remaining speeches makes the case pass. Every step was kept only after the real
framework had been run again and had broken the same rule.

**A scenario that fails as written reduces to itself**, however far the found case had
moved from it.

**The reduced case replays to the same ledger.** `tests/bundles/` holds the two known
defects, searched for and reduced on livekit-agents 1.5.6. Both reference cases fail on
that release as their author wrote them, so each reduction is the scenario as written,
with the windows of its speeches measured.

| Bundle | Predicate | Runs to reduce and measure | How far each speech can move and still fail | Replayed 100 times on 1.5.6 |
| --- | --- | --- | --- | --- |
| `lk-1.5.6.book_then_interrupt.A2-rule` | `A2/rule` | 75 | first: 0 to 2.5 s; second: 3.000 to 7.869 s | 1 ledger, the one recorded |
| `lk-1.5.6.speak_over_new_turn.L2-rule-b` | `L2/rule b` | 57 | first: 0.251 to 0.699 s; second: 3.001 to 3.449 s | 1 ledger, the one recorded |

Neither sits on an instant, and each speech is at least 199 ms inside its window.

Replayed on every supported release, the same two cases are red where the defect is and
green where it was repaired:

| livekit-agents | `A2/rule` | `L2/rule b` |
| --- | --- | --- |
| 1.5.6 | reproduced, identical | reproduced, identical |
| 1.5.7 | reproduced | not reproduced |
| 1.6.5 | reproduced | not reproduced |
| 1.6.6 | not reproduced | not reproduced |
| 1.8.12 | not reproduced | not reproduced |

Every replay is repeatable, and none reports that the scenario's code has changed. The
tests assert the table above on whichever release they run on, and that a replay on 1.5.6
gives the recorded hash. CI runs them, and replays both bundles, in every job: Linux and
macOS, five releases, Python 3.10 to 3.14.

```bash
python scripts/replay.py tests/bundles/lk-1.5.6.book_then_interrupt.A2-rule
```

## Mutation checks

`tests/mutants.py` lists 123 deliberate breaks of the code. Each is one piece of source
text and what replaces it: the offsets of a search ignored, the delay limit removed, a
window's ends mislabelled, a reduction kept without the failure it needs, a timed-out
write that never commits, a hangup that excuses a turn already overdue.
`scripts/mutate.py` runs the unbroken code as a control, then applies each break in a
copy of the repository and runs the tests there.

```bash
.venvs/lk-1.8.12/bin/python scripts/mutate.py
```

On livekit-agents 1.8.12 the control passes and each of the 123 broken versions fails at
least one test. The run takes about 20 minutes with five copies side by side and is not
part of CI. The lint job does check, on every change, that the text of each break is
still found exactly once in its file (`scripts/mutate.py --verify`), so a change that
moves the code under a break fails until the break is brought along.

## Limits

Stated in full in `docs/search.md` section 7 and `docs/contracts.md`. The ones that bear
on this gate:

- **Two moved times at once are not explored.** A fault can be combined with a moved
  speech, and speeches can be moved under redrawn provider delays (`docs/search.md`,
  section 3).
- **A failure with no boundary beside it is not looked for.** Stepping through time
  (`scripts/contracts_sweep.py`) covers those.
- **Faults are the backend's, and the user can leave.** A write can fail, time out or be
  lost, and the user can hang up once their last speech is over. A hangup in the middle
  of a speech and a provider that stalls cannot be declared.
- **There is no package surface yet.** The search, the replay and the checks are
  scripts. There is no command-line tool, pytest plugin or report.
