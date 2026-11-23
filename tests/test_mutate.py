"""The runner of the deliberate breaks chooses, cuts and runs them as it says."""

import json
import subprocess
import sys
from dataclasses import astuple, replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import mutate
from mutants import MUTANTS, Mutant

A_BREAK = MUTANTS[0]


def _project(tmp_path, tests):
    """A repository with one source file and the test files ``tests``, by name."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src/thing.py").write_text("VALUE = 1\n")
    (tmp_path / "tests").mkdir()
    for name, text in tests.items():
        (tmp_path / "tests" / name).write_text(text)
    return tmp_path


def test_a_break_runs_first_the_files_that_hold_a_test_it_names(tmp_path):
    root = _project(
        tmp_path,
        {
            "test_one.py": "def test_stop_that_lands():\n    pass\n",
            "test_two.py": "async def test_other():\n    pass\n",
            "test_three.py": "def test_stop_again():\n    pass\n",
        },
    )
    mutate._keywords.cache_clear()
    try:

        def files(first):
            return mutate.files_of(Mutant("x", "src/thing.py", "1", "2", first), root)

        assert files("stop_that_lands") == ("tests/test_one.py",)
        assert files("STOP_that or other") == ("tests/test_one.py", "tests/test_two.py")
        assert files("test_two") == ("tests/test_two.py",)  # a file's own name is matched
        assert files("asyncio") == ("tests/test_two.py",)  # and the marker of its async tests
        # "tests" is the directory's name, which every test carries.
        assert len(files("tests")) == 3
    finally:
        mutate._keywords.cache_clear()


def test_every_break_names_a_file_and_the_files_are_far_fewer_than_the_suite():
    files = [mutate.files_of(m) for m in MUTANTS]
    assert all(files)
    one = sum(len(f) == 1 for f in files)
    assert one > len(MUTANTS) * 0.8, one


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (["LICENSE"], []),
        ([A_BREAK.path], [m for m in MUTANTS if m.path == A_BREAK.path]),
        (["scripts/mutate.py"], MUTANTS),
        (["tests/conftest.py"], MUTANTS),
        (["tests/scaffolded.py"], MUTANTS),  # a helper the tests share
    ],
)
def test_a_change_chooses_the_breaks_it_can_affect(paths, expected):
    assert mutate.chosen(paths) == list(expected)


def test_a_changed_test_file_chooses_the_breaks_that_run_it():
    picked = mutate.chosen(["tests/test_clock.py"])
    assert picked and all("tests/test_clock.py" in mutate.files_of(m) for m in picked)
    assert len(picked) == sum("tests/test_clock.py" in mutate.files_of(m) for m in MUTANTS)


def test_a_change_to_the_list_chooses_the_breaks_it_changed_or_added():
    before = {m.name: astuple(m) for m in MUTANTS}
    assert mutate.chosen([mutate.MUTANTS_FILE], before) == []
    changed = MUTANTS[3]
    before[changed.name] = astuple(replace(changed, new="something else"))
    del before[MUTANTS[7].name]  # a break added since
    assert mutate.chosen([mutate.MUTANTS_FILE], before) == [changed, MUTANTS[7]]
    assert mutate.chosen([mutate.MUTANTS_FILE], None) == MUTANTS  # the list before is unknown


def test_the_list_of_breaks_at_a_commit_is_read_from_git():
    head = mutate.breaks_at("HEAD")
    if head is None:
        pytest.skip("not a git checkout")  # an unpacked source distribution
    assert len(head) > 100 and all(len(fields) == 5 for fields in head.values())
    assert mutate.breaks_at("no-such-commit") is None


def test_the_parts_hold_every_break_once_and_the_longest_are_spread(tmp_path, monkeypatch):
    breaks = [Mutant(f"b{n}", "src/thing.py", "1", "2", "x") for n in range(10)]
    monkeypatch.setattr(mutate, "MUTANTS", breaks)
    (tmp_path / "tests").mkdir()
    seconds = {"b0": 100, "b1": 90, **{f"b{n}": 1 for n in range(2, 10)}}
    (tmp_path / mutate.SECONDS_FILE).write_text(json.dumps(seconds))
    parts = [mutate.part(breaks, k, 3, tmp_path) for k in (1, 2, 3)]
    assert sorted(m.name for p in parts for m in p) == sorted(m.name for m in breaks)
    assert [m.name for m in parts[0]][:1] == ["b0"] and [m.name for m in parts[1]][:1] == ["b1"]
    assert parts == [mutate.part(breaks, k, 3, tmp_path) for k in (1, 2, 3)]  # the same each time


def test_the_number_of_parts_follows_the_recorded_seconds(tmp_path, monkeypatch):
    breaks = [Mutant(f"b{n}", "src/thing.py", "1", "2", "x") for n in range(4)]
    monkeypatch.setattr(mutate, "MUTANTS", breaks)
    (tmp_path / "tests").mkdir()
    seconds_file = tmp_path / mutate.SECONDS_FILE

    def parts(each):
        seconds_file.write_text(json.dumps({m.name: each for m in breaks}))
        return mutate.part_count(breaks, tmp_path)

    assert mutate.part_count([], tmp_path) == 0
    assert parts(1) == 1
    assert parts(mutate.PART_SECONDS / 2) == 2
    assert parts(mutate.PART_SECONDS * 100) == mutate.MOST_PARTS
    seconds_file.write_text(json.dumps({"b0": mutate.PART_SECONDS}))  # the rest are not recorded
    assert mutate.part_count(breaks, tmp_path) == min(4, mutate.MOST_PARTS)


def test_a_name_the_seconds_hold_and_no_break_has_changes_nothing(tmp_path, monkeypatch):
    breaks = [Mutant(f"b{n}", "src/thing.py", "1", "2", "x") for n in range(4)]
    monkeypatch.setattr(mutate, "MUTANTS", breaks)
    (tmp_path / "tests").mkdir()
    gone = {f"gone{n}": mutate.PART_SECONDS * 10 for n in range(3)}  # renamed or deleted since
    seconds = {"b0": 1, "b1": 1, **gone}
    (tmp_path / mutate.SECONDS_FILE).write_text(json.dumps(seconds))
    # b2 and b3 count as the median of b0 and b1 alone.
    assert mutate._seconds(breaks, tmp_path) == dict.fromkeys(["b0", "b1", "b2", "b3"], 1)
    assert mutate.part_count(breaks, tmp_path) == 1
    assert mutate.recorded()  # the file kept in the repository is read


def test_the_seconds_of_a_run_replace_those_recorded_and_the_rest_are_kept(tmp_path):
    (tmp_path / "tests").mkdir()
    names = [m.name for m in MUTANTS]
    before = {names[0]: 7.0, names[1]: 8.0, "a break deleted since": 9.0}
    (tmp_path / mutate.SECONDS_FILE).write_text(json.dumps(before))
    results = tmp_path / "part.json"
    rows = [{"name": "control: nothing is broken", "seconds": 99.0}]
    rows += [{"name": name, "seconds": 2.5} for name in names[1:3]]
    results.write_text(json.dumps(rows))
    assert mutate.record([results], tmp_path) == 0
    kept = json.loads((tmp_path / mutate.SECONDS_FILE).read_text())
    assert kept == {names[0]: 7.0, names[1]: 2.5, names[2]: 2.5}


def test_a_break_caught_only_outside_the_files_it_ran_first_says_so(tmp_path):
    root = _project(
        tmp_path,
        {
            # The scan chooses this file for "alpha", and its test does not read the code.
            "test_alpha.py": "def test_alpha_holds():\n    pass\n",
            # pytest -k also matches a parameter's id, which the scan does not read.
            "test_beta.py": (
                "import pytest\n\nfrom thing import VALUE\n\n\n"
                "@pytest.mark.parametrize('x', [1], ids=['alpha'])\n"
                "def test_value(x):\n    assert VALUE == x\n"
            ),
        },
    )
    mutate._keywords.cache_clear()
    mutate._test_names.cache_clear()
    try:
        broken = Mutant("x", "src/thing.py", "VALUE = 1", "VALUE = 2", "alpha")
        assert mutate.files_of(broken, root) == ("tests/test_alpha.py",)
        outcome, detail = mutate._outcome(broken, tmp_path / "copy", 60, root)
    finally:
        mutate._keywords.cache_clear()
        mutate._test_names.cache_clear()
    assert outcome == "caught"
    assert detail == mutate.OUTSIDE + "tests/test_beta.py::test_value[alpha]"


def test_a_test_that_never_ends_is_stopped(tmp_path):
    root = _project(
        tmp_path, {"test_hangs.py": "import time\n\ndef test_hangs():\n    time.sleep(20)\n"}
    )
    status, output = mutate._pytest(root, "tests/test_hangs.py", timeout=2)
    assert status == -1 and mutate._judge(status, output) == ("timed out", "no result after 2 s")


def test_a_break_runs_its_tests_with_searches_in_their_own_process(tmp_path, monkeypatch):
    test = "import os\n\ndef test_workers():\n    assert os.environ['EARFUL_WORKERS'] == '{}'\n"
    # Two files: one rewritten within a second at the same size would run its old bytecode.
    root = _project(tmp_path, {"test_none.py": test.format("0"), "test_three.py": test.format("3")})
    monkeypatch.delenv("EARFUL_WORKERS", raising=False)
    assert mutate._pytest(root, "tests/test_none.py", timeout=60)[0] == 0
    monkeypatch.setenv("EARFUL_WORKERS", "3")  # a number given is kept
    assert mutate._pytest(root, "tests/test_three.py", timeout=60)[0] == 0


def test_a_selection_that_matches_no_break_is_refused():
    done = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "mutate.py"),
            "--verify",
            "--only",
            "no such break",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 2 and "no break's name contains 'no such break'" in done.stdout


def test_the_control_of_no_break_runs_nothing_and_passes():
    result = mutate.control([], workers=1, timeout=1)
    assert (result.outcome, result.detail) == ("passed", "0 test files")


def test_a_break_in_a_file_other_than_python_is_checked_without_compiling_it():
    shell = Mutant("x", "scripts/ran_every_test.sh", "set -euo pipefail", "", "run_counts_only")
    assert mutate.problem(shell) is None
