"""What each change runs before it merges, what the full tier runs, and the check main requires
(scripts/ci_scope.py, scripts/ci_result.py)."""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ci_result
import ci_scope
from ci_scope import LIVEKIT, OTHER_PYTHONS, PIPECAT

CHECKS = (ROOT / ".github" / "workflows" / "checks.yml").read_text()
OLDEST, *_, NEWEST = LIVEKIT
PC, *_, PC_NEWEST = PIPECAT


def lk(version, system="Linux", python="3.13"):
    return f"lk {version} / {system} / py{python}"


def pc(python, system="Linux", wheel=False, version=PC):
    return f"pc {version} / {system} / py{python}" + (" / the wheel" if wheel else "")


EVERY_LK = {lk(v) for v in LIVEKIT}
EDGES_LK = {lk(OLDEST), lk(NEWEST)}
EVERY_PC = {pc("3.13", version=v) for v in PIPECAT}
PC_311 = {pc("3.11", version=v) for v in PIPECAT}
PC_MAC = {pc("3.13", "macOS", version=v) for v in PIPECAT}
PC_WHEEL = {pc("3.12", wheel=True, version=v) for v in PIPECAT}
PROSE: tuple[set[str], set[str]] = ({"prose"}, set())
PIPECAT_ = ({"lint"}, EVERY_PC)
LIVEKIT_ = ({"lint", "quickstart"}, EDGES_LK)
CORE = ({"lint", "quickstart", "wheel"}, EDGES_LK | {pc("3.13", version=PC_NEWEST)})
OTHERS = {lk(v, python=py) for py, v in OTHER_PYTHONS.items()}
BUILD = (CORE[0], EDGES_LK | {pc("3.12", wheel=True, version=PC_NEWEST)})
BROAD = (CORE[0] | {"report", "bare"}, CORE[1])
FULL = (
    {"lint", "wheel", "quickstart", "report", "bare"},
    EVERY_LK | {lk(v, "macOS") for v in LIVEKIT} | OTHERS | EVERY_PC | PC_311 | PC_WHEEL | PC_MAC,
)


def _read(files=None):
    files = files or {}

    def read(path):
        if path in files:
            return files[path]
        return (ROOT / path).read_text() if (ROOT / path).is_file() else None

    return read


def _runs(*paths, files=None):
    """The jobs and the cells a change to ``paths`` runs."""
    result = ci_scope.plan("pull-request", list(paths), _read(files))
    return set(result.jobs), {cell.name for cell in result.cells}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("docs/pipecat.md", PROSE),
        ("docs/img/report-booking.png", PROSE),
        ("README.md", PROSE),
        ("CHANGELOG.md", PROSE),
        (".github/ISSUE_TEMPLATE/bug.yml", PROSE),
        (".github/pull_request_template.md", PROSE),
        ("src/earful/pipecat/harness.py", PIPECAT_),
        ("examples/pipecat_booking/bot.py", PIPECAT_),
        ("compat/pc-1.16.0.lock.txt", PIPECAT_),
        ("tests/golden/pc-1.16.0.greeting.ledger.normalized.jsonl", PIPECAT_),
        ("tests/test_pipecat.py", PIPECAT_),  # by the framework it declares
        ("tests/scaffolded_bots.py", PIPECAT_),  # by the framework it imports
        ("src/earful/livekit/harness.py", LIVEKIT_),
        ("examples/booking/agent.py", LIVEKIT_),
        ("skills/earful/SKILL.md", LIVEKIT_),  # Markdown that tests/test_examples.py runs
        ("tests/golden/lk-1.8.13.greeting.ledger.normalized.jsonl", LIVEKIT_),
        ("tests/bundles/lk-1.5.6.book_then_interrupt.A2-rule/manifest.json", LIVEKIT_),
        ("tests/test_golden.py", LIVEKIT_),
        ("tests/breaks.py", LIVEKIT_),
        ("compat/lk-1.6.5.lock.txt", (set(), {lk("1.6.5")})),
        ("compat/lk-9.9.9.lock.txt", (set(), EVERY_LK)),  # a release the table does not know
        ("src/earful/cli.py", CORE),
        ("src/earful/core/checker/m1.py", CORE),
        ("tests/conftest.py", CORE),
        ("tests/test_checker.py", CORE),
        ("src/earful/scripted/clock.py", CORE),
        ("src/earful/core/html.py", ({"lint", "report"}, {lk(NEWEST)})),
        ("pyproject.toml", BUILD),
        ("LICENSE", BUILD),
        ("tests/mutants.py", ({"lint"}, set())),
        ("tests/mutants_seconds.json", ({"lint"}, set())),
        ("releases.toml", BROAD),
        (".github/workflows/ci.yml", BROAD),  # a workflow, a script and anything unnamed
        (".github/workflows/checks.yml", BROAD),
        ("scripts/ci_scope.py", BROAD),
        ("scripts/make_env.sh", BROAD),
        (".pre-commit-config.yaml", BROAD),
        ("tools/new.py", BROAD),
    ],
)
def test_a_changed_path_runs_what_its_rule_selects(path, expected):
    assert _runs(path) == expected


