"""The scripts CI decides with do what they say, and the workflows are wired to them."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
WORKFLOWS = {path.name: path.read_text() for path in (ROOT / ".github/workflows").glob("*.yml")}
CI = WORKFLOWS["ci.yml"]

# The machine's own git settings stay out: a signing rule there would fail every commit.
_GIT = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Earful tests",
    "GIT_AUTHOR_EMAIL": "tests@example.invalid",
    "GIT_COMMITTER_NAME": "Earful tests",
    "GIT_COMMITTER_EMAIL": "tests@example.invalid",
}
_NO_COMMIT = "0" * 40


def _git(repo, *args):
    done = subprocess.run(
        ["git", *args],
        cwd=repo,
        env={**os.environ, **_GIT},
        capture_output=True,
        text=True,
        check=False,  # a failure is reported below, with what git said
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _commit(repo, changes):
    """Commit ``changes``, a path to its new text or to None for a deletion."""
    for path, text in changes.items():
        if text is None:
            (repo / path).unlink()
        else:
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_text(text)
    _git(repo, "add", "--all")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "a change")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _commit(root, {"README.md": "one\n", "src/earful/cli.py": "a = 1\n", "docs/page.md": "p\n"})
    return root


def _script(name, *args, cwd, **env):
    return subprocess.run(
        [str(SCRIPTS / name), *args],
        cwd=cwd,
        env={**os.environ, **_GIT, **env},
        capture_output=True,
        text=True,
        check=False,  # the tests assert on the exit status
    )


def _scope(repo, *args, **env):
    """What ``scripts/ci_scope.sh`` printed, as a dict, and its exit status."""
    done = _script("ci_scope.sh", *args, cwd=repo, **env)
    return dict(line.split("=") for line in done.stdout.splitlines()), done.returncode


NOTHING = {"code": "false", "docs": "false", "mutants": "false"}
PROSE = {"code": "false", "docs": "true", "mutants": "false"}
CODE = {"code": "true", "docs": "false", "mutants": "false"}
BEHAVIOUR = {"code": "true", "docs": "false", "mutants": "true"}  # the deliberate breaks run
NIGHTLY = {"code": "true", "docs": "true", "mutants": "false"}
EVERYTHING = {"code": "true", "docs": "true", "mutants": "true"}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("docs/pipecat.md", PROSE),  # read by tests/test_pipecat.py
        ("docs/img/report-booking.png", PROSE),
        ("README.md", PROSE),  # read by tests/test_docs.py
        ("CHANGELOG.md", PROSE),
        ("src/earful/cli.py", BEHAVIOUR),
        ("tests/test_docs.py", BEHAVIOUR),
        ("scripts/mutate.py", BEHAVIOUR),
        ("scripts/make_lint_env.sh", BEHAVIOUR),  # the environment the breaks run in
        (".github/workflows/mutation.yml", BEHAVIOUR),
        ("scripts/lint.sh", CODE),
        ("skills/earful/SKILL.md", CODE),  # Markdown that tests/test_examples.py runs
        ("examples/booking/agent.py", CODE),
        (".github/workflows/ci.yml", CODE),
        ("compat/pc-1.16.0.lock.txt", CODE),
        ("pyproject.toml", CODE),
        ("LICENSE", CODE),  # the build reads it
    ],
)
def test_a_changed_path_runs_the_jobs_whose_tests_read_it(repo, path, expected):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, {path: "changed\n"})
    assert _scope(repo, base, head) == (expected, 0)


def test_the_scope_of_several_paths_is_the_union(repo):
    base = _git(repo, "rev-parse", "HEAD")
    assert _scope(repo, base, _commit(repo, {})) == (NOTHING, 0)
    head = _commit(repo, {"docs/page.md": "new\n", "pyproject.toml": "[project]\n"})
    assert _scope(repo, base, head) == (NIGHTLY, 0)
    head = _commit(repo, {"src/earful/cli.py": "a = 2\n"})
    assert _scope(repo, base, head) == (EVERYTHING, 0)


def test_a_deleted_or_moved_file_counts_where_it_was(repo):
    base = _git(repo, "rev-parse", "HEAD")
    assert _scope(repo, base, _commit(repo, {"src/earful/cli.py": None})) == (BEHAVIOUR, 0)
    _git(repo, "reset", "-q", "--hard", base)
    _git(repo, "mv", "src/earful/cli.py", "docs/cli.py")
    assert _scope(repo, base, _commit(repo, {})) == (EVERYTHING, 0)


@pytest.mark.parametrize("base", ["", _NO_COMMIT, "no-such-branch"])
def test_a_base_that_cannot_be_read_runs_everything(repo, base):
    assert _scope(repo, base, "HEAD") == (EVERYTHING, 0)


def test_a_diff_that_fails_fails_the_scope_and_prints_nothing(repo):
    scope, status = _scope(repo, "HEAD", _NO_COMMIT)
    assert status != 0 and scope == {}


def test_a_pull_request_is_scoped_by_what_its_merge_brings_to_the_base(repo):
    _git(repo, "checkout", "-q", "-b", "topic")
    _commit(repo, {"docs/page.md": "from the branch\n"})
    _git(repo, "checkout", "-q", "main")
    _commit(repo, {"src/earful/cli.py": "a = 3  # on main meanwhile\n"})
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "topic")
    assert _scope(repo, "--event", "pull_request") == (PROSE, 0)


def test_a_nightly_or_manual_run_checks_everything(repo):
    assert _scope(repo, "--event", "workflow_dispatch") == (EVERYTHING, 0)
    # The deliberate breaks have their own nightly run, 30 minutes after this one.
    assert _scope(repo, "--event", "schedule") == (NIGHTLY, 0)
    assert '    - cron: "17 4 * * *"' in CI
    assert '    - cron: "47 4 * * *"' in WORKFLOWS["mutation.yml"]


def test_an_event_has_to_be_named(repo):
    scope, status = _scope(repo, "--event")
    assert status != 0 and scope == {}


_GH = """#!/usr/bin/env bash
# Stands in for gh: answers the three calls of scripts/ci_tested.sh from the environment.
echo "$*" >>"$STUB_LOG"
case "$2" in
  */commits/*/pulls) [ "${STUB_PR_HEAD-}" != fail ] || exit 1; echo "${STUB_PR_HEAD-}" ;;
  */git/commits/*) echo "$STUB_HEAD_TREE" ;;
  */actions/workflows/ci.yml/runs*) echo "$STUB_PASSED" ;;
  *) exit 2 ;;
