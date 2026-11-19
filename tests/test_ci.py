"""The scripts CI decides with do what they say, and the workflows are wired to them."""

import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
WORKFLOWS = {path.name: path.read_text() for path in (ROOT / ".github/workflows").glob("*.yml")}
CI = WORKFLOWS["ci.yml"]
RELEASE = WORKFLOWS["release.yml"]

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
        ("SECURITY.md", PROSE),
        ("src/earful/cli.py", BEHAVIOUR),
        ("tests/test_docs.py", BEHAVIOUR),
        ("scripts/mutate.py", BEHAVIOUR),
        ("scripts/make_lint_env.sh", BEHAVIOUR),  # the environment the breaks run in
        (".github/workflows/mutation.yml", BEHAVIOUR),
        ("scripts/lint.sh", CODE),
        ("skills/earful/SKILL.md", CODE),  # Markdown that tests/test_examples.py runs
        ("examples/booking/agent.py", CODE),
        (".github/workflows/ci.yml", CODE),
        (".github/workflows/canary.yml", CODE),  # every workflow is read by tests/test_ci.py
        ("scripts/canary.sh", CODE),
        (".github/ISSUE_TEMPLATE/bug.yml", CODE),  # read by tests/test_docs.py
        (".pre-commit-config.yaml", CODE),
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
# Stands in for gh: answers the calls of scripts/ci_tested.sh and scripts/ci_passed.sh.
echo "$*" >>"$STUB_LOG"
case "$2" in
  */commits/*/pulls) [ "${STUB_PR_HEAD-}" != fail ] || exit 1; echo "${STUB_PR_HEAD-}" ;;
  */git/commits/*) echo "$STUB_HEAD_TREE" ;;
  */actions/workflows/ci.yml/runs*branch=main*)
    [ "${STUB_MAIN_RUNS-}" != fail ] || exit 1
    printf '%s' "${STUB_MAIN_RUNS-}"
    ;;
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


def _passed(repo, env):
    return _script("ci_passed.sh", _git(repo, "rev-parse", "HEAD"), cwd=repo, **env)


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch"])
def test_a_release_is_allowed_when_a_run_of_ci_on_main_passed_on_the_commit(repo, gh, event):
    runs = f"pull_request\n{event}\n"
    done = _passed(repo, {**gh, "STUB_PR_HEAD": "", "STUB_MAIN_RUNS": runs})
    assert done.returncode == 0, done.stderr
    asked = Path(gh["STUB_LOG"]).read_text().splitlines()[-1]
    head = _git(repo, "rev-parse", "HEAD")
    assert f"/ci.yml/runs?head_sha={head}&branch=main&status=success&" in asked
    assert asked.endswith(" --jq .workflow_runs[].event")


def test_a_release_is_allowed_for_files_that_passed_as_a_pull_request(repo, gh):
    # Whatever became of the run on main: a later push cancels one still in progress.
    done = _passed(repo, gh)
    assert done.returncode == 0, done.stderr
    assert "branch=main" not in Path(gh["STUB_LOG"]).read_text()


@pytest.mark.parametrize(
    "runs",
    [
        "",  # no run on main passed
        "pull_request\npull_request\n",  # ci passed only for pull requests
        "fail",  # gh could not ask
        '{"message": "Not Found"}\n',  # an answer that names no run
        "pushed\n",
    ],
)
def test_a_release_is_refused_unless_ci_passed_on_the_commit(repo, gh, runs):
    done = _passed(repo, {**gh, "STUB_PASSED": "0", "STUB_MAIN_RUNS": runs})
    assert done.returncode == 1 and done.stdout == ""
    assert _git(repo, "rev-parse", "HEAD") in done.stderr
    assert ("GitHub did not say" in done.stderr) == (runs == "fail")
    unnamed = _passed(repo, {**gh, "GITHUB_REPOSITORY": "", "STUB_MAIN_RUNS": "push\n"})
    assert unnamed.returncode == 1 and "no repository is named" in unnamed.stderr


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
    # Nothing else: each environment names a release and a date, which no update would move.
    assert dependabot.count("package-ecosystem:") == 1


