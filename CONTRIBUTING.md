# Contributing

## Set up

Earful needs [uv](https://docs.astral.sh/uv/) and nothing else installed by hand.

```bash
git clone https://github.com/smartfox96/earful && cd earful
scripts/make_env.sh 1.8.12 2026-11-14     # one pinned environment: .venvs/lk-1.8.12
.venvs/lk-1.8.12/bin/python -m pytest -m "not slow"
```

`make_env.sh` takes a livekit-agents release and the date its dependencies are resolved
as of. `docs/compatibility.md` lists the supported releases with their dates. The package
is installed editable, so the `earful` command and the pytest plugin in that environment
run the code in your checkout.

## The checks

| Command | What it runs | Time |
| --- | --- | --- |
| `.venvs/lk-1.8.12/bin/python -m pytest -m "not slow"` | Every test under a second, on one release | 20 s |
| `scripts/lint.sh` | ruff format, ruff, mypy, and that every deliberate break still applies. `--fix` reformats first | 20 s |
| `scripts/wheel_smoke.sh` | Builds the wheel and the source distribution, installs the wheel alone outside the repository and uses it | 5 s |
| `scripts/quickstart_check.sh` | Follows the README's quickstart from the wheel, with livekit-agents, outside the repository | 90 s |
| `scripts/site.sh DIR` | Draws the two example reports the README links to, into `DIR` | 15 s |
| `scripts/check_all.sh` | All of the above, then on each of the five releases: every test, repeatability of the reference ledgers, the two historical cases and the replays | 8 min |
| `.venvs/lk-1.8.12/bin/python scripts/mutate.py` | Applies each deliberate break of `tests/mutants.py` and checks that a test it names fails | 5 min |

CI runs `check_all.sh`'s parts on every pull request, on Linux for the five releases and
on macOS for the oldest and the newest. The deliberate breaks run each night.

`main` takes a change only as a pull request, and only once that pull request's
`ci result` check has passed. A pull request also needs the maintainer's approval, given
after its last push (`.github/CODEOWNERS`). Nobody can push to `main`, and a merged
branch is deleted from the repository.

`check_all.sh` is heavy: five test runs side by side for six minutes. It shares the
processors between them (`EARFUL_WORKERS`), and it is still not something to run on
battery or after every change. While working, the first row of the table is the check to
run. CI runs everything on the pull request, and the deliberate breaks run there in seven
minutes (`gh workflow run mutation.yml --ref <branch>`), where they take a whole machine
for five here.

Run `check_all.sh` before opening a pull request that touches `src/`. A change can pass
on the newest release and fail on the oldest: some tests search a real session, and the
older releases have the two defects described in `docs/cases/`.

## Rules the code keeps

**The core imports no framework.** Everything under `src/earful/core/` works on plain
data. `tests/test_core_boundary.py` fails if a module there imports `livekit` or
`earful.livekit`. Anything that touches livekit-agents belongs in `src/earful/livekit/`,
and a name taken from inside livekit-agents, one that it does not export at its top
level, goes through `src/earful/livekit/_compat.py`.

**Time is integer microseconds.** Authors write seconds. Providers, the sink and the
backend convert once (`earful.core.timebase`) and compute in integers, so that an event on
a framework threshold is decided by the scenario and never by float rounding.

**The checker reads only the ledger.** A contract may use `world`, `llm`, `sink`,
`backend` and `harness` events. The framework's own `fw.*` events explain a run and
decide nothing (`docs/contracts.md`, section 2).

**The reference ledgers change only on purpose.** `tests/golden/` holds 15 ledgers that
every platform must reproduce byte for byte, and `tests/bundles/` the two reduced
historical failures. Checks never write there. If a change is meant to alter a ledger,
run `scripts/check_all.sh --update`, read the diff, and say in the pull request why each
change is expected. An unexplained diff in a reference ledger is a bug.

**The formats are versioned.** `earful.ledger/1`, `earful.case/1` and `earful.bundle/1`
each have a JSON Schema under `src/earful/core/schemas/`. An optional field can be added
within `/1`. A new kind, a new required field or a changed meaning is a new major version
(`docs/ledger.md`, Versioning). A test keeps `docs/ledger.md` and `docs/search.md` in step
with the schemas: a field the schema has and the page does not mention fails it.

**A change to behaviour comes with a deliberate break.** `tests/mutants.py` lists small
breaks of the code, each one piece of source text and what replaces it. When you add
behaviour, add the break that removes it and name the test that should catch it. Only
the named tests are run against a break, which is what keeps the whole run short; a break
that some other test catches fails the run as misnamed. When you move code a break is
anchored on, or rename a test a break names, `scripts/lint.sh` fails until the break is
brought along. A break that no test can catch is a sign the code it breaks is not needed.

**Tests over a second are marked `slow`.** `pytest -m "not slow"` is the loop to work in.

**The README's picture and its example reports are made from the code.** The two reports
are drawn by `scripts/site.sh`: on every pull request, to see that they still build, and
from `main` by the `pages` workflow, which publishes them. The picture,
`docs/img/report-booking.png`, is a screenshot of `booking.html#f1-1` (the A2 finding
selected) in a window 1280 pixels wide, scrolled to the timeline. Take it again when a
change alters how a report is drawn.

**The README's quickstart is run.** Its two files are `examples/booking`, a test runs
them as shipped and with the fix, and `scripts/quickstart_check.sh` follows the steps from
a built wheel. A change to the example, to a message the README quotes or to a command it
uses fails one of them until the README is brought along.

## Adding a livekit-agents release

1. `scripts/make_env.sh <version> <date>`, with a date a few days after the release. This
   writes `compat/lk-<version>.lock.txt`.
2. Add the release to `scripts/check_all.sh`, to the matrix in `.github/workflows/ci.yml`
   and to `docs/compatibility.md`. A test fails while the three disagree.
3. `scripts/check_all.sh --update` records its three reference ledgers.
4. Read the new ledgers against those of the release before it
   (`python scripts/ledger_diff.py`). Differences in behaviour belong in the pull request.

## Releasing

A release is a tag. `.github/workflows/release.yml` builds the wheel and the source
distribution from it, runs `scripts/wheel_smoke.sh` and `scripts/quickstart_check.sh`
against them, and publishes both to PyPI.

1. Set `version` in `pyproject.toml` and merge that to `main`.
2. Run the deliberate breaks on that commit: the `mutation` workflow, from the Actions
   page. Every one has to be caught.
3. `git tag v<version>` on that commit of `main` and push the tag. The workflow refuses a
   tag that does not name the version in `pyproject.toml`, and a tag on a commit that is
   not on `main`.

A `v*` tag cannot be moved or deleted once it is pushed. A release that went out wrong is
followed by a new version.

Publishing uses a PyPI trusted publisher, which is set up once on pypi.org for the
project `earful`: owner `smartfox96`, repository `earful`, workflow `release.yml`,
environment `pypi`. No token is stored in the repository.

The formats have their own versions (`docs/ledger.md`, Versioning). A release that
changes what a recorded ledger hashes to says so in its notes.

## Reporting what Earful finds

A broken contract is evidence, and what it is evidence of has to be worked out before
anything is said in public. Classify a failure as one of: a defect in the framework, a
default that is hazardous and documented, an error in the application or its backend, or
an artifact of the harness. Compare the behaviour with what livekit-agents documents
before attributing it to livekit-agents, reproduce it on purpose with `earful search`,
and keep the bundle so that someone else can replay it. `docs/cases/` shows the form.

## Pull requests

Work on a branch and open a pull request against `main`. Keep commit messages short: a
subject line and a few plain sentences on what changed and why. CI has to pass.

## Prose

Docs, comments and messages are written for developers who use voice-agent frameworks.
State what is true, with the command, the number or the event that shows it. Short
sentences, active voice, no selling adjectives. Define Earful's own terms the first time
and do not explain what the reader already knows.
