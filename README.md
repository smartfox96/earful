# Earful

Deterministic regression tests for real-time voice agents. First target: LiveKit Agents
(Python, cascaded STT-LLM-TTS).

Status: in development. Three gates have passed:

- Gate 0, feasibility: [`docs/gate0-feasibility.md`](docs/gate0-feasibility.md)
- Gate 1, the core: [`docs/gate1-core.md`](docs/gate1-core.md)
- Gate 2, two known LiveKit defects shown red and green: [`docs/gate2-historical-proof.md`](docs/gate2-historical-proof.md)

Earful runs a real LiveKit `AgentSession` deterministically and records a ledger of what
happened ([`docs/ledger.md`](docs/ledger.md)). It checks seven contracts against the
ledger ([`docs/contracts.md`](docs/contracts.md)). It searches a scenario for the timings,
the backend faults and the moment of a hangup under which a contract breaks, and measures
how wide each failure is. A failing case is reduced to a small one that replays
([`docs/search.md`](docs/search.md)).
A test gives it an agent, a scripted user and a scripted model, and asserts the contracts
([`docs/testing.md`](docs/testing.md)).
The command line, the pytest plugin and the report are not built yet.

```bash
scripts/check_all.sh            # builds five pinned environments; runs tests, repeatability, historical checks and replays
scripts/check_all.sh --update   # rewrites tests/golden/ and tests/bundles/; review the diff
scripts/lint.sh                 # format, lint rules, types
.venvs/lk-1.8.12/bin/python -m pytest -m "not slow"   # the tests under a second each: about 15 s in all

python scripts/check_ledger.py tests/golden/lk-1.5.6.*.jsonl           # the verdicts for a ledger file
python scripts/search.py earful.livekit.cases:book_then_interrupt      # every time to every boundary, every fault on every write: what breaks, how wide, what was reached
python scripts/replay.py tests/bundles/lk-1.5.6.book_then_interrupt.A2-rule   # run a reduced failing case again
```

Layout:

- `src/earful/core/` - ledger, its JSON Schema and validator, run validity, effect backend, the contract checker (`checker/`), the case format (`case.py`), the boundary scheduler (`search.py`), the shrinker (`shrink.py`) and the failure bundle (`bundle.py`). Never imports `livekit`.
- `src/earful/livekit/` - virtual clock, scripted providers, tool witness, clocked sink, harness, cases, and the adapter that runs a case from its data (`scenario.py`).
- `scripts/` - environment builder, the static checks (`lint.sh`), repeatability and historical checks, the sweeps the contract defaults rest on (`contracts_sweep.py`, `contracts_m1_sweep.py`), and the checks behind the Gate 1 page: deliberate breaks of the code (`mutate.py`), its figures (`gate1_figures.py`) and the search against the sweep (`search_vs_sweep.py`).
- `tests/golden/` - reference ledgers every platform must reproduce byte for byte. Checks never write here.
- `tests/bundles/` - the two known defects, found by the search and reduced: red on the releases that have them, green on the ones that repaired them.
- `docs/cases/` - one page per known defect: the commands, what the ledger shows, the limits.
- `docs/gate0/evidence/` - Gate 0's raw output (ledger schema 0).
- `compat/` - exact dependency set for each supported livekit-agents release.