def test_the_pre_commit_hooks_run_the_ruff_that_the_lint_script_runs():
    config = (ROOT / ".pre-commit-config.yaml").read_text()
    assert "  - repo: https://github.com/astral-sh/ruff-pre-commit\n" in config
    ((_, hooked),) = re.findall(r"^    rev: ([0-9a-f]{40}) # frozen: v(\S+)$", config, re.MULTILINE)
    project = (ROOT / "pyproject.toml").read_text()
    (pinned,) = re.findall(r'^lint = \[.*"ruff==([\d.]+)"', project, re.MULTILINE)
    assert hooked == pinned
    assert re.findall(r"^      - id: (\S+)$", config, re.MULTILINE) == ["ruff-check", "ruff-format"]
    assert config.count("rev:") == 1 and "args:" not in config  # the settings are pyproject's


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


def test_a_release_takes_its_notes_from_its_section_of_the_changelog(tmp_path):
    def section(version, *changelog):
        done = _script("changelog_section.sh", version, *changelog, cwd=tmp_path)
        return done.returncode, done.stdout

    status, notes = section("0.1.1")
    assert status == 0 and notes.startswith("### Added\n\n- `earful init module:Class`")
    assert "## [" not in notes and "]: https://" not in notes and not notes.endswith("\n\n")
    status, notes = section("0.1.0")  # the last section stops before the link definitions
    assert status == 0 and notes.startswith("The first release.\n")
    assert notes.endswith("JSON Schema.\n")

    small = tmp_path / "CHANGELOG.md"
    small.write_text(
        "# Changelog\n\n## [Unreleased]\n\n## [1.10.0] - 2027-01-02\n\n- Ten.\n\n"
        "## [1.1.0] - 2027-01-01\n\n- One.\n\n[1.1.0]: https://example.invalid/1.1.0\n"
    )
    assert section("1.1.0", str(small)) == (0, "- One.\n")
    assert section("1.10.0", str(small)) == (0, "- Ten.\n")
    for absent in ("1.1", "2.0.0", "Unreleased"):  # no such section, or nothing in it
        assert section(absent, str(small)) == (1, "")


def _needs(job):
    (needs,) = re.findall(r"^    needs: (.+)$", job, re.MULTILINE)
    return needs.strip("[]").split(", ")


def test_a_tag_is_released_only_from_main_and_only_when_ci_passed_on_its_commit():
    assert '\non:\n  push:\n    tags: ["v*"]\n\npermissions:\n  contents: read\n' in RELEASE
    jobs = _jobs(RELEASE)
    assert list(jobs) == ["allowed", "build", "check", "publish", "notes"]
    allowed = jobs["allowed"]
    assert "          fetch-depth: 0 " in allowed  # ci_tested.sh reads the commit's tree
    assert "git merge-base --is-ancestor HEAD origin/main" in allowed
    assert '        run: scripts/changelog_section.sh "${GITHUB_REF_NAME#v}"\n' in allowed
    assert '        run: scripts/ci_passed.sh "$(git rev-parse HEAD)"\n' in allowed
    assert "      pull-requests: read " in allowed and "      actions: read " in allowed
    assert _needs(jobs["build"]) == ["allowed"]
    assert _needs(jobs["notes"]) == ["publish"]
    notes = _run_block(
        jobs["notes"],
        "Create the release, with the version's section of the changelog as its notes",
    )
    assert 'scripts/changelog_section.sh "${GITHUB_REF_NAME#v}" > "$RUNNER_TEMP/notes.md"' in notes
    assert 'gh release create "$GITHUB_REF_NAME" --verify-tag ' in notes
    assert [name for name, job in jobs.items() if "contents: write" in job] == ["notes"]


def test_a_release_builds_once_and_publishes_the_files_it_checked():
    jobs = _jobs(RELEASE)
    assert RELEASE.count("uv build") == 1
    assert "        run: uv build --out-dir dist\n" in jobs["build"]
    assert "      sums: ${{ steps.sums.outputs.sums }}\n" in jobs["build"]
    assert _needs(jobs["check"]) == ["build"]
    assert _needs(jobs["publish"]) == ["build", "check"]
    taken = "download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093 # v4.3.0\n"
    taken += "        with:\n          name: dist\n          path: dist\n"
    for name in ("check", "publish"):
        assert jobs[name].count(taken) == 1, name
        assert "          SUMS: ${{ needs.build.outputs.sums }}\n" in jobs[name], name
    compared = _run_block(jobs["check"], "The files are the ones that were built")
    assert compared == _run_block(
        jobs["publish"], "The files are the ones that were built and checked"
    )
    assert "        run: scripts/wheel_smoke.sh dist\n" in jobs["check"]
    assert "        run: scripts/quickstart_check.sh dist\n" in jobs["check"]
    # The job that publishes runs nothing of the repository, and it alone can sign in to PyPI.
    publish = jobs["publish"]
    assert "    environment: pypi\n    permissions:\n      id-token: write " in publish
    assert [name for name, job in jobs.items() if "id-token" in job] == ["publish"]
    assert "actions/checkout@" not in publish and publish.count(" run: ") == 1
    assert publish.index("shasum ") < publish.index("uses: pypa/gh-action-pypi-publish@")


