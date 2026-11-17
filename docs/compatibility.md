# Compatibility

What Earful is tested with. A combination that is not in the tables below may work and
is not claimed to.

## livekit-agents

Five releases are supported. Each is installed with its dependencies resolved as of a
date a few days after it came out, which gives the set the release was tested with at the
time (`scripts/make_env.sh`). The exact set for each is in `compat/`.

| livekit-agents | Dependencies as of | Lock file | #3702 | #5509 |
| --- | --- | --- | --- | --- |
| 1.5.6 | 2026-04-30 | `compat/lk-1.5.6.lock.txt` | present | present |
| 1.5.7 | 2026-05-06 | `compat/lk-1.5.7.lock.txt` | present | repaired |
| 1.6.5 | 2026-07-20 | `compat/lk-1.6.5.lock.txt` | present | repaired |
| 1.6.6 | 2026-07-20 | `compat/lk-1.6.6.lock.txt` | repaired | repaired |
| 1.8.12 | 2026-11-14 | `compat/lk-1.8.12.lock.txt` | repaired | repaired |

The last two columns say whether the release has the two known defects Earful reproduces
(`docs/cases/`). A search of your own agent on an affected release can meet them.

`pip install "earful[livekit]"` accepts livekit-agents from 1.5.6 up to, and not
including, 1.9. A release between two rows, or a later 1.8 release, is accepted and not
tested. Earful takes over the framework's clock and wraps its tools, and it checks on
every run that it could: a run in which a clock read could not be given the virtual time
is marked invalid and yields no verdict, so an unsupported release fails loudly.

## What CI runs

On every pull request, each row below runs every test, checks that each of the three
reference scenarios gives the committed ledger byte for byte in 100 runs in one process
and 20 in fresh processes, runs the two historical cases, and replays both kept failures.

| livekit-agents | Linux, Python 3.13 | macOS, Python 3.13 | Linux, other Pythons |
| --- | --- | --- | --- |
| 1.5.6 | every change | every change | 3.11 |
| 1.5.7 | every change | nightly | |
| 1.6.5 | every change | nightly | |
| 1.6.6 | every change | nightly | 3.10 |
| 1.8.12 | every change | every change | 3.12, 3.14 |

Python 3.10 to 3.14 are each tested with one release. Windows is not tested.

The reference ledgers are the same on Linux and macOS and on every Python above: a ledger
recorded on one machine is what every other must reproduce.

A separate job builds the wheel, installs it alone in a fresh environment outside the
repository, without livekit-agents, and uses it (`scripts/wheel_smoke.sh`): the package
imports and checks a ledger with nothing but its own dependencies.

## The portable core

`earful` without the `livekit` extra depends only on `jsonschema`. It reads and checks
ledgers, and reads bundles. It runs nothing. Python 3.10 or later.

## What is outside

- Realtime (speech-to-speech) models, and any pipeline other than STT, LLM and TTS.
- Real providers: the STT, the model, the voice and the VAD are Earful's scripted ones.
- A room, a network transport, or audio as a caller's device plays it. The output is a
  sink that consumes frames on the virtual clock.
- A turn detector that is a model. Turns end on the scripted VAD's silence.