esac
"""


@pytest.fixture
def gh(tmp_path, repo):
    """A ``gh`` first on PATH that says HEAD came from a pull request on which ci passed."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(_GH)
    (bin_dir / "gh").chmod(0o755)
    return {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_REPOSITORY": "someone/project",
        "STUB_LOG": str(tmp_path / "gh.log"),
        "STUB_PR_HEAD": "f" * 40,
        "STUB_HEAD_TREE": _git(repo, "rev-parse", "HEAD^{tree}"),
        "STUB_PASSED": "1",
    }


def _tested(repo, env):
    done = _script("ci_tested.sh", _git(repo, "rev-parse", "HEAD"), cwd=repo, **env)
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_a_commit_is_tested_when_ci_passed_on_a_pull_request_with_the_same_files(repo, gh):
    assert _tested(repo, gh) == "tested=true\n"
    head = _git(repo, "rev-parse", "HEAD")
    calls = Path(gh["STUB_LOG"]).read_text().splitlines()
    assert calls[0].startswith(f"api repos/someone/project/commits/{head}/pulls ")
    assert calls[1].startswith(f"api repos/someone/project/git/commits/{'f' * 40} ")
    asked = f"runs?head_sha={'f' * 40}&event=pull_request&status=success"
    assert asked in calls[2] and len(calls) == 3


@pytest.mark.parametrize(
    "change",
    [
        {"GITHUB_REPOSITORY": ""},
        {"STUB_PR_HEAD": ""},  # no merged pull request holds the commit
        {"STUB_PR_HEAD": "fail"},  # gh could not ask
        {"STUB_HEAD_TREE": "e" * 40},  # the pull request's files are not these files
        {"STUB_PASSED": "0"},
        {"STUB_PASSED": ""},
    ],
)
def test_a_commit_is_untested_unless_every_answer_says_so(repo, gh, change):
    assert _tested(repo, {**gh, **change}) == "tested=false\n"


def test_a_push_of_tested_files_runs_nothing_and_any_other_push_is_scoped(repo, gh):
    before = _git(repo, "rev-parse", "HEAD")
    _commit(repo, {"src/earful/cli.py": "a = 4\n"})
    tested = {**gh, "STUB_HEAD_TREE": _git(repo, "rev-parse", "HEAD^{tree}")}
    assert _scope(repo, "--event", "push", before, **tested) == (NOTHING, 0)
    assert _scope(repo, "--event", "push", before, **gh) == (BEHAVIOUR, 0)
    assert _scope(repo, "--event", "push", _NO_COMMIT, **gh) == (EVERYTHING, 0)
    assert _scope(repo, "--event", "push", **gh) == (EVERYTHING, 0)