def test_the_full_tier_runs_every_job_and_every_cell():
    result = ci_scope.plan("full", None)
    assert (set(result.jobs), {cell.name for cell in result.cells}) == FULL
    assert len(result.breaks) == len(ci_scope.mutate.MUTANTS) and result.left == []


def test_a_change_that_cannot_be_read_runs_everything():
    result = ci_scope.plan("pull-request", None)
    assert (set(result.jobs), {cell.name for cell in result.cells}) == FULL


def test_the_scope_of_several_paths_is_the_union():
    assert _runs() == (set(), set())
    assert _runs("docs/api.md", "compat/lk-1.6.5.lock.txt") == ({"prose"}, {lk("1.6.5")})
    both = _runs("src/earful/pipecat/sink.py", "src/earful/livekit/sink.py")
    assert both == ({"lint", "quickstart"}, EDGES_LK | EVERY_PC)


def test_the_tests_that_read_the_pages_have_a_job_only_where_no_cell_runs_them():
    assert _runs("docs/api.md", "src/earful/pipecat/sink.py")[0] == {"prose", "lint"}
    assert _runs("docs/api.md", "src/earful/livekit/sink.py")[0] == {"prose", "lint", "quickstart"}
    assert "prose" not in _runs("docs/api.md", "src/earful/cli.py")[0]


@pytest.mark.parametrize(
    ("text", "group"),
    [
        ('import pytest\n\npytest.importorskip("livekit.agents")\n', "livekit"),
        ('import pytest\n\npytest.importorskip("pipecat")\n', "pipecat"),
        ("import pytest\n", "core"),
        ('pytest.importorskip("livekit.agents")\npytest.importorskip("pipecat")\n', "broad"),
    ],
)
def test_a_test_file_selects_the_framework_it_declares(text, group):
    assert ci_scope.group_of("tests/test_new.py", _read({"tests/test_new.py": text}))[1] == group


def test_a_file_the_change_deleted_counts_as_what_it_held():
    gone = ci_scope.group_of("tests/test_gone.py", _read({"tests/test_gone.py": None}))
    assert gone[1] == "broad"  # unreadable at both ends: no rule can place it


def test_core_and_shared_code_select_both_adapters_and_an_adapter_only_its_own():
    for path in sorted((ROOT / "src" / "earful").rglob("*.py")):
        name = path.relative_to(ROOT).as_posix()
        frameworks = {cell.split()[0] for cell in _runs(name)[1]}
        adapter = name.split("/")[2]
        expected = {"livekit": {"lk"}, "pipecat": {"pc"}}.get(adapter, {"lk", "pc"})
        if name == "src/earful/core/html.py":
            expected = {"lk"}  # the report: drawn and tested on the newest LiveKit release
        assert frameworks == expected, name


def test_each_test_file_selects_the_framework_it_needs():
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        text = path.read_text()
        said = set(re.findall(r'^pytest\.importorskip\("(livekit|pipecat)[."]', text, re.MULTILINE))
        group = ci_scope.group_of(path.relative_to(ROOT).as_posix(), _read())[1]
        assert group == (said.pop() if said else "core"), path.name


def test_a_shape_selects_its_framework_and_the_code_shapes_share_selects_both():
    shapes = sorted((ROOT / "tests" / "shapes").rglob("*.py"))
    assert len(shapes) > 4
    for path in shapes:
        name = path.relative_to(ROOT).as_posix()
        framework = path.parent.name if path.parent.name in ("livekit", "pipecat") else "core"
        assert ci_scope.group_of(name, _read())[1] == framework, name


