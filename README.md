# Earful

Deterministic regression tests for real-time voice agents. First target: LiveKit Agents
(Python, cascaded STT-LLM-TTS).

Status: in development. Gate 0 (feasibility) and Gate 1 (the core) have passed; see
[`docs/gate0-feasibility.md`](docs/gate0-feasibility.md) and
[`docs/gate1-core.md`](docs/gate1-core.md). Earful runs a real LiveKit `AgentSession`
deterministically, records a ledger of what happened ([`docs/ledger.md`](docs/ledger.md)),
checks seven contracts against it ([`docs/contracts.md`](docs/contracts.md)), searches a
scenario for the timings at which it fails, measures how wide each failure is, and reduces
a failing case to a small one that replays ([`docs/search.md`](docs/search.md)). Fault
injection and the developer-facing package are not built yet.

```bash
scripts/check_all.sh            # builds five pinned environments; runs tests, repeatability, historical checks and replays
scripts/check_all.sh --update   # rewrites tests/golden/ and tests/bundles/; review the diff

python scripts/check_ledger.py tests/golden/lk-1.5.6.*.jsonl           # the verdicts for a ledger file
python scripts/search.py earful.livekit.cases:book_then_interrupt      # move every time to every boundary: what breaks, how wide, what was reached
python scripts/replay.py tests/bundles/lk-1.5.6.book_then_interrupt.A2-rule   # run a reduced failing case again
```

Layout:

- `src/earful/core/` - ledger, its JSON Schema and validator, run validity, effect backend, the contract checker (`checker/`), the case format (`case.py`), the boundary scheduler (`search.py`), the shrinker (`shrink.py`) and the failure bundle (`bundle.py`). Never imports `livekit`.
- `src/earful/livekit/` - virtual clock, scripted providers, tool witness, clocked sink, harness, cases, and the adapter that runs a case from its data (`scenario.py`).
- `scripts/` - environment builder, repeatability and historical checks, and the sweeps the contract defaults rest on (`contracts_sweep.py`, `contracts_m1_sweep.py`).
- `tests/golden/` - reference ledgers every platform must reproduce byte for byte. Checks never write here.
- `tests/bundles/` - the two known defects, found by the search and reduced: red on the releases that have them, green on the ones that repaired them.
- `docs/gate0/evidence/` - Gate 0's raw output (ledger schema 0).
- `compat/` - exact dependency set for each supported livekit-agents release.