def _jobs(workflow):
    """The text of each job of ``workflow``, by its key."""
    parts = re.split(r"^  ([a-z_]+):\n", workflow.partition("\njobs:\n")[2], flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def _run_block(text, step):
    """The shell script of the step named ``step``, as the runner would be handed it."""
    after = text.partition(f"- name: {step}\n")[2]
    assert after, f"no step is named {step}"
    before_run, _, body = after.partition("run: |\n")
    assert "- name:" not in before_run and "- uses:" not in before_run
    lines = body.splitlines()
    indent = len(lines[0]) - len(lines[0].lstrip())
    block = []
    for line in lines:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        block.append(line[indent:])
    return "\n".join(block) + "\n"


# How a runner starts bash: with ``shell: bash`` named, and as it does when none is named.
_SHELLS = [["bash", "--noprofile", "--norc", "-eo", "pipefail"], ["bash", "-e"]]


@pytest.mark.parametrize("shell", _SHELLS, ids=["shell named", "no shell named"])
@pytest.mark.parametrize("scope_script", ["exit 3", "echo code=true; exit 3"])
def test_the_scope_step_fails_and_sets_no_output_when_its_script_fails(
    tmp_path, shell, scope_script
):
    step = _run_block(
        _jobs(CI)["changes"], "Decide from the event and the changed paths (scripts/ci_scope.sh)"
    )
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/ci_scope.sh").write_text(f"#!/usr/bin/env bash\n{scope_script}\n")
    (tmp_path / "scripts/ci_scope.sh").chmod(0o755)
    (tmp_path / "step.sh").write_text(step)
    output = tmp_path / "output"
    output.touch()
    env = {
        "EVENT": "push",
        "BEFORE": "",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(output),
    }
    environment = {**os.environ, **env}
    done = subprocess.run([*shell, "step.sh"], cwd=tmp_path, env=environment, check=False)
    assert done.returncode != 0
    assert output.read_text() == ""


def test_the_scope_step_hands_the_scope_to_the_jobs(tmp_path, repo):
    jobs = _jobs(CI)
    step = _run_block(
        jobs["changes"], "Decide from the event and the changed paths (scripts/ci_scope.sh)"
    )
    (repo / "scripts").symlink_to(SCRIPTS)
    (tmp_path / "step.sh").write_text(step)
    output = tmp_path / "output"
    env = {
        "EVENT": "schedule",
        "BEFORE": "",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(output),
    }
    shell = [*_SHELLS[0], str(tmp_path / "step.sh")]
    subprocess.run(shell, cwd=repo, env={**os.environ, **env}, check=True)
    assert output.read_text() == "code=true\ndocs=true\nmutants=false\n"
    for name in ("code", "docs", "mutants"):
        assert f"{name}: ${{{{ steps.scope.outputs.{name} }}}}" in jobs["changes"]
        assert f"{name.upper()}: ${{{{ needs.changes.outputs.{name} }}}}" in jobs["result"]


_ALL = ("changes", "docs", "lint", "wheel", "quickstart", "pipecat", "determinism", "mutants")


def _result(scope, /, **results):
    """The exit status of ``scripts/ci_result.sh``; a job not named was skipped."""
    pairs = " ".join(f"{job}={results.pop(job, 'skipped')}" for job in _ALL)
    pairs += "".join(f" {job}={result}" for job, result in results.items())
    given = {name.upper(): value for name, value in scope.items()}
    done = _script("ci_result.sh", cwd=ROOT, RESULTS=pairs, **given)
    assert done.returncode in (0, 1), done.stderr
    return done.returncode


_RAN = dict.fromkeys(
    ("changes", "lint", "wheel", "quickstart", "pipecat", "determinism"), "success"
)


def test_the_result_passes_when_every_job_the_scope_asks_for_passed():
    assert _result(CODE, **_RAN) == 0
    assert _result(NIGHTLY, **_RAN) == 0
    assert _result(BEHAVIOUR, **_RAN, mutants="success") == 0
    assert _result(EVERYTHING, **_RAN, mutants="success") == 0
    assert _result(PROSE, changes="success", docs="success") == 0
    assert _result(NOTHING, changes="success") == 0


@pytest.mark.parametrize(
    ("scope", "results"),
    [
        ({"code": "", "docs": "", "mutants": ""}, {"changes": "success"}),  # no output was set
        ({**CODE, "docs": ""}, _RAN),
        ({**CODE, "code": "yes"}, _RAN),
        ({"code": "true", "docs": "false"}, _RAN),  # mutants is not given at all
        (CODE, {**_RAN, "lint": "skipped"}),
        (CODE, {**_RAN, "determinism": "failure"}),
        (CODE, {**_RAN, "pipecat": "cancelled"}),
        (CODE, {**_RAN, "wheel": ""}),
        (BEHAVIOUR, _RAN),  # the deliberate breaks did not run
        (BEHAVIOUR, {**_RAN, "mutants": "failure"}),
        (PROSE, {"changes": "success"}),  # the docs job did not run
        (NOTHING, {"changes": "success", "lint": "failure"}),
        (NOTHING, {}),  # the scope job itself was skipped
        (NOTHING, {"changes": "failure"}),
        (CODE, {**_RAN, "another": "success"}),  # a job the script has no rule for
    ],
)
def test_the_result_fails_otherwise(scope, results):
    assert _result(scope, **results) == 1


def test_the_result_fails_when_a_job_is_missing_from_what_it_is_given():
    pairs = " ".join(f"{job}=success" for job in _ALL if job != "determinism")
    done = _script(
        "ci_result.sh", cwd=ROOT, CODE="true", DOCS="false", MUTANTS="true", RESULTS=pairs
    )
    assert done.returncode == 1 and "determinism" in done.stderr


def test_the_required_check_waits_for_every_job_of_the_workflow():
    jobs = _jobs(CI)
    assert "    name: ci result\n" in jobs["result"]
    assert "    if: always() && github.event.action != 'closed'\n" in jobs["result"]
    others = sorted(set(jobs) - {"result"})
    (needs,) = re.findall(r"^    needs: \[(.*)\]$", jobs["result"], re.MULTILINE)
    assert sorted(needs.split(", ")) == others
    given = re.findall(r"^ {12}([a-z_]+)=\$\{\{ needs\.([a-z_]+)\.result \}\}$", CI, re.MULTILINE)
    assert sorted(given) == [(job, job) for job in others]
    script = (SCRIPTS / "ci_result.sh").read_text()
    (known,) = re.findall(r'^JOBS="(.*)"$', script, re.MULTILINE)
    assert sorted(known.split()) == others == sorted(_ALL)
    # Every job but the first waits for the scope, so none starts before it is known.
    for name in others:
        if name != "changes":
            assert "    needs: changes\n" in jobs[name], name


def test_every_workflow_runs_its_steps_with_pipefail():
    for name, text in WORKFLOWS.items():
        assert "\ndefaults:\n  run:\n    shell: bash\n" in text, name
        assert not re.search(r"^\s+shell:(?! bash$)", text, re.MULTILINE), name


@pytest.mark.slow
@pytest.mark.parametrize("issue", ["3702", "5509"])
def test_a_historical_case_script_fails_on_a_verdict_its_release_should_not_give(tmp_path, issue):
    import livekit.agents as lk

    source = (SCRIPTS / f"spike_{issue}.py").read_text()
    (fixed_in,) = re.findall(r"^FIXED_IN = \((\d+), (\d+), (\d+)\)", source, re.MULTILINE)
    installed = tuple(int(part) for part in lk.__version__.split(".")[:3])
    # The other side of the installed release, so that the verdict it gives is the wrong one.
    moved = "(99, 0, 0)" if installed >= tuple(map(int, fixed_in)) else "(0, 0, 0)"
    wrong = tmp_path / "wrong.py"
    wrong.write_text(source.replace(f"FIXED_IN = ({', '.join(fixed_in)})", f"FIXED_IN = {moved}"))
    for script, status in ((SCRIPTS / f"spike_{issue}.py", 0), (wrong, 1)):
        done = subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True, check=False
        )
        assert done.returncode == status, done.stderr
        assert '"distinct_hashes": 1' in done.stdout
    assert done.stderr.startswith(f"#{issue}: ")
    assert f"scripts/spike_{issue}.py evidence\n" in _jobs(CI)["determinism"]


