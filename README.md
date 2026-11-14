# Earful

Deterministic regression tests for real-time voice agents. First target: LiveKit Agents
(Python, cascaded STT-LLM-TTS).

Status: design stage. Gate 0 (feasibility) has passed on macOS; see
[`docs/gate0-feasibility.md`](docs/gate0-feasibility.md).

```bash
scripts/gate0_all.sh    # builds five pinned environments and reproduces every Gate 0 result
```

Layout:

- `src/earful/core/` - ledger, effect backend, ledger queries. Never imports `livekit`.
- `src/earful/livekit/` - virtual clock, scripted providers, clocked sink, harness, cases.
- `scripts/` - environment builder and the Gate 0 runs.
- `docs/gate0/evidence/` - raw summaries and ledgers from those runs.
- `compat/` - exact dependency set for each supported livekit-agents release.
