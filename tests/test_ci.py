"""The scripts CI decides with do what they say, and the workflows are wired to them."""

import json
import os
import re
import shutil
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


def _jobs(workflow):
    """The text of each job of ``workflow``, by its key."""
    parts = re.split(r"^  ([a-z_]+):\n", workflow.partition("\njobs:\n")[2], flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


CHECKS = WORKFLOWS["checks.yml"]


def _ci_scope(repo, *args, **env):
    """Run ``scripts/ci_scope.py`` in ``repo``, as the scope job does."""
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "ci_scope.py"), *args],
        cwd=repo,
        env={**os.environ, **_GIT, "GITHUB_STEP_SUMMARY": "", **env},
        capture_output=True,
        text=True,
        check=False,  # the tests assert on the exit status
    )


def _scope(repo, **env):
    """What the scope job hands the other jobs, as a dict, and its exit status."""
    done = _ci_scope(repo, "--github", **{"TIER": "pull-request", **env})
    return dict(line.split("=", 1) for line in done.stdout.splitlines()), done.returncode


def _due(repo, **env):
    outputs, status = _scope(repo, **env)
    assert status == 0, outputs
    return json.loads(outputs["due"])


CORE_JOBS = ["lint", "wheel", "quickstart", "livekit", "pipecat", "control", "breaks"]
EVERY_JOB = ["lint", "wheel", "quickstart", "report", "livekit", "pipecat"]
EVERY_JOB += ["control", "breaks"]


def test_a_pull_request_is_scoped_by_what_its_merge_brings_to_the_base(repo):
    _git(repo, "checkout", "-q", "-b", "topic")
    _commit(repo, {"docs/page.md": "from the branch\n"})
    _git(repo, "checkout", "-q", "main")
    _commit(repo, {"src/earful/cli.py": "a = 3  # on main meanwhile\n"})
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "topic")
    assert _due(repo, EVENT="pull_request") == ["prose"]


def test_a_new_base_is_checked_only_once_github_merged_the_pull_request_with_it(repo):
    _git(repo, "checkout", "-q", "-b", "topic")
    _commit(repo, {"docs/page.md": "from the branch\n"})
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "topic")
    base = _git(repo, "rev-parse", "HEAD^1")
    assert _due(repo, EVENT="pull_request", ACTION="edited", BASE_SHA=base) == ["prose"]
    outputs, status = _scope(repo, EVENT="pull_request", ACTION="edited", BASE_SHA="f" * 40)
    assert status != 0 and outputs == {}
    refused = _ci_scope(repo, "--github", TIER="pull-request", EVENT="pull_request")
    assert refused.returncode == 0  # an event that is not an edit is not compared


def test_the_full_tier_runs_everything_whatever_changed(repo):
    for event in ("schedule", "workflow_dispatch", "push", "pull_request"):
        assert _due(repo, TIER="full", EVENT=event) == EVERY_JOB


@pytest.mark.parametrize(
    "env",
    [{"TIER": "nightly", "EVENT": "push"}, {"TIER": "", "EVENT": "push"}, {"EVENT": "schedule"}],
)
def test_a_tier_or_an_event_the_scope_does_not_know_is_refused(repo, env):
    outputs, status = _scope(repo, **env)
    assert status != 0 and outputs == {}


def test_a_base_that_cannot_be_read_runs_everything(repo):
    # The first commit has no parent to compare a pull request's merge with.
    assert _due(repo, EVENT="pull_request") == EVERY_JOB


def test_a_diff_that_fails_fails_the_scope_and_prints_nothing(repo):
    done = _ci_scope(repo, "--explain", "HEAD", _NO_COMMIT)
    assert done.returncode != 0 and done.stdout == ""


def test_the_explanation_names_each_path_its_rule_and_what_it_runs(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, {"tests/test_lk.py": 'import pytest\n\npytest.importorskip("livekit.agents")\n'})
    head = _commit(repo, {"docs/page.md": "new\n"})
    done = _ci_scope(repo, "--explain", base, head)
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert lines[:4] == [
        "Tier: pull-request.",
        "2 changed paths:",
        "  docs/page.md      prose  (docs/**)",
        "  tests/test_lk.py  livekit  (tests/*.py)",
    ]
    assert "Runs:" in lines and "Left to the full tier:" in lines