# A path of each rule, which no earlier rule takes.
_EXAMPLES = {
    "docs/**": "docs/cases/one.md",
    "*.md": "SECURITY.md",
    ".github/ISSUE_TEMPLATE/**": ".github/ISSUE_TEMPLATE/config.yml",
    ".github/pull_request_template.md": ".github/pull_request_template.md",
    "compat/lk-*.lock.txt": "compat/lk-1.5.6.lock.txt",
    "compat/pc-*.lock.txt": "compat/pc-1.16.0.lock.txt",
    "src/earful/pipecat/**": "src/earful/pipecat/clock.py",
    "examples/pipecat_booking/**": "examples/pipecat_booking/test_bot.py",
    "tests/golden/pc-*": "tests/golden/pc-1.16.0.x.jsonl",
    "tests/shapes/pipecat/**": "tests/shapes/pipecat/booking.py",
    "src/earful/livekit/**": "src/earful/livekit/seams.py",
    "examples/booking/**": "examples/booking/test_booking.py",
    "skills/**": "skills/earful/SKILL.md",
    "tests/golden/lk-*": "tests/golden/lk-1.5.6.x.jsonl",
    "tests/bundles/**": "tests/bundles/b/minimal.ledger.jsonl",
    "tests/shapes/livekit/**": "tests/shapes/livekit/booking.py",
    "src/earful/core/html.py": "src/earful/core/html.py",
    "tests/mutants.py": "tests/mutants.py",
    "tests/mutants_seconds.json": "tests/mutants_seconds.json",
    "tests/conftest.py": "tests/conftest.py",
    "tests/*.py": "tests/test_cli.py",
    "tests/shapes/*.py": "tests/shapes/corpus.py",
    "src/**": "src/earful/core/schemas/ledger.json",
    "releases.toml": "releases.toml",
    "pyproject.toml": "pyproject.toml",
    "LICENSE": "LICENSE",
    "NOTICE": "NOTICE",
    ".gitignore": ".gitignore",
}


def test_every_rule_takes_a_path_of_its_own():
    assert [glob for glob, _ in ci_scope.RULES] == list(_EXAMPLES)
    for glob, example in _EXAMPLES.items():
        assert ci_scope.rule_of(example)[0] == glob, example
    assert ci_scope.rule_of("docs") == ("no rule", "broad")  # "**" needs the slash
    assert ci_scope.rule_of("src/earful.py") == ("src/**", "core")
    assert ci_scope.rule_of("docs/x.md.orig")[0] == "docs/**"
    assert ci_scope.rule_of("tests/golden/README.md")[0] == "no rule"  # "*.md" is top-level
    assert ci_scope.rule_of("tests/sub/test_x.py")[0] == "no rule"  # "*" stays in a directory


def test_a_path_the_repository_holds_has_a_rule_unless_it_is_broad_on_purpose():
    listed = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    if listed.returncode != 0:
        pytest.skip("not a git checkout")  # an unpacked source distribution
    unnamed = {p for p in listed.stdout.splitlines() if ci_scope.rule_of(p)[0] == "no rule"}
    on_purpose = re.compile(
        r"\.github/workflows/.+|scripts/.+|\.github/(CODEOWNERS|dependabot\.yml)|\.pre-commit-config\.yaml"
    )
    assert {p for p in unnamed if not on_purpose.fullmatch(p)} == set()


def test_every_group_a_rule_names_exists_and_stands_for_something():
    for _, group in ci_scope.RULES:
        assert group == "declared" or group in ci_scope.GROUPS, group
    for name, items in ci_scope.GROUPS.items():
        jobs, cells = ci_scope._expand(items, "")
        assert jobs or cells, name


def _outputs(*paths, tier="pull-request"):
    if tier == "full":
        return ci_scope.github(ci_scope.plan("full", None))
    return ci_scope.github(ci_scope.plan(tier, list(paths), _read()))


def test_on_a_change_the_middle_livekit_releases_leave_the_slow_tests_out():
    middle = list(LIVEKIT)[1]
    cells = json.loads(_outputs("src/earful/cli.py", f"compat/lk-{middle}.lock.txt")["livekit"])
    assert {cell["version"]: cell["slow"] for cell in cells} == {
        OLDEST: True,
        middle: False,
        NEWEST: True,
    }
    assert all(cell["slow"] for cell in json.loads(_outputs(tier="full")["livekit"]))
    # A change that cannot be read runs every cell: the middle releases on Linux alone are quick.
    unread = json.loads(ci_scope.github(ci_scope.plan("pull-request", None))["livekit"])
    quick = {(cell["os"], cell["python"]) for cell in unread if not cell["slow"]}
    assert quick == {("ubuntu-latest", "3.13")}


