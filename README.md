# Earful

Deterministic regression tests for real-time voice agents. First target: LiveKit Agents
(Python, cascaded STT-LLM-TTS).

Status: in development. Gate 0 (feasibility) has passed; see
[`docs/gate0-feasibility.md`](docs/gate0-feasibility.md). Earful runs a real LiveKit
`AgentSession` deterministically, records a ledger of what happened
([`docs/ledger.md`](docs/ledger.md)), and checks seven contracts against it
([`docs/contracts.md`](docs/contracts.md)). Fault injection, the boundary search and the
developer-facing package are not built yet.

```bash
scripts/check_all.sh            # builds five pinned environments; runs tests, repeatability and historical checks
scripts/check_all.sh --update   # rewrites the reference ledgers in tests/golden/; review the diff

python scripts/check_ledger.py tests/golden/lk-1.5.6.*.jsonl   # the verdicts for a ledger file
```

Layout:

- `src/earful/core/` - ledger, its JSON Schema and validator, run validity, effect backend, and the contract checker (`checker/`). Never imports `livekit`.
- `src/earful/livekit/` - virtual clock, scripted providers, tool witness, clocked sink, harness, cases.
- `scripts/` - environment builder, repeatability and historical checks, and the sweeps the contract defaults rest on (`contracts_sweep.py`, `contracts_m1_sweep.py`).
- `tests/golden/` - reference ledgers every platform must reproduce byte for byte. Checks never write here.
- `docs/gate0/evidence/` - Gate 0's raw output (ledger schema 0).
- `compat/` - exact dependency set for each supported livekit-agents release.