def test_a_deleted_file_counts_as_what_it_held(repo):
    _commit(repo, {"tests/test_pc.py": 'import pytest\n\npytest.importorskip("pipecat")\n'})
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, {"tests/test_pc.py": None})
    done = _ci_scope(repo, "--explain", base, head)
    assert "  tests/test_pc.py  pipecat  (tests/*.py)\n" in done.stdout


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
_SCOPE_STEP = "Decide from the tier, the event and the changed paths (scripts/ci_scope.py)"


@pytest.mark.parametrize("shell", _SHELLS, ids=["shell named", "no shell named"])
@pytest.mark.parametrize("scope_script", ["exit(3)", "print('lint=true'); exit(3)"])
def test_the_scope_step_fails_and_sets_no_output_when_its_script_fails(
    tmp_path, shell, scope_script
):
    step = _run_block(_jobs(CHECKS)["scope"], _SCOPE_STEP)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/ci_scope.py").write_text(f"{scope_script}\n")
    (tmp_path / "step.sh").write_text(step)
    output = tmp_path / "output"
    output.touch()
    env = {**os.environ, "RUNNER_TEMP": str(tmp_path), "GITHUB_OUTPUT": str(output)}
    done = subprocess.run([*shell, "step.sh"], cwd=tmp_path, env=env, check=False)
    assert done.returncode != 0
    assert output.read_text() == ""


def test_the_scope_step_hands_the_scope_to_the_jobs(tmp_path, repo):
    step = _run_block(_jobs(CHECKS)["scope"], _SCOPE_STEP)
    (tmp_path / "step.sh").write_text(step)
    output = tmp_path / "output"
    env = {
        **os.environ,
        "TIER": "full",
        "EVENT": "schedule",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(output),
        "GITHUB_STEP_SUMMARY": "",
    }
    shell = [*_SHELLS[0], str(tmp_path / "step.sh")]
    work = tmp_path / "checkout"
    shutil.copytree(repo, work)
    (work / "scripts").symlink_to(SCRIPTS)
    subprocess.run(shell, cwd=work, env=env, check=True)
    handed = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert json.loads(handed["due"]) == EVERY_JOB and json.loads(handed["explained"])
    assert "      TIER: ${{ inputs.tier }}\n" in _jobs(CHECKS)["scope"]


def test_every_workflow_runs_its_steps_with_pipefail():
    for name, text in WORKFLOWS.items():
        assert "\ndefaults:\n  run:\n    shell: bash\n" in text, name
        assert not re.search(r"^\s+shell:(?! bash$)", text, re.MULTILINE), name


_TOKEN = re.compile(r"\s*(?:('[^']*')|(\d+)|(==|!=|&&|\|\||!|\(|\)|,)|([A-Za-z_][\w.*-]*))")


class _Expression:
    """Evaluates the part of GitHub's expression language that ci.yml uses: literals,
    contexts, ==, !=, !, &&, ||, format() and contains()."""

    def __init__(self, text, context):
        found = list(_TOKEN.finditer(text.strip()))
        assert sum(len(m.group()) for m in found) == len(text.strip()), text  # all of it read
        self.tokens = [next(t for t in m.groups() if t) for m in found]
        self.context, self.at = context, 0

    def value(self):
        found = self._or()
        assert self.at == len(self.tokens), self.tokens[self.at :]
        return found

    def _take(self, token=None):
        got = self.tokens[self.at]
        assert token is None or got == token, (got, token)
        self.at += 1
        return got

    def _peek(self):
        return self.tokens[self.at] if self.at < len(self.tokens) else None

    def _or(self):
        left = self._and()
        while self._peek() == "||":
            self._take()
            right = self._and()
            left = left or right
        return left

    def _and(self):
        left = self._equal()
        while self._peek() == "&&":
            self._take()
            right = self._equal()
            left = left and right
        return left

    def _equal(self):
        left = self._not()
        while self._peek() in ("==", "!="):
            same = self._take() == "=="
            right = self._not()
            left = (left == right) == same
        return left

    def _not(self):
        if self._peek() == "!":
            self._take()
            return not self._not()
        return self._primary()

    def _primary(self):
        token = self._take()
        if token == "(":
            inner = self._or()
            self._take(")")
            return inner
        if token.startswith("'"):
            return token[1:-1]
        if token.isdigit():
            return int(token)
        if self._peek() == "(":
            self._take()
            args = [self._or()]
            while self._peek() == ",":
                self._take()
                args.append(self._or())
            self._take(")")
            if token == "format":
                return args[0].format(*args[1:])
            assert token == "contains", token
            return args[1] in (args[0] or [])
        return self._lookup(token)

    def _lookup(self, path):
        found = self.context
        for part in path.split("."):
            if part == "*":
                continue  # the parts after it are read from each item of the list
            if isinstance(found, list):
                found = [item.get(part) for item in found]
            else:
                found = found.get(part) if isinstance(found, dict) else None
        return found


