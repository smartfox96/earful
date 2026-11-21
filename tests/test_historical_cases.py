"""The two historical defects: red on affected releases, green on repaired ones."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("livekit.agents")

import livekit.agents as lk

from earful.core.checker import FAIL, PASS, check
from earful.livekit.cases import BOOKING_INTENT, CASES
from earful.livekit.harness import run_case

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
VERSION = tuple(int(part) for part in lk.__version__.split(".")[:3])
FIX_3702 = (1, 6, 6)  # livekit/agents PR #6349
FIX_5509 = (1, 5, 7)  # livekit/agents PR #5535


def _report(case):
    return check(run_case(case).ledger.events)


def test_3702_interrupted_tool_result():
    """A booking's result is missing from the next model call, which books again."""
    report = _report(CASES["book_then_interrupt"]())
    if VERSION < FIX_3702:
        (duplicate,) = report.results["A2"].failures
        assert duplicate.instance == {"intent": BOOKING_INTENT}
        assert duplicate.measures["committed"] == 2
        first = report.results["M2"].failures[0]
        assert first.instance == {"context": "call 2"}
        assert [f.rule for f in first.findings] == ["rule d"]
    else:
        assert report.outcome("A2") == PASS
        assert report.outcome("M2") == PASS
    assert report.outcome("A3") == PASS  # the booking was confirmed only after it committed


def test_5509_stale_reply_over_new_user_turn():
    """A reply still being generated starts playing after the user has taken the floor."""
    report = _report(CASES["speak_over_new_turn"]())
    if VERSION < FIX_5509:
        (over,) = report.results["L2"].failures
        assert over.instance == {"speech": 1}
        assert [f.rule for f in over.findings] == ["rule b"]
        assert "started" in over.findings[0].detail
        assert "after the user took the floor" in over.findings[0].detail
    else:
        assert report.outcome("L2") == PASS


def test_5509_fix_needs_a_pausable_output():
    """The repair pauses the output, so an output that cannot pause is still affected."""
    case = CASES["speak_over_new_turn"]()
    case.sink_can_pause = False
    assert _report(case).outcome("L2") == FAIL


def test_interrupted_reply_is_cut_mid_word():
    report = _report(CASES["barge_in_mid_word"]())
    cut = report.results["M1"].verdicts[0]
    assert cut.outcome == PASS
    assert (cut.measures["played"], cut.measures["cut"]) == (9, 1)
    assert report.failures == ()


@pytest.mark.slow
@pytest.mark.parametrize("issue", ["3702", "5509"])
def test_a_historical_case_script_fails_on_a_verdict_its_release_should_not_give(tmp_path, issue):
    source = (SCRIPTS / f"spike_{issue}.py").read_text()
    (fixed_in,) = re.findall(r"^FIXED_IN = \((\d+), (\d+), (\d+)\)", source, re.MULTILINE)
    # The other side of the installed release, so that the verdict it gives is the wrong one.
    moved = "(99, 0, 0)" if VERSION >= tuple(map(int, fixed_in)) else "(0, 0, 0)"
    wrong = tmp_path / "wrong.py"
    wrong.write_text(source.replace(f"FIXED_IN = ({', '.join(fixed_in)})", f"FIXED_IN = {moved}"))
    for script, status in ((SCRIPTS / f"spike_{issue}.py", 0), (wrong, 1)):
        done = subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True, check=False
        )
        assert done.returncode == status, done.stderr
        assert '"distinct_hashes": 1' in done.stdout
    assert done.stderr.startswith(f"#{issue}: ")