def test_a_change_to_prose_alone_runs_every_test_file_that_reads_prose():
    reads = re.compile(r'"docs["/]|"(README|CHANGELOG|CONTRIBUTING|SECURITY|CODE_OF_CONDUCT)\.md"')
    readers = {
        path.name
        for path in (ROOT / "tests").glob("test_*.py")
        if reads.search(path.read_text()) and path != Path(__file__).resolve()
    }
    ran = set(re.findall(r" tests/(test_\w+\.py)$", _jobs(CI)["docs"], re.MULTILINE))
    assert readers == ran, "the docs job of ci.yml has to run each test file that reads prose"


def _executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture
def environment(tmp_path):
    """An environment, and a ``uv`` first on PATH, that say what a test tells them to."""
    _executable(tmp_path / "bin/uv", '#!/usr/bin/env bash\ncat "$STUB_HOLDS"\n')
    _executable(tmp_path / "env/bin/python", '#!/usr/bin/env bash\necho "$STUB_PYTHON"\n')
    holds = tmp_path / "holds"
    wheel = "earful @ file:///somewhere/earful-0.1.1-py3-none-any.whl"
    holds.write_text(f"attrs==26.1.0\n{wheel}\npytest==9.2.0\n")
    return {
        "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "STUB_HOLDS": str(holds),
        "STUB_PYTHON": "3.13",
    }


