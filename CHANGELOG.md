# Changelog

What changes for someone who uses Earful, release by release. The format is
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the version numbers follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The ledger, case and bundle
formats have versions of their own
([`docs/ledger.md`](https://github.com/smartfox96/earful/blob/main/docs/ledger.md), Versioning).

## [Unreleased]

### Added

- A Pipecat adapter, `earful.pipecat`, for pipecat-ai 1.16 on Python 3.11 or later.
  Install it with `pip install "earful[pipecat]"`.
  [`docs/pipecat.md`](https://github.com/smartfox96/earful/blob/main/docs/pipecat.md)
  shows a first scenario.
- `earful.scripted`, the package both adapters share: the virtual clock, the scripted
  user, model and voice, and a scenario's intents, delays and faults. `earful.livekit`
  exports the names it did before.

### Changed

- The `earful` command and the pytest fixture choose the adapter from the scenario or
  the kept bundle they are given.

## [0.1.1] - 2026-11-18

### Added

- `earful init module:Class` writes a first test for an agent from the agent's own
  function tools. The test runs as written.
- `EARFUL_WORKERS` sets how many worker processes a search starts when no number is
  given. The pytest fixture and `earful search` read it.
- [`skills/earful/SKILL.md`](https://github.com/smartfox96/earful/blob/main/skills/earful/SKILL.md),
  one page that tells a coding assistant how to write and read an Earful test.
- `docs/testing.md` shows how to keep a failing test's reports as an artifact in CI.

### Changed

- Under pytest, `--earful-workers` defaults to `auto`: a search of a scenario that can
  be imported starts one worker process per processor. A number on the command line or
  in the call is used as given.
- The README opens with the report of a failing run and links to two example reports.

## [0.1.0] - 2026-11-18

The first release.

### Added

- A livekit-agents `AgentSession` run on a virtual clock against a scripted user, a
  scripted model, a scripted voice and a fake backend, and recorded as a ledger that is
  the same byte for byte on every run. Tested with livekit-agents 1.5.6, 1.5.7, 1.6.5,
  1.6.6 and 1.8.12, on Linux and macOS, Python 3.10 to 3.14.
- Seven contracts decided from the ledger alone: A1, A2, A3, M1, M2, L1 and L2
  ([`docs/contracts.md`](https://github.com/smartfox96/earful/blob/main/docs/contracts.md)).
- A search of the timings and backend faults near a scenario, the reduction of a
  failing run to a small case, and a bundle that replays it.
- A report of a run as one HTML page: a timeline, the verdict of each contract and
  every event.
- The `earful` command (`check`, `run`, `search`, `replay`, `crosscheck`, `report`) and
  the `earful` pytest fixture.
- The formats `earful.ledger/1`, `earful.case/1` and `earful.bundle/1`, each with a
  JSON Schema.

[Unreleased]: https://github.com/smartfox96/earful/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/smartfox96/earful/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/smartfox96/earful/releases/tag/v0.1.0