def test_built_files_that_differ_from_the_sums_of_the_build_stop_a_release(tmp_path):
    jobs = _jobs(RELEASE)
    (tmp_path / "record.sh").write_text(
        _run_block(jobs["build"], "Record the SHA-256 of each file")
    )
    (tmp_path / "compare.sh").write_text(
        _run_block(jobs["publish"], "The files are the ones that were built and checked")
    )
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "earful-1.0.0-py3-none-any.whl").write_text("a wheel")
    (dist / "earful-1.0.0.tar.gz").write_text("a source distribution")
    env = {**os.environ, "RUNNER_TEMP": str(tmp_path), "GITHUB_OUTPUT": str(tmp_path / "output")}
    subprocess.run([*_SHELLS[0], "record.sh"], cwd=tmp_path, env=env, check=True)
    opened, _, sums = (tmp_path / "output").read_text().partition("\n")
    assert opened == "sums<<END" and sums.endswith("\nEND\n") and sums.count("\n") == 3
    sums = sums[: -len("\nEND\n")]

    def compare(sums):
        done = subprocess.run(
            [*_SHELLS[0], "compare.sh"],
            cwd=tmp_path,
            env={**env, "SUMS": sums},
            capture_output=True,
            check=False,  # the status is what is asserted
        )
        return done.returncode

    assert compare(sums) == 0
    assert compare("") != 0
    assert compare(sums.partition("\n")[0]) != 0  # a file the build did not record
    (dist / "earful-1.0.0.tar.gz").write_text("another source distribution")
    assert compare(sums) != 0
    (dist / "earful-1.0.0.tar.gz").unlink()
    assert compare(sums) != 0


@pytest.fixture
def uv_log(tmp_path):
    """A ``uv`` first on PATH that records each call, and fails one that would install."""
    _executable(
        tmp_path / "bin/uv", '#!/usr/bin/env bash\necho "$*" >>"$STUB_LOG"\n[ "$1" != pip ]\n'
    )
    (tmp_path / "uv.log").touch()
    return {
        "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "STUB_LOG": str(tmp_path / "uv.log"),
    }


def _calls(uv_log):
    """The calls of ``uv`` since this was last asked."""
    log = Path(uv_log["STUB_LOG"])
    calls = log.read_text().splitlines()
    log.write_text("")
    return calls


def test_the_wheel_check_takes_built_files_from_a_directory_and_builds_none(tmp_path, uv_log):
    project = (ROOT / "pyproject.toml").read_text()
    (version,) = re.findall(r'^version = "(.+)"$', project, re.MULTILINE)
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / f"earful-{version}-py3-none-any.whl"
    other = dist / "earful-0.0.1.tar.gz"

    def check():
        return _script("wheel_smoke.sh", str(dist), cwd=tmp_path, **uv_log)

    wheel.touch()
    other.touch()  # a source distribution of another version
    refused = check()
    assert refused.returncode == 1 and f"the sdist of {version} and nothing else" in refused.stderr
    (dist / f"earful-{version}.tar.gz").touch()
    refused = check()
    assert refused.returncode == 1 and other.name in refused.stderr  # a third file
    other.unlink()
    with zipfile.ZipFile(wheel, "w"):
        pass  # a wheel that holds nothing
    examined = check()
    assert examined.returncode == 1
    assert "the wheel does not hold earful/py.typed" in examined.stderr
    assert _calls(uv_log) == []

    built = _script("wheel_smoke.sh", cwd=tmp_path, **uv_log)
    assert built.returncode != 0  # the stand-in built nothing
    (call,) = _calls(uv_log)
    assert call.startswith("build -q --out-dir ") and call.endswith(f" {ROOT}")


