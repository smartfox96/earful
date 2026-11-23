# Contributing

## Set up

Earful needs [uv](https://docs.astral.sh/uv/) and nothing else installed by hand.

```bash
git clone https://github.com/smartfox96/earful && cd earful
scripts/make_env.sh 1.8.13 2026-11-20     # one pinned environment: .venvs/lk-1.8.13
.venvs/lk-1.8.13/bin/python -m pytest -m "not slow"
```

`make_env.sh` takes a livekit-agents release and the date its dependencies are resolved
as of. `docs/compatibility.md` lists the supported releases with their dates. The package
is installed editable, so the `earful` command and the pytest plugin in that environment
run the code in your checkout.

For the Pipecat-only environment, run `scripts/make_pipecat_env.sh` and
`.venvs/pc-1.16.0/bin/python -m pytest -q`. The script takes a pipecat-ai release and a
date the same way, and with no arguments builds the supported release. Each environment
runs the tests of the core and of its own framework, and pytest reports the test files
of the other framework as skipped.

The files in `compat/` are generated. A release and a date decide what an environment
holds, and a lock file records what the two resolve to on Python 3.13. Nothing installs
from a lock file. CI builds each environment on Python 3.13 with `EARFUL_LOCK=check` and
fails if it holds anything else, so a lock file that is wrong does not merge.
`EARFUL_LOCK=write scripts/make_env.sh <version> <date>` writes one, and
`scripts/make_pipecat_env.sh` takes the same setting.

`pre-commit install` is optional. With it, each commit runs the ruff of `scripts/lint.sh`
on the files it changes (`.pre-commit-config.yaml`).

## The checks

| Command | What it runs | Time |
| --- | --- | --- |
| `.venvs/lk-1.8.13/bin/python -m pytest -m "not slow"` | Every test under a second, on one release | 20 s |
| `scripts/lint.sh` | ruff format, ruff, mypy, and that every deliberate break still applies. `--fix` reformats first | 20 s |
| `scripts/wheel_smoke.sh` | Builds the wheel and the source distribution, installs the wheel alone outside the repository and uses it | 5 s |
| `scripts/quickstart_check.sh` | Follows the README's quickstart from the wheel, with livekit-agents, outside the repository | 90 s |
| `scripts/site.sh DIR` | Draws the two example reports the README links to, into `DIR` | 15 s |
| `scripts/check_all.sh` | All of the above, then on each of the six LiveKit releases: every test, repeatability of the reference ledgers, the two historical cases and the replays | 8 min |
| `.venvs/lint/bin/python scripts/mutate.py` | The control, then each deliberate break of `tests/mutants.py`: a test it names has to fail. `--since main` runs those a change can affect | 4.5 min |

Every environment above names a release and a date. The `canary` workflow is the one
place with neither: each night `scripts/canary.sh livekit` and `scripts/canary.sh pipecat`
install the newest release each extra admits, with that night's dependencies, and run the
tests under a second on it. `scripts/newer_release.sh` fails there when PyPI has a
release an extra does not admit. No pull request waits for the canary. A failed run is
the report: GitHub mails it to whoever last changed the workflow's schedule.

`main` takes a change only as a pull request, and only once that pull request's
`ci / result` check has passed. A pull request also needs the maintainer's approval, given
after its last push (`.github/CODEOWNERS`). Administrators can bypass branch protection. GitHub deletes a merged
branch from the repository.

`check_all.sh` is heavy: six test runs side by side for six minutes. It shares the
processors between them (`EARFUL_WORKERS`), and it is still not something to run on
battery or after every change. While working, the first row of the table is the check to
run. CI runs the rest.

Run `check_all.sh` before opening a pull request that touches `src/`. A change can pass
on the newest release and fail on the oldest: some tests search a real session, and the
older releases have the two defects described in `docs/cases/`.

## What CI runs

`.github/workflows/checks.yml` holds every job. `ci.yml` and `release.yml` call it in one
of two tiers:

- The pull-request tier runs what a change's paths select, by the tables below, which
  `scripts/ci_scope.py` reads. It runs on each pull request, again when its base changes,
  and on each push to `main`. A push to `main` runs nothing when the same files passed
  as a pull request (`scripts/ci_tested.sh`).
- The full tier runs every job: each night on `main`, on a pull request with the label
  `full ci`, on request (`gh workflow run ci.yml --ref <branch>`), and in the release
  workflow on the tagged commit. A failed night opens an issue.

`python3 scripts/ci_scope.py --explain <base> [<head>]` prints what a diff runs and why,
and what it leaves to the full tier. The check `main` requires, `ci / result`, passes when
every job the scope asked for passed and no other ran (`scripts/ci_result.py`), and prints
what the scope decided. A new push, a new base or closing a pull request cancels its run
in progress. A run on `main` is never cancelled.

A path takes the group of the first rule it matches. A test file takes the group of the
framework it declares, so a change to a LiveKit test runs no Pipecat cell, and neither
adapter imports the other. A path no rule matches runs everything, every workflow and
script among them. `python3 scripts/ci_scope.py --table` prints these tables:

| Path | Group |
| --- | --- |
| `docs/**` | prose |
| `*.md` | prose |
| `.github/ISSUE_TEMPLATE/**` | prose |
| `.github/pull_request_template.md` | prose |
| `compat/lk-*.lock.txt` | its release |
| `compat/pc-*.lock.txt` | pipecat |
| `src/earful/pipecat/**` | pipecat |
| `examples/pipecat_booking/**` | pipecat |
| `tests/golden/pc-*` | pipecat |
| `tests/shapes/pipecat/**` | pipecat |
| `src/earful/livekit/**` | livekit |
| `examples/booking/**` | livekit |
| `skills/**` | livekit |
| `tests/golden/lk-*` | livekit |
| `tests/bundles/**` | livekit |
| `tests/shapes/livekit/**` | livekit |
| `src/earful/scripted/clock.py` | clock |
| `src/earful/core/html.py` | report |
| `tests/mutants.py` | breaks |
| `tests/mutants_seconds.json` | breaks |
| `tests/conftest.py` | core |
| `tests/*.py` | declared |
| `tests/shapes/*.py` | declared |
| `src/**` | core |
| `pyproject.toml` | build |
| `LICENSE` | build |
| `NOTICE` | build |
| `.gitignore` | build |
| anything else | everything |

| Group | Runs before a pull request merges |
| --- | --- |
| declared | the group of the framework the file needs: livekit, pipecat or core |
| prose | the tests that read the pages |
| pipecat | format, lint, types; Pipecat on Python 3.11 and 3.13 |
| livekit | format, lint, types; the quickstart from the wheel; LiveKit, every release |
| its release | LiveKit, that release alone |
| core | format, lint, types; the quickstart from the wheel; the wheel installs clean; LiveKit, every release; Pipecat on Python 3.11 and 3.13 |
| clock | format, lint, types; the quickstart from the wheel; the wheel installs clean; LiveKit, every release; LiveKit 1.5.6 and 1.8.13 on macOS; LiveKit on every other Python; Pipecat on Python 3.11 and 3.13; Pipecat on macOS |
| report | format, lint, types; the example reports; LiveKit 1.8.13 |
| build | format, lint, types; the wheel installs clean; the quickstart from the wheel; LiveKit 1.5.6 and 1.8.13; Pipecat from the wheel |
| breaks | format, lint, types |
| everything | the full tier: the tests that read the pages; format, lint, types; the wheel installs clean; the quickstart from the wheel; the example reports; no framework, the oldest jsonschema; LiveKit, every release; LiveKit on macOS, every release; LiveKit on every other Python; Pipecat on Python 3.11 and 3.13; Pipecat from the wheel; Pipecat on macOS |

On a pull request the four middle LiveKit releases leave out the slow tests. The
deliberate breaks a change runs are those anchored in a changed file, those whose named
tests are in a changed file, and those `tests/mutants.py` changed or added. A change to
`scripts/mutate.py`, `tests/conftest.py` or another helper the tests share runs every
break (`scripts/mutate.py --since`).

A change can merge and then fail the full tier on a job its own tier left out. The night's
issue names it, and no release can be made until it passes: the release runs the full
tier on its own commit.

## Rules the code keeps

**The core imports no framework.** Everything under `src/earful/core/` works on plain
data. `tests/test_core_boundary.py` fails if a module there imports `livekit`, `pipecat`
or either Earful adapter. The same rule covers `src/earful/scripted/`. Neither adapter
imports the other or the other's framework. Anything that touches livekit-agents belongs
in `src/earful/livekit/`, and a name taken from inside livekit-agents, one that it does
not export at its top level, goes through `src/earful/livekit/_compat.py`.

**A test file says which framework it needs.** A file under `tests/` that imports
livekit-agents or the LiveKit adapter starts with `pytest.importorskip("livekit.agents")`,
before those imports, and a Pipecat file with `pytest.importorskip("pipecat")`. A file
with neither line imports no framework and runs in every environment, so a test of the
core goes in one of those. `tests/test_core_boundary.py` fails while a file disagrees
with its imports. A file that needs pydantic, which both frameworks install and Earful
alone does not, starts with `pytest.importorskip("pydantic")`. CI runs the Pipecat
environment with `scripts/ran_every_test.sh --without livekit`, where the LiveKit files
are left out and any other skip fails the job, and an environment with no framework with
`--without livekit --without pipecat --without pydantic`.

**Time is integer microseconds.** Authors write seconds. Providers, the sink and the
backend convert once (`earful.core.timebase`) and compute in integers, so that an event on
a framework threshold is decided by the scenario and never by float rounding.

**The checker reads only the ledger.** A contract may use `world`, `llm`, `sink`,
`backend` and `harness` events. The framework's own `fw.*` events explain a run and
decide nothing (`docs/contracts.md`, section 2).

**The reference ledgers change only on purpose.** `tests/golden/` holds 23 ledgers that
every platform must reproduce byte for byte, 18 of LiveKit and 5 of Pipecat, and
`tests/bundles/` the two reduced historical failures. Checks never write there. If a change is meant to alter a ledger,
run `scripts/check_all.sh --update`, read the diff, and say in the pull request why each
change is expected. An unexplained diff in a reference ledger is a bug.

**The formats are versioned.** `earful.ledger/1`, `earful.case/1` and `earful.bundle/1`
each have a JSON Schema under `src/earful/core/schemas/`. An optional field can be added
within `/1`. A new kind, a new required field or a changed meaning is a new major version
(`docs/ledger.md`, Versioning). A test keeps `docs/ledger.md` and `docs/search.md` in step
with the schemas: a field the schema has and the page does not mention fails it.

**A change to behaviour comes with a deliberate break.** `tests/mutants.py` lists small
breaks of the code, each one piece of source text and what replaces it. When you add
behaviour, add the break that removes it and name the test that should catch it. A break
runs the test files that hold the tests it names, which is what keeps the whole run
short; if none of them fails, it runs its named tests across the suite. The run ends by
naming each break caught only that way, where the scan of test names in
`scripts/mutate.py` missed a test. A break that some other test catches fails the run as
misnamed, and one that keeps its tests running for five minutes fails it as timed out. When you move code a break is
anchored on, or rename a test a break names, `scripts/lint.sh` fails until the break is
brought along. Code whose break no test can catch is not needed.

`tests/mutants_seconds.json` holds how long each break took, and CI cuts the breaks into
parts of about sixteen minutes of work from it, four on a runner. After a run,
`mutate.py --record mutation-*.json` with the run's result files replaces the seconds of
the breaks it ran. A break the file does not list counts as the median, and a name it
lists that no break has is left out, so the file never stops a pull request.

**Tests over a second are marked `slow`.** `pytest -m "not slow"` is the loop to work in.

**Property tests try the same inputs on every run.** `tests/test_properties.py` states
what the checker, the shrinker and the time conversion keep on inputs that
[Hypothesis](https://hypothesis.readthedocs.io/) generates. Its profile is derandomized
and keeps no example database: an environment tries the same inputs each time, and a
failure repeats. The inputs differ between Hypothesis releases, and so between
environments. The test tools are the `test` dependency group in `pyproject.toml`, which
every environment script installs.

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

## Shapes, conformance and variations

`tests/shapes/livekit/` and `tests/shapes/pipecat/` hold small applications, one per
module, each written as an application of that framework would be. A module has a
docstring saying what the application does, `case()`, and `SHOULD`: what a correct Earful
reports about it, built with the helpers of `tests/shapes/corpus.py`.

| `SHOULD` | The run |
| --- | --- |
| `judged(M2="pass", L1="pass")` | is judged with those outcomes, and no other contract fails. A contract can name the rules it breaks: `judged(L1="milestone 2")` |
| `invalid(reason)`, `refused(message)`, `raises(error)` | ends that way, with a message that holds the text |
| `ends_within(seconds)` | ends within that many real seconds. The shape runs in a process of its own, stopped then |

While Earful gets a shape wrong, `DEFECT` says what it reports instead, and `DEFECT_ON`
names the releases that shows on when it is not all of them. The shape's test is then a
strict expected failure: it fails when Earful does anything other than `DEFECT` says, and
on the day Earful gets the shape right, until `DEFECT` is removed.
`tests/test_livekit_shapes.py` and `tests/test_pipecat_shapes.py` run every shape on the
installed release.

The same files vary every shape as `tests/invariants.py` says: every time shifted, every
model call and backend write declared longer, a starting history in the words of the run,
a reply voiced in two parts, and the same case object run again, which must give the same
ledger. A variation leaves the verdicts as they are. Longer calls may change them, since a
reply can move off or onto the user's turn, but an L1 that failed still fails: a slower
agent meets no deadline it missed. `DEFECT_VARIED` says what a shape is still judged under
a variation while Earful gets that wrong, and `NOT_VARIED` names a variation that does not
fit the shape, with the reason.

`tests/conformance.py` lists what every adapter does, each behaviour with one check over
what its runs showed. `tests/test_livekit_conformance.py` and
`tests/test_pipecat_conformance.py` give a scenario of each, and fail while one is missing.
A scenario declares its `defect` as a shape does.

A defect of Earful goes in as a shape or a scenario with its `DEFECT` before it is fixed.
One no application shows, in a command, a ledger or the clock, goes in the test file of
that part as a strict expected failure that raises `KnownDefect` (`tests/shapes/corpus.py`)
only where the defect shows, so any other failure fails the test.
`scripts/ran_every_test.sh` counts a strict expected failure as a test that ran.

## Adding a livekit-agents release

1. `EARFUL_LOCK=write scripts/make_env.sh <version> <date>`, with a date a few days after
   the release. This builds `.venvs/lk-<version>` and writes
   `compat/lk-<version>.lock.txt`.
2. Add the release and resolution date to `releases.toml`, to `SUPPORT` in `src/earful/scripted/releases.py`, to
   `scripts/check_all.sh`, to `LIVEKIT` in `scripts/ci_scope.py` and to
   `docs/compatibility.md`. A test fails while they disagree. The newest release is also
   the one in `scripts/make_lint_env.sh`, and the README lists them all.
3. `scripts/check_all.sh --update` records its three reference ledgers.
4. Read the new ledgers against those of the release before it
   (`python scripts/ledger_diff.py`). Differences in behaviour belong in the pull request.

A release in a new minor series also moves the upper bound of the `livekit` extra: in
`SUPPORT`, in `pyproject.toml` and in the sentence of `docs/compatibility.md` that states
it. Until then `earful.livekit` refuses to import on that series. The canary's `beyond`
job fails from the day such a release is on PyPI.

## Adding a pipecat-ai release

Supported pipecat-ai series: 1.16. The release and resolution date are in `releases.toml`.

1. `EARFUL_LOCK=write scripts/make_pipecat_env.sh <version> <date>`, with a date a few
   days after the release. This builds `.venvs/pc-<version>` and writes
   `compat/pc-<version>.lock.txt`. Delete the lock file of the release it replaces.
2. Put the release and the date beside the others, or where the replaced ones are: the defaults of
   `scripts/make_pipecat_env.sh`, `scripts/make_lint_env.sh`, `scripts/check_all.sh`,
   `PIPECAT` in `scripts/ci_scope.py`, the `prose` job of `.github/workflows/checks.yml`,
   `docs/compatibility.md`, the README, this page, `tests/test_ci.py` and `SUPPORT` in
   `src/earful/scripted/releases.py`. A release in a new minor series also moves the
   `pipecat` extra, in `SUPPORT` and in `pyproject.toml`: its lowest release is that of the
   oldest supported series. Tests fail while these disagree.
3. `scripts/ran_every_test.sh --without livekit .venvs/pc-<version>/bin/python` runs
   every test that does not need livekit-agents and fails if one is skipped.
   `scripts/lint.sh` checks the adapter's types against the new release.
4. `scripts/check_all.sh --update` records the five Pipecat reference ledgers under the
   new release's name. Delete those of the release it replaces, and read the new ones against them
   (`python scripts/ledger_diff.py`). A difference in behaviour, in a ledger or in a test
   that had to change, belongs in the pull request.

## Adding a contract

A contract is decided from the ledger alone (`docs/contracts.md`, section 1). Write it
down before the code.

1. Add its section to `docs/contracts.md`, section 6, with the rows the others have: the
   instance, the judged record, each rule, the policy values, when it is unobservable, a
   deliberate break and a clean control. Section 2.2 says which events may decide a
   verdict.
2. Add `src/earful/core/checker/<name>.py` with a `check(view, policy)` that reads the
   run through `View`, and declare the contract in `DECLARED`
   (`src/earful/core/checker/contracts.py`): its title and statement, its policy class,
   its rules, the codes a missing verdict can carry, and what exercises it.
3. Test it three ways. The break and the control are scenarios in
   `tests/test_contract_breaks.py`. Hand-edited ledgers in `tests/test_checker.py` put a
   run on each side of every tolerance. `tests/mutants.py` gets a deliberate break for
   each rule and each tolerance.
4. List its rules in `docs/testing.md`. A test fails while a rule is missing there or has
   no row in its section of `docs/contracts.md`.
5. A contract that needs an event or a field the ledger does not record is a change to
   the format first (`docs/ledger.md`, Versioning).
6. Run `scripts/check_all.sh` and say in the pull request what the contract finds on the
   reference ledgers of the six releases.

A kept bundle records the version of the contracts it was judged with,
`earful.contracts/2`. A new contract, or a rule that changes what it fails, changes what
that version names. Raise in the pull request, before the code, whether it is a new
version.

## Releasing

A release is a tag. `.github/workflows/release.yml` runs the full tier of ci on the tagged
commit, every deliberate break among it, then builds the wheel and the source distribution
once, runs `scripts/wheel_smoke.sh` and `scripts/quickstart_check.sh` on those two files,
publishes the same two files to PyPI, and creates the GitHub release.

1. In one pull request, set `version` in `pyproject.toml` and, in `CHANGELOG.md`, move
   what is under `## [Unreleased]` to a new section `## [<version>] - <date>`, with its
   link at the foot of the file. Merge it. A test fails while the newest section is not
   the version's.
2. `git tag v<version>` on that commit of `main` and push the tag.

The workflow refuses a tag that does not name the version in `pyproject.toml`, a tag on a
commit that is not on `main`, and a version with no section in the changelog. It builds
nothing until every job of the full tier has passed on the tagged commit. A job that
failed for a reason outside the commit can be run again from the release's run.

The two files are built in one job. The job that checks them and the job that publishes
them each compare what they were handed with the SHA-256 sums the build recorded. The
GitHub release takes the version's section of the changelog as its notes
(`scripts/changelog_section.sh`).

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
an artifact of the harness. Compare the behaviour with what the framework documents
before attributing it to the framework, reproduce it on purpose with `earful search`,
and keep the bundle so that someone else can replay it. `docs/cases/` shows the form.

## Pull requests

Work on a branch and open a pull request against `main`. Keep commit messages short: a
subject line and a few plain sentences on what changed and why. CI has to pass. The
pull request template lists what to check. A change a user would notice gets a line under
`## [Unreleased]` in `CHANGELOG.md`.

A bug goes in an issue, with the ledger or the bundle that shows it. A vulnerability is
reported privately (`SECURITY.md`). `CODE_OF_CONDUCT.md` applies to everyone who takes
part.

## Prose

Docs, comments and messages are written for developers who use voice-agent frameworks.
State what is true, with the command, the number or the event that shows it. Short
sentences, active voice, no selling adjectives. Define Earful's own terms the first time
and do not explain what the reader already knows.