def _block(text, key):
    """The value of ``key``, indented as given, with a folded scalar joined into one line."""
    deeper = re.escape(" " * (len(key) - len(key.lstrip()) + 2))
    pattern = rf"^{re.escape(key)}: (?:>-\n((?:{deeper}.*\n)+)|(.*)\n)"
    found = re.search(pattern, text, re.MULTILINE)
    assert found, key
    if found.group(1):
        return " ".join(line.strip() for line in found.group(1).splitlines())
    return found.group(2)


def _evaluate(text, context):
    """``text`` with each ${{ }} replaced by its value; a condition without one is evaluated."""
    if "${{" not in text:
        return _Expression(text, context).value()
    return re.sub(r"\$\{\{(.*?)\}\}", lambda m: str(_Expression(m.group(1), context).value()), text)


def _pull_request(action, labels=(), **event):
    pull = {"number": 7, "labels": [{"name": name} for name in labels]}
    return {
        "event_name": "pull_request",
        "run_id": 99,
        "event": {"action": action, "pull_request": pull, **event},
    }


_EVENTS = {
    "a push to main": ({"event_name": "push", "run_id": 99, "event": {}}, "ci-99", "pull-request"),
    "the night": ({"event_name": "schedule", "run_id": 99, "event": {}}, "ci-99", "full"),
    "a request": ({"event_name": "workflow_dispatch", "run_id": 99, "event": {}}, "ci-99", "full"),
    "a pull request opened": (_pull_request("opened"), "ci-pull-request-7", "pull-request"),
    "a push to it": (_pull_request("synchronize"), "ci-pull-request-7", "pull-request"),
    "with the label": (_pull_request("synchronize", ["full ci"]), "ci-pull-request-7", "full"),
    "a new base": (
        _pull_request("edited", changes={"base": {"ref": {"from": "topic"}}}),
        "ci-pull-request-7",
        "pull-request",
    ),
    "the label put on": (
        _pull_request("labeled", ["full ci"], label={"name": "full ci"}),
        "ci-pull-request-7",
        "full",
    ),
    "a new title": (_pull_request("edited", changes={"title": {"from": "x"}}), "ci-99", None),
    "another label": (_pull_request("labeled", ["bug"], label={"name": "bug"}), "ci-99", None),
    "closing it": (_pull_request("closed"), "ci-pull-request-7", None),
}


@pytest.mark.parametrize("name", _EVENTS)
def test_each_event_runs_the_tier_it_asks_for_in_a_group_that_cancels_what_it_replaces(name):
    github, group, tier = _EVENTS[name]
    context = {"github": github}
    assert _evaluate(_block(CI, "  group"), context) == group
    assert _block(CI, "  cancel-in-progress") == "true"
    job = _jobs(CI)["ci"]
    runs = _evaluate(_block(job, "    if"), context)
    assert bool(runs) == (tier is not None)
    if tier:
        assert _evaluate(_block(job, "      tier"), context) == tier


def test_ci_starts_on_each_event_of_a_pull_request_that_changes_what_it_checks():
    assert "    types: [opened, synchronize, reopened, edited, labeled, closed]\n" in CI
    assert '    - cron: "17 4 * * *"\n' in CI and "\n  workflow_dispatch:\n" in CI
    job = _jobs(CI)["ci"]
    assert "    name: ci\n" in job and "    uses: ./.github/workflows/checks.yml\n" in job
    # The check main requires is the called workflow's last job, "ci / result": a skipped
    # caller leaves a check named "ci" alone, and never a skipped "ci / result".
    assert "\n  result:\n    name: result\n" in CHECKS
    assert set(_jobs(CI)) == {"ci"}
    assert "\n  workflow_call:\n" in CHECKS and not re.search(
        r"^  (push|pull_request|schedule):", CHECKS, re.MULTILINE
    )


