"""The scripts CI decides with do what they say, and the workflows are wired to them."""

import os
import re
import subprocess
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


NOTHING = {"code": "false", "docs": "false"}
PROSE = {"code": "false", "docs": "true"}
CODE = {"code": "true", "docs": "false"}
EVERYTHING = {"code": "true", "docs": "true"}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("docs/pipecat.md", PROSE),
        ("docs/img/report-booking.png", PROSE),
        ("src/earful/cli.py", CODE),
        ("tests/test_docs.py", CODE),
        ("skills/earful/SKILL.md", CODE),
        (".github/workflows/ci.yml", CODE),
        ("pyproject.toml", CODE),
        ("README.md", NOTHING),
    ],
)
def test_a_changed_path_is_prose_or_code(repo, path, expected):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, {path: "changed\n"})
    assert _scope(repo, base, head) == (expected, 0)


def test_the_scope_of_several_paths_is_the_union(repo):
    base = _git(repo, "rev-parse", "HEAD")
    assert _scope(repo, base, _commit(repo, {})) == (NOTHING, 0)
    head = _commit(repo, {"docs/page.md": "new\n", "src/earful/cli.py": "a = 2\n"})
    assert _scope(repo, base, head) == (EVERYTHING, 0)


def test_a_deleted_or_moved_file_counts_where_it_was(repo):
    base = _git(repo, "rev-parse", "HEAD")
    assert _scope(repo, base, _commit(repo, {"src/earful/cli.py": None})) == (CODE, 0)
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


@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
def test_a_nightly_or_manual_run_checks_everything(repo, event):
    assert _scope(repo, "--event", event) == (EVERYTHING, 0)


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
    assert _scope(repo, "--event", "push", before, **gh) == (CODE, 0)
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
    assert output.read_text() == "code=true\ndocs=true\n"
    for name in ("code", "docs"):
        assert f"{name}: ${{{{ steps.scope.outputs.{name} }}}}" in jobs["changes"]
        assert f"{name.upper()}: ${{{{ needs.changes.outputs.{name} }}}}" in jobs["result"]


_ALL = ("changes", "docs", "lint", "wheel", "quickstart", "pipecat", "determinism")


def _result(code, docs, /, **results):
    """The exit status of ``scripts/ci_result.sh``; a job not named was skipped."""
    pairs = " ".join(f"{job}={results.pop(job, 'skipped')}" for job in _ALL)
    pairs += "".join(f" {job}={result}" for job, result in results.items())
    done = _script("ci_result.sh", cwd=ROOT, CODE=code, DOCS=docs, RESULTS=pairs)
    assert done.returncode in (0, 1), done.stderr
    return done.returncode


_RAN = dict.fromkeys(
    ("changes", "lint", "wheel", "quickstart", "pipecat", "determinism"), "success"
)


def test_the_result_passes_when_every_job_the_scope_asks_for_passed():
    assert _result("true", "false", **_RAN) == 0
    assert _result("true", "true", **_RAN) == 0
    assert _result("false", "true", changes="success", docs="success") == 0
    assert _result("false", "false", changes="success") == 0


@pytest.mark.parametrize(
    ("code", "docs", "results"),
    [
        ("", "", {"changes": "success"}),  # the scope step set no output
        ("true", "", _RAN),
        ("yes", "false", _RAN),
        ("true", "false", {**_RAN, "lint": "skipped"}),
        ("true", "false", {**_RAN, "determinism": "failure"}),
        ("true", "false", {**_RAN, "pipecat": "cancelled"}),
        ("true", "false", {**_RAN, "wheel": ""}),
        ("false", "true", {"changes": "success"}),  # the docs job did not run
        ("false", "false", {"changes": "success", "lint": "failure"}),
        ("false", "false", {}),  # the scope job itself was skipped
        ("false", "false", {"changes": "failure"}),
        ("true", "false", {**_RAN, "another": "success"}),  # a job the script has no rule for
    ],
)
def test_the_result_fails_otherwise(code, docs, results):
    assert _result(code, docs, **results) == 1


def test_the_result_fails_when_a_job_is_missing_from_what_it_is_given():
    pairs = " ".join(f"{job}=success" for job in _ALL if job != "determinism")
    done = _script("ci_result.sh", cwd=ROOT, CODE="true", DOCS="false", RESULTS=pairs)
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