def test_the_examples_of_the_pages_run_in_the_livekit_cells_on_linux_with_python_3_13():
    cells = json.loads(_outputs(tier="full")["livekit"])
    ran = [cell for cell in cells if cell["walkthrough"]]
    assert {(cell["os"], cell["python"]) for cell in ran} == {("ubuntu-latest", "3.13")}
    assert [cell["version"] for cell in ran] == list(LIVEKIT)


def test_a_test_of_a_pages_examples_runs_in_one_step_of_a_cell():
    # The quick step leaves out the slow tests and the slow step these, which have their own.
    marked = 0
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        text = path.read_text()
        slow = re.findall(r"^@pytest\.mark\.slow\n@pytest\.mark\.walkthrough$", text, re.MULTILINE)
        assert len(slow) == len(re.findall(r"^@pytest\.mark\.walkthrough$", text, re.MULTILINE))
        marked += len(slow)
    assert marked == 4
    job = CHECKS.partition("\n  livekit:\n")[2].partition("\n  pipecat:\n")[0]
    assert re.findall(r"^          -m (.+) --durations=10$", job, re.MULTILINE) == [
        '"slow and not walkthrough"',
        "walkthrough",
    ]
    assert "        if: matrix.cell.slow && matrix.cell.walkthrough\n" in job


def test_the_real_clock_control_runs_in_the_newest_livekit_cell_on_linux_alone():
    cells = json.loads(_outputs(tier="full")["livekit"])
    controls = [c for c in cells if c["control"]]
    assert [(c["version"], c["os"], c["python"]) for c in controls] == [
        (NEWEST, "ubuntu-latest", "3.13")
    ]


def test_a_cell_carries_what_its_steps_read():
    for name in ("livekit", "pipecat"):
        for cell in json.loads(_outputs(tier="full")[name]):
            assert cell["as_of"] == {**LIVEKIT, **PIPECAT}[cell["version"]]
            assert cell["name"].startswith(("lk ", "pc "))
            for key in cell:
                if key != "name":
                    assert f"matrix.cell.{key}" in CHECKS, key


def test_the_jobs_read_every_output_the_scope_gives_and_no_other():
    written = set(_outputs("src/earful/cli.py"))
    passed_on = set(
        re.findall(r"^      (\w+): \$\{\{ steps\.scope\.outputs\.(\w+) \}\}$", CHECKS, re.MULTILINE)
    )
    assert passed_on == {(name, name) for name in written}
    read = set(re.findall(r"needs\.scope\.outputs\.(\w+)", CHECKS))
    assert read == written


def test_the_jobs_the_scope_names_are_the_jobs_of_the_workflow():
    parts = re.split(r"^  ([a-z_]+):\n", CHECKS.partition("\njobs:\n")[2], flags=re.MULTILINE)
    jobs = dict(zip(parts[1::2], parts[2::2], strict=True))
    assert tuple(jobs) == (*ci_result.JOBS, "result")
    assert set(json.loads(_outputs(tier="full")["due"])) == set(ci_result.JOBS[1:]) - {"prose"}
    assert re.findall(r"^    needs: \[(.*)\]$", jobs["result"], re.MULTILINE) == [
        ", ".join(ci_result.JOBS)
    ]
    assert "    if: always()\n" in jobs["result"]
    # Every job but the first waits for the scope, so none starts before it is known.
    for name in ci_result.JOBS[1:]:
        assert "    needs: scope\n" in jobs[name], name
        output = "breaks" if name in ("control", "breaks") else name
        assert re.search(
            rf"^    if: needs\.scope\.outputs\.{output} (==|!=) ", jobs[name], re.MULTILINE
        ), name


