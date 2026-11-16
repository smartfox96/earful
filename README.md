# Earful

Deterministic regression tests for real-time voice agents. First target: LiveKit Agents
(Python, cascaded STT-LLM-TTS).

Status: design stage. Gate 0 (feasibility) has passed; see
[`docs/gate0-feasibility.md`](docs/gate0-feasibility.md). The contracts Earful will check
are specified in [`docs/contracts.md`](docs/contracts.md), and the ledger they are checked
against in [`docs/ledger.md`](docs/ledger.md).

```bash
scripts/check_all.sh            # builds five pinned environments; runs tests, repeatability and historical checks
scripts/check_all.sh --update   # rewrites the reference ledgers in tests/golden/; review the diff
```

Layout:

- `src/earful/core/` - ledger, its JSON Schema and validator, run validity, effect backend, ledger queries. Never imports `livekit`.
- `src/earful/livekit/` - virtual clock, scripted providers, tool witness, clocked sink, harness, cases.
- `scripts/` - environment builder, repeatability and historical checks.
- `tests/golden/` - reference ledgers every platform must reproduce byte for byte. Checks never write here.
- `docs/gate0/evidence/` - Gate 0's raw output (ledger schema 0).
- `compat/` - exact dependency set for each supported livekit-agents release.