def _lock(tmp_path, env, mode):
    return _script(
        "env_lock.sh", str(tmp_path / "env"), str(tmp_path / "lock"), cwd=tmp_path, **env, **mode
    )


def test_a_lock_file_is_written_from_an_environment_and_compared_with_one(tmp_path, environment):
    lock = tmp_path / "lock"
    assert _lock(tmp_path, environment, {}).returncode == 0 and not lock.exists()
    assert _lock(tmp_path, environment, {"EARFUL_LOCK": "write"}).returncode == 0
    assert lock.read_text() == "attrs==26.1.0\npytest==9.2.0\n"
    assert _lock(tmp_path, environment, {"EARFUL_LOCK": "check"}).returncode == 0

    lock.write_text("attrs==26.1.0\npytest==9.1.0\n")
    differs = _lock(tmp_path, environment, {"EARFUL_LOCK": "check"})
    assert differs.returncode == 1
    assert "-pytest==9.1.0\n+pytest==9.2.0\n" in differs.stdout
    assert "EARFUL_LOCK=write" in differs.stderr
    assert lock.read_text() == "attrs==26.1.0\npytest==9.1.0\n"
    lock.unlink()
    assert _lock(tmp_path, environment, {"EARFUL_LOCK": "check"}).returncode == 1


@pytest.mark.parametrize(
    "change",
    [
        {"EARFUL_LOCK": "write", "STUB_PYTHON": "3.11"},  # resolves to another set
        {"EARFUL_LOCK": "check", "STUB_PYTHON": "3.11"},
        {"EARFUL_LOCK": "update"},
    ],
)
def test_a_lock_file_is_refused_on_another_python_or_in_an_unknown_mode(
    tmp_path, environment, change
):
    refused = _lock(tmp_path, {**environment, **change}, {})
    assert refused.returncode == 2 and refused.stderr
    assert not (tmp_path / "lock").exists()


def test_a_resolve_date_means_the_same_day_on_every_machine():
    # uv reads a date in the local time zone, so each script that passes one names UTC.
    dated = [path for path in SCRIPTS.glob("*.sh") if "--exclude-newer" in path.read_text()]
    assert {path.name for path in dated} >= {"make_env.sh", "make_pipecat_env.sh"}
    for path in dated:
        assert "\nexport TZ=UTC " in path.read_text(), path.name
    for name, text in WORKFLOWS.items():
        assert "--exclude-newer" not in text, f"{name} has a date that belongs in a script"


def test_ci_compares_the_environments_it_builds_on_python_3_13_with_their_lock_files():
    jobs = _jobs(CI)
    assert "EARFUL_LOCK: ${{ matrix.python == '3.13' && 'check' || '' }}\n" in jobs["determinism"]
    assert "EARFUL_LOCK: ${{ matrix.cell.python == '3.13' && 'check' || '' }}\n" in jobs["pipecat"]
    for name in ("make_env.sh", "make_pipecat_env.sh"):
        script = (SCRIPTS / name).read_text()
        assert re.search(r'^\S*scripts/env_lock\.sh"? "\$env_dir" ', script, re.MULTILINE), name