_GH = """#!/usr/bin/env bash
# Stands in for gh: answers the calls of scripts/ci_tested.sh.
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
    assert _due(repo, EVENT="push", BEFORE=before, **tested) == []
    assert _due(repo, EVENT="push", BEFORE=before, **gh) == CORE_JOBS
    assert _due(repo, EVENT="push", BEFORE=_NO_COMMIT, **gh) == EVERY_JOB
    assert _due(repo, EVENT="push", **gh) == EVERY_JOB


@pytest.mark.parametrize("issue", ["3702", "5509"])
def test_ci_runs_the_script_of_each_historical_case(issue):
    assert f"scripts/spike_{issue}.py evidence\n" in _jobs(CHECKS)["livekit"]


def test_a_change_to_prose_alone_runs_every_test_file_that_reads_prose():
    reads = re.compile(
        r'"docs["/]|"(README|CHANGELOG|CONTRIBUTING|SECURITY|CODE_OF_CONDUCT)\.md"'
        r"|^from test_docs import ",  # the pages as test_docs.py read them
        re.MULTILINE,
    )
    readers = {
        path.name
        for path in (ROOT / "tests").glob("test_*.py")
        if reads.search(path.read_text()) and path != Path(__file__).resolve()
    }
    ran = set(re.findall(r" tests/(test_\w+\.py)$", _jobs(CHECKS)["prose"], re.MULTILINE))
    assert readers == ran, "the prose job of checks.yml has to run each test file that reads prose"


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
    jobs = _jobs(CHECKS)
    for name in ("livekit", "pipecat"):
        assert "EARFUL_LOCK: ${{ matrix.cell.python == '3.13' && 'check' || '' }}\n" in jobs[name]
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


def _wants(module, then=_PASSES):
    return f'import pytest\n\npytest.importorskip("{module}")\n\n\n' + then


def test_a_file_that_skips_itself_for_the_framework_named_absent_is_left_out(tmp_path):
    def run(*without):
        return _script("ran_every_test.sh", *without, sys.executable, cwd=tmp_path)

    (tmp_path / "test_core.py").write_text(_PASSES)
    (tmp_path / "test_adapter.py").write_text(_wants("no_such_framework.agents"))
    done = run("--without", "no_such_framework")
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.endswith("all 1 tests ran, 1 files need no_such_framework\n")
    assert run().returncode == 1  # unless the framework is named, the skip fails the run
    # Any other skip still fails: of a test, for another module, or inside a test.
    for name, text in {
        "a test is skipped": _SKIPPED + _PASSES,
        "another module is absent": _wants("no_such_framework_plugins"),
        "a test asks for the framework": _wants("pytest", "def test_two():\n")
        + '    pytest.importorskip("no_such_framework.agents")\n',
    }.items():
        (tmp_path / "test_other.py").write_text(text)
        done = run("--without", "no_such_framework")
        assert done.returncode == 1 and "skipped 1: here every test" in done.stderr, name
    done = run("--without", "a framework")
    assert done.returncode == 2 and "takes the name of a module" in done.stderr


def test_the_reference_ledgers_of_both_frameworks_are_repeated_and_compared():
    repeated = "bin/python scripts/repeat.py\n          --check tests/golden --out evidence\n"
    jobs = _jobs(CHECKS)
    assert ".venvs/lk-${{ matrix.cell.version }}/" + repeated in jobs["livekit"]
    assert ".venvs/pc-${{ matrix.cell.version }}/" + repeated in jobs["pipecat"]
    everything = (SCRIPTS / "check_all.sh").read_text()
    pipecat = ".venvs/pc-1.16.0/bin/python scripts/repeat.py"
    for command in (
        '".venvs/lk-$1/bin/python" scripts/repeat.py --update "$GOLDEN" | grep ',
        '"$py" scripts/repeat.py --check "$GOLDEN" | grep ',
        f'{pipecat} --update "$GOLDEN" | grep ',
        f'{pipecat} --check "$GOLDEN" | grep ',
    ):
        assert command in everything, command
    # Recorded before the Pipecat tests compare a run with them.
    tests = "scripts/ran_every_test.sh --without livekit .venvs/pc-1.16.0/bin/python\n"
    assert everything.index(f"{pipecat} --update") < everything.index(tests)


def test_where_livekit_is_absent_every_other_test_runs_and_a_skip_fails_the_job():
    jobs = _jobs(CHECKS)
    ran = "scripts/ran_every_test.sh --without livekit\n"
    assert ran + "          .venvs/pc-${{ matrix.cell.version }}/bin/python\n" in jobs["pipecat"]
    every = "scripts/ran_every_test.sh --without livekit .venvs/pc-1.16.0/bin/python\n"
    assert every in (SCRIPTS / "check_all.sh").read_text()
    # A change to prose alone runs the one Pipecat file that reads docs/.
    assert (
        "scripts/ran_every_test.sh .venvs/pc-1.16.0/bin/python tests/test_pipecat.py\n"
        in (jobs["prose"])
    )


def test_the_breaks_run_once_unbroken_and_in_parts_each_of_its_own_breaks():
    jobs = _jobs(CHECKS)
    since = '${SINCE:+--since "$SINCE"}'
    assert f".venvs/lint/bin/python scripts/mutate.py --control {since}\n" in jobs["control"]
    part = f'.venvs/lint/bin/python scripts/mutate.py --part "$PART" {since}\n'
    assert part in jobs["breaks"]
    for name in ("control", "breaks"):
        # One environment for the breaks, here and in CI: the one scripts/lint.sh builds.
        assert "        run: scripts/make_lint_env.sh\n" in jobs[name], name
        assert "          fetch-depth: 0 " in jobs[name], name  # --since reads the history
        assert "          SINCE: ${{ needs.scope.outputs.since }}\n" in jobs[name], name
    assert "        part: ${{ fromJSON(needs.scope.outputs.breaks) }}\n" in jobs["breaks"]
    assert "          PART: ${{ matrix.part.part }}/${{ matrix.part.of }}\n" in jobs["breaks"]
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


def test_a_tag_is_released_only_from_main_and_only_after_the_full_tier_passed_on_it():
    assert '\non:\n  push:\n    tags: ["v*"]\n\npermissions:\n  contents: read\n' in RELEASE
    jobs = _jobs(RELEASE)
    assert list(jobs) == ["allowed", "ci", "build", "check", "publish", "notes"]
    allowed = jobs["allowed"]
    assert "          fetch-depth: 0 " in allowed  # whether main holds the tagged commit
    assert "git merge-base --is-ancestor HEAD origin/main" in allowed
    assert '        run: scripts/changelog_section.sh "${GITHUB_REF_NAME#v}"\n' in allowed
    ci = jobs["ci"]
    assert "    uses: ./.github/workflows/checks.yml\n    with:\n      tier: full\n" in ci
    assert _needs(ci) == ["allowed"]
    assert _needs(jobs["build"]) == ["allowed", "ci"]
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
    taken = r"uses: actions/download-artifact@.+\n"
    taken += r"        with:\n          name: dist\n          path: dist\n"
    for name in ("check", "publish"):
        assert len(re.findall(taken, jobs[name])) == 1, name
        assert jobs[name].count("download-artifact@") == 1, name
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
        "uv pip install -q -e .[livekit] --group test",  # no release and no date
        "uv pip freeze -q --exclude-editable",
        "python -m pytest -q -m not slow -rs",
    ]
    assert "=== what an install holds today: livekit-agents==1.8.13\nattrs==26.1.0\n" in done.stdout

    done, calls = canary("pipecat")
    assert done.returncode == 0, done.stderr
    assert calls[1] == "uv pip install -q -e .[pipecat] --group test"
    # A test that is skipped fails the run, as it does in ci. The one test left out
    # compares a run with a reference ledger, which a newer release does not have.
    ran = "ran_every_test.sh --without livekit .venvs/canary-pipecat/bin/python -m not slow"
    left_out = "tests/test_pipecat_golden.py::test_pipecat_run_reproduces_the_committed_ledger"
    assert len(calls) == 4 and calls[3] == f"{ran} --deselect {left_out}"
    path, _, name = left_out.partition("::")
    assert f"\ndef {name}(" in (ROOT / path).read_text()
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