def test_the_quickstart_check_installs_the_wheel_of_a_directory_it_is_given(tmp_path, uv_log):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "earful-0.0.0-py3-none-any.whl").touch()
    given = _script("quickstart_check.sh", str(dist), cwd=tmp_path, **uv_log)
    assert given.returncode != 0  # the stand-in installs nothing
    made, installed = _calls(uv_log)
    assert made.startswith("venv ")
    assert installed.startswith("pip install ")
    assert "/dist/earful-0.0.0-py3-none-any.whl[livekit] " in installed

    built = _script("quickstart_check.sh", cwd=tmp_path, **uv_log)
    assert built.returncode != 0
    (call,) = _calls(uv_log)
    assert call.startswith("build -q --wheel --out-dir ") and call.endswith(f" {ROOT}")


def test_the_canary_runs_each_night_and_on_request_and_no_pull_request_waits_for_it():
    canary = WORKFLOWS["canary.yml"]
    starts = '\non:\n  schedule:\n    - cron: "17 5 * * *" # an hour after the nightly run of ci\n'
    assert starts + "  workflow_dispatch:\n\npermissions:\n  contents: read\n" in canary
    assert not re.search(r"pull_request|push|workflow_call", canary)
    assert canary.count("permissions:") == 1  # no job is given more than the reading above
    for name, text in WORKFLOWS.items():
        assert name == "canary.yml" or "canary" not in text, name  # no workflow calls it
    jobs = _jobs(canary)
    assert list(jobs) == ["newest", "beyond"]
    assert (
        "      fail-fast: false\n      matrix:\n        framework: [livekit, pipecat]\n"
        in (jobs["newest"])
    )
    assert "        run: scripts/canary.sh ${{ matrix.framework }}\n" in jobs["newest"]
    assert "        run: scripts/newer_release.sh\n" in jobs["beyond"]
    # Nothing a run finds comes from an earlier run, and nothing in it names a date.
    assert canary.count("          enable-cache: false ") == canary.count("astral-sh/setup-uv@")
    for name in ("canary.sh", "newer_release.sh"):
        assert "--exclude-newer" not in (SCRIPTS / name).read_text(), name


_CANARY_UV = """#!/usr/bin/env bash
# Stands in for uv: an environment it makes has a python that records how it was called.
echo "uv $*" >>"$STUB_LOG"
case "$1 $2" in
  "venv -q")
    mkdir -p "$5/bin"
    cp "$(dirname "$0")/its-python" "$5/bin/python"
    ;;
  "pip freeze") printf 'attrs==26.1.0\\nlivekit-agents==1.8.13\\npipecat-ai==1.16.2\\n' ;;
esac
"""
_RECORDS = '#!/usr/bin/env bash\necho "{} $*" >>"$STUB_LOG"\nexit "$STUB_TESTS"\n'


@pytest.fixture
def canary(tmp_path):
    """A copy of ``scripts/canary.sh`` in a project of its own, and what it runs there."""
    _executable(tmp_path / "bin/uv", _CANARY_UV)
    _executable(tmp_path / "bin/its-python", _RECORDS.format("python"))
    _executable(tmp_path / "scripts/canary.sh", (SCRIPTS / "canary.sh").read_text())
    _executable(tmp_path / "scripts/ran_every_test.sh", _RECORDS.format("ran_every_test.sh"))
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "STUB_LOG": str(tmp_path / "log"),
    }

    def run(*args, tests=0):
        (tmp_path / "log").write_text("")
        done = subprocess.run(
            [str(tmp_path / "scripts/canary.sh"), *args],
            cwd=tmp_path / "scripts",
            env={**env, "STUB_TESTS": str(tests)},
            capture_output=True,
            text=True,
            check=False,  # the tests assert on the exit status
        )
        return done, (tmp_path / "log").read_text().splitlines()

    return run