def test_a_change_runs_the_breaks_its_files_can_affect_whatever_rule_names_it():
    m1 = ci_scope.plan("pull-request", ["src/earful/core/checker/m1.py"], _read()).breaks
    assert m1 and len(m1) < len(ci_scope.mutate.MUTANTS)
    assert {m.path for m in m1} >= {"src/earful/core/checker/m1.py"}
    assert ci_scope.plan("pull-request", ["docs/api.md"], _read()).breaks == []
    assert ci_scope.plan("pull-request", ["tools/x.py"], _read()).breaks == []
    scope = ci_scope.plan("pull-request", ["scripts/ci_scope.py"], _read()).breaks
    assert scope and {m.path for m in scope} >= {"scripts/ci_scope.py"}
    assert len(scope) < len(ci_scope.mutate.MUTANTS)
    shared = ci_scope.plan("pull-request", ["tests/conformance.py"], _read()).breaks
    assert len(shared) == len(ci_scope.mutate.MUTANTS)  # a helper the tests share


def test_what_is_left_to_the_full_tier_is_said():
    result = ci_scope.plan("pull-request", ["src/earful/pipecat/sink.py"], _read())
    left = result.left
    assert lk(NEWEST) in left and pc("3.13", "macOS") in left and "the example reports" in left
    assert pc("3.13") not in left and any(line.endswith(" deliberate breaks") for line in left)
    explained = ci_scope.explain(result)
    assert "src/earful/pipecat/sink.py  pipecat  (src/earful/pipecat/**)" in explained
    assert "\nLeft to the full tier:\n" in explained and f"  {lk(NEWEST)}\n" in explained


def test_the_table_on_the_contributing_page_is_the_one_the_scope_reads():
    page = (ROOT / "CONTRIBUTING.md").read_text()
    assert ci_scope.table() in page


_NEEDS = {job: {"result": "skipped", "outputs": {}} for job in ci_result.JOBS}


def _result(due, **results):
    needs = {**_NEEDS, "scope": {"result": "success", "outputs": {}}}
    for job, result in results.items():
        needs[job] = {"result": result, "outputs": {}}
    return ci_result.problems(json.dumps(due), json.dumps(needs))[0]


def test_the_result_passes_when_each_job_the_scope_asks_for_passed_and_no_other_ran():
    assert _result([]) == []
    assert _result(["lint", "livekit"], lint="success", livekit="success") == []
    every = dict.fromkeys(ci_result.JOBS[1:], "success")
    assert _result(list(every), **every) == []


@pytest.mark.parametrize(
    ("due", "results"),
    [
        (["lint"], {}),  # a job asked for was skipped
        (["lint"], {"lint": "failure"}),
        (["livekit"], {"livekit": "cancelled"}),
        ([], {"breaks": "failure"}),  # a job nobody asked for failed
        ([], {"control": "success"}),  # or ran at all
        (["something"], {}),  # a job the workflow does not have
        ("lint", {"lint": "success"}),  # not a list
    ],
)
def test_the_result_fails_otherwise(due, results):
    assert _result(due, **results)


def test_the_result_fails_when_the_scope_failed_or_a_job_is_missing_or_unknown():
    needs = dict(_NEEDS)
    for scope in ("failure", "cancelled", "skipped"):
        needs["scope"] = {"result": scope}
        assert ci_result.problems("[]", json.dumps(needs))[0]
    needs["scope"] = {"result": "success"}
    assert ci_result.problems("not json", json.dumps(needs))[0]
    assert ci_result.problems("[]", "not json")[0]
    missing = {job: v for job, v in needs.items() if job != "report"}
    assert (
        "the result of the job 'report' is missing"
        in ci_result.problems("[]", json.dumps(missing))[0]
    )
    unknown = {**needs, "another": {"result": "success"}}
    wrong = ci_result.problems("[]", json.dumps(unknown))[0]
    assert "no rule here says when the job 'another' has to run" in wrong


def test_the_result_says_what_the_scope_decided_and_what_each_job_did(tmp_path):
    summary = tmp_path / "summary"
    needs = {**_NEEDS, "scope": {"result": "success"}, "lint": {"result": "success"}}
    env = {
        "DUE": '["lint"]',
        "NEEDS": json.dumps(needs),
        "EXPLAINED": json.dumps("Tier: pull-request.\n"),
        "GITHUB_STEP_SUMMARY": str(summary),
        "PATH": "/usr/bin:/bin",
    }
    done = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "ci_result.py")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == "Tier: pull-request.\nJobs:\n  lint: success\n"
    assert summary.read_text() == f"```\n{done.stdout}```\n"
    env["NEEDS"] = json.dumps({**needs, "lint": {"result": "failure"}})
    failed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "ci_result.py")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed.returncode == 1 and "lint had to run and its result is 'failure'" in failed.stderr