_PASSES = "def test_one():\n    pass\n"
_SKIPPED = "import pytest\n\n\n@pytest.mark.skip\ndef test_two():\n    pass\n\n\n"
_SKIPS_ITSELF = 'import pytest\n\npytest.importorskip("no_such_framework")\n\n\n'
_TESTS = {
    "every test passes": _PASSES + "\n\ndef test_two():\n    pass\n",
    "a test is skipped": _SKIPPED + _PASSES,  # pytest itself exits 0
    "the file skips itself": _SKIPS_ITSELF + _PASSES,
    "a test fails": "def test_one():\n    assert False\n",
    "there is no test": "",
}


@pytest.mark.parametrize("name", _TESTS)
def test_a_run_counts_only_when_every_test_ran(tmp_path, name):
    (tmp_path / "test_these.py").write_text(_TESTS[name])
    done = _script("ran_every_test.sh", sys.executable, "test_these.py", cwd=tmp_path)
    if name == "every test passes":
        assert done.returncode == 0, done.stdout + done.stderr
        assert done.stdout.endswith("all 2 tests ran\n")
    else:
        assert done.returncode != 0, done.stdout
    if name == "a test is skipped":
        assert "1 passed, 1 skipped" in done.stdout and "skipped 1" in done.stderr


def test_the_pipecat_tests_run_where_a_skip_fails_the_job():
    jobs = _jobs(CI)
    ran = "scripts/ran_every_test.sh .venvs/pc-{}/bin/python tests/test_pipecat.py\n"
    assert ran.format("${{ matrix.pc.version }}") in jobs["pipecat"]
    assert ran.format("1.16.0") in jobs["docs"]
    assert ran.format("1.16.0") in (SCRIPTS / "check_all.sh").read_text()
    assert "pytest.importorskip(" in (ROOT / "tests" / "test_pipecat.py").read_text()


def test_the_deliberate_breaks_run_for_a_pull_request_that_changes_behaviour():
    jobs = _jobs(CI)
    assert "    if: needs.changes.outputs.mutants == 'true'\n" in jobs["mutants"]
    assert "    uses: ./.github/workflows/mutation.yml\n" in jobs["mutants"]
    mutation = WORKFLOWS["mutation.yml"]
    assert "\n  workflow_call:\n" in mutation and "\n  workflow_dispatch:\n" in mutation
    # One environment for the breaks, here and in CI: the one scripts/lint.sh builds.
    assert "run: scripts/make_lint_env.sh\n" in mutation
    assert ".venvs/lint/bin/python scripts/mutate.py\n" in mutation
    assert ".venvs/lint/bin/python scripts/mutate.py " in (SCRIPTS / "mutate.py").read_text()
    assert "scripts/make_lint_env.sh || exit 1\n" in (SCRIPTS / "lint.sh").read_text()


def test_every_action_is_pinned_to_a_commit_and_names_its_release():
    pins: dict[str, set[tuple[str, str]]] = {}
    for name, text in WORKFLOWS.items():
        used = re.findall(r"^\s+(?:- )?uses: (\S+)(.*)$", text, re.MULTILINE)
        assert used, name
        for action, comment in used:
            if action.startswith("./"):
                continue  # a workflow of this repository, at the commit that calls it
            repository, _, commit = action.partition("@")
            assert re.fullmatch(r"[0-9a-f]{40}", commit), f"{name}: {action}"
            assert re.fullmatch(r" # v\d+\.\d+\.\d+", comment), f"{name}: {action}{comment}"
            pins.setdefault(repository, set()).add((commit, comment))
    assert all(len(pinned) == 1 for pinned in pins.values()), pins
    dependabot = (ROOT / ".github" / "dependabot.yml").read_text()
    assert "  - package-ecosystem: github-actions\n    directory: /\n" in dependabot


def test_every_workflow_installs_one_version_of_uv_and_leaves_no_credentials_behind():
    versions = set()
    for name, text in WORKFLOWS.items():
        checkouts = text.count("uses: actions/checkout@")
        assert text.count("          persist-credentials: false\n") == checkouts, name
        installs = text.count("uses: astral-sh/setup-uv@")
        if installs:
            versions.update(re.findall(r'^env:\n  UV_VERSION: "(.+)"$', text, re.MULTILINE))
            assert text.count("          version: ${{ env.UV_VERSION }}\n") == installs, name
    assert len(versions) == 1 and re.fullmatch(r"\d+\.\d+\.\d+", versions.pop())
