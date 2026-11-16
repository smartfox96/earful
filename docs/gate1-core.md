# Gate 1: is the core complete?

Status: **passed.** 16 November 2026.

Gate 0 showed that a real LiveKit `AgentSession` can be run deterministically
(`docs/gate0-feasibility.md`). Gate 1 asks whether the core that sits on top of that is
whole: a record of what happened, a checker that judges it, a way to look for the timing
at which a scenario fails, and a way to reduce a failure to something worth keeping.

## Exit criteria

Gate 1 is complete only when each of these is demonstrated.

| Criterion | Where it is shown |
| --- | --- |
| The ledger is versioned and defined by a published schema | `docs/ledger.md`; `earful.ledger/3` |
| The checker reads only the ledger and imports no framework | `docs/contracts.md`; `tests/test_core_boundary.py` |
| Tools write to a deterministic backend that records each write apart from what the tool returns | `src/earful/core/backend.py`; `effect.*` events |
| Every contract detects a deliberate break, and passes the same scenario without it | Mutations and controls, below |
| A search places the events a scenario controls before, at and after the moments the system changes state, reaches the boundaries that matter, and says which it did not reach | The boundary scheduler, below |
| It finds a deliberate break from a scenario that passes as written, however narrow, and tells a real failure from a same-instant tie | The same |
| A failing case is reduced by re-running the real framework, and the reduced case replays to the same ledger every time | Reduction and replay, below |

Everything below is reproduced by `scripts/check_all.sh`, which builds the five pinned
environments and runs the tests, the repeatability check, the historical cases and the
replay of the reference bundles on each, side by side.

## Mutations and controls

Each contract has a deliberate break and a clean control
(`tests/test_contract_breaks.py`). The break is reached through a seam any application
has; nothing inside the framework is patched, and the checker sees only the ledger. The
same checker, with no special cases, fails the break and passes the control on all five
releases.

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

The figures are the same on all five releases except where noted. The second row is what
the boundary search is for: a fixed step of 50 ms lands inside 30 ms by luck, and the
search goes to the end of the word and finds it there and 10 ms after. The third row is
what the window is for. A candidate that fails exactly at a boundary may be a real
failure with little room or a tie between two events; only measuring tells them apart.

### The boundaries that matter are reached

In the reference booking scenario the tool is called and commits half a second after the
user stops speaking, as the turn is committed. A user's next speech can be put there
because a VAD hears speech that resumes that soon as one segment, and so does the
scripted one. The commit can be put anywhere after its request because a backend latency
can be declared as data. `tests/test_search_live.py` asserts that the user's VAD report
and interruption point reach `tool.called`, `tool.returned`, `effect.requested` and
`effect.committed`, and that the commit reaches the playout, the sink's pause and the
user's transcript.

A search of that scenario with every default variable runs 667 to 1 230 candidates,
depending on the release, with none refused, none raising and none invalid. A further 889
to 1 439 placements are counted as impossible and not run: a delay cannot be negative, and
a speech cannot overlap or pass another. It takes 10 to 20 seconds on eight cores.

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

### The search against stepping through time

`scripts/contracts_sweep.py` steps one speech through 23 scenario shapes in 50 ms
increments, 3 441 runs per release (`docs/contracts.md`, section 10). On 22 of those
shapes, on releases 1.5.6, 1.6.6 and 1.8.12, the boundary search with its default
variables was compared with the sweep: 14 847 candidates per release. Every rule the
sweep found broken in a shape, the search found broken in that shape. The comparison is
one of recall. It cannot show that the search finds what neither method looks for, and
`docs/search.md` section 7 says what that is.

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
| `lk-1.5.6.book_then_interrupt.A2-rule` | `A2/rule` | 77 | first: 0 to 2.5 s; second: 3.000 to 7.869 s | 1 ledger, the one recorded |
| `lk-1.5.6.speak_over_new_turn.L2-rule-b` | `L2/rule b` | 60 | first: 0.251 to 0.699 s; second: 3.001 to 3.449 s | 1 ledger, the one recorded |

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

## The tests have teeth

Thirty-three parts of the search, the shrinker, the bundle and what they rest on were
each broken in turn: the offsets ignored, the delay limit removed, a window's ends
mislabelled, a reduction kept without the failure it needs, a bundle written for a case
that does not repeat, the VAD never merging, L1 naming one rule for every milestone. Each
broken version fails at least one test.

## Limits

Stated in full in `docs/search.md` section 7 and `docs/contracts.md`. The ones that bear
on this gate:

- **One variable at a time.** Pairs of moved events are not explored.
- **A failure with no boundary beside it is not looked for.** Stepping through time is
  the complement.
- **No faults.** Delays move; the backend commits every write. A write that fails or
  times out is not recorded and cannot be injected.
- **Scripts, not a package surface.** There is no command-line tool, pytest plugin or
  report yet.
