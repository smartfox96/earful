# Gate 2: do known defects show up, and go away where they were fixed?

Status: **passed.** 16 November 2026.

Gate 1 showed that the checker catches deliberate breaks (`docs/gate1-core.md`). Gate 2
tests it against two defects that LiveKit users reported and the maintainers fixed. Each
must fail on the releases that have it and pass on the releases that fixed it, with one
checker and no code written for the defect.

## Exit criteria

| Criterion | Where it is shown |
| --- | --- |
| Two independently reported defects fail on affected releases | The case pages below; `tests/test_historical_cases.py` |
| Both pass on releases that contain the fix | The same |
| The checker has no code for either defect | `src/earful/core/` contains no reference to either issue; the cases are in `src/earful/livekit/cases.py` |
| Each case can be run by someone else from pinned inputs | The commands on each case page; `compat/` for the dependency sets; `tests/bundles/` for the cases and recorded ledgers |
| Each case is classified, with its limits stated | Each case page |

## The cases

| Case | Contract rule | Fails on | Passes on |
| --- | --- | --- | --- |
| [#3702: a tool result is lost when the reply is interrupted](cases/3702-tool-result-lost-on-interruption.md) | M2 rule d, A2 | 1.5.6, 1.5.7, 1.6.5 | 1.6.6, 1.8.12 |
| [#5509: a reply starts playing after the user has begun a new turn](cases/5509-reply-starts-over-new-turn.md) | L2 rule b | 1.5.6 | 1.5.7, 1.6.5, 1.6.6, 1.8.12 |

## Run both on every release

```bash
scripts/check_all.sh
```

This builds the five pinned environments if they are missing, then on each release runs
the tests, the repeatability check, both cases and a replay of both bundles. CI runs the
same steps on Linux and macOS.

## Limits

Both cases run scripted providers against a real `AgentSession` on a virtual clock, and
neither has been compared with a run on a real clock. Each case page lists the rest.