def test_the_canary_installs_what_an_extra_admits_today_and_runs_the_fast_tests(canary, tmp_path):
    yesterday = tmp_path / ".venvs/canary-livekit/lib/yesterday"
    yesterday.parent.mkdir(parents=True)
    yesterday.touch()
    done, calls = canary("livekit")
    assert done.returncode == 0, done.stderr
    assert not yesterday.exists()
    assert calls == [
        "uv venv -q --python 3.13 .venvs/canary-livekit",
        "uv pip install -q -e .[livekit] pytest pytest-asyncio",  # no release and no date
        "uv pip freeze -q --exclude-editable",
        "python -m pytest -q -m not slow -rs",
    ]
    assert "=== what an install holds today: livekit-agents==1.8.13\nattrs==26.1.0\n" in done.stdout

    done, calls = canary("pipecat")
    assert done.returncode == 0, done.stderr
    assert calls[1] == "uv pip install -q -e .[pipecat] pytest pytest-asyncio"
    # A Pipecat test that is skipped fails the run, as it does in ci.
    ran = "ran_every_test.sh .venvs/canary-pipecat/bin/python tests/test_pipecat.py -m not slow"
    assert calls[3:] == [ran]
    assert "=== what an install holds today: pipecat-ai==1.16.2\n" in done.stdout


@pytest.mark.parametrize("framework", ["livekit", "pipecat"])
def test_the_canary_fails_when_a_test_fails_and_refuses_a_framework_it_does_not_know(
    canary, framework
):
    done, calls = canary(framework, tests=1)
    assert done.returncode == 1 and len(calls) == 4
    for unknown in ([], ["both"]):
        done, calls = canary(*unknown)
        assert done.returncode == 2 and "usage: " in done.stderr and calls == []


_RESOLVES = """#!/usr/bin/env bash
# Stands in for uv pip compile: within an extra it resolves to one set, and without to another.
echo "$*" >>"$STUB_LOG"
case "$*" in
  *" --extra "*) printf '%b' "$STUB_ADMITTED" ;;
  *) printf '%b' "$STUB_NEWEST" ;;
esac
"""


def _newer(tmp_path, admitted, newest):
    _executable(tmp_path / "bin/uv", _RESOLVES)
    (tmp_path / "log").write_text("")
    env = {
        "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "STUB_LOG": str(tmp_path / "log"),
        "STUB_ADMITTED": admitted,
        "STUB_NEWEST": newest,
    }
    done = _script("newer_release.sh", cwd=tmp_path, **env)
    return done, (tmp_path / "log").read_text().splitlines()


def test_a_release_newer_than_an_extra_admits_is_named_and_fails_the_check(tmp_path):
    admitted = "jsonschema==4.26.0\\nlivekit-agents==1.8.13\\npipecat-ai==1.16.2\\n"
    done, calls = _newer(tmp_path, admitted, "livekit-agents==1.8.13\\npipecat-ai==1.16.2\\n")
    assert done.returncode == 0, done.stderr
    assert "livekit-agents 1.8.13 is the newest release, and earful[livekit] admits it\n" in (
        done.stdout
    )
    asked = "pip compile -q --no-deps --no-header --no-annotate --python-version 3.13 "
    assert calls == [
        asked + "--extra livekit pyproject.toml",
        asked + "-",
        asked + "--extra pipecat pyproject.toml",
        asked + "-",
    ]
    project = (ROOT / "pyproject.toml").read_text()
    assert '\nlivekit = ["livekit-agents>=' in project and '\npipecat = ["pipecat-ai>=' in project

    done, _ = _newer(tmp_path, admitted, "livekit-agents==1.8.13\\npipecat-ai==1.17.0\\n")
    assert done.returncode == 1
    assert done.stdout.count("\n") == 1 and "livekit-agents 1.8.13 is the newest" in done.stdout
    assert done.stderr == (
        "pipecat-ai 1.17.0 is on PyPI, and earful[pipecat] admits nothing after 1.16.2\n"
    )
    done, _ = _newer(tmp_path, admitted, "livekit-agents==1.9.0\\npipecat-ai==1.17.0\\n")
    assert done.returncode == 1 and done.stdout == "" and done.stderr.count(" is on PyPI, ") == 2


@pytest.mark.parametrize(
    ("admitted", "newest"),
    [("", "livekit-agents==1.8.13\\n"), ("livekit-agents==1.8.13\\n", "")],
)
def test_the_check_for_a_newer_release_fails_when_uv_names_no_release(tmp_path, admitted, newest):
    done, _ = _newer(tmp_path, admitted, newest)
    assert done.returncode == 2 and "uv did not say which livekit-agents" in done.stderr
