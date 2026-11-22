"""The committed Pipecat reference ledgers: every machine reproduces them, run after run."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

pytest.importorskip("pipecat")

import pipecat
from loguru import logger

from earful.core.ledger import first_difference, normalize
from earful.pipecat import clock
from earful.pipecat.cases import CASES
from earful.pipecat.harness import run_case
from earful.scripted.clock import run_virtual
from earful.scripted.releases import SUPPORT

GOLDEN = Path(__file__).parent / "golden"


@pytest.fixture
def reference_ledger():
    """Gives a case's committed ledger for the installed pipecat-ai, or skips the test."""

    def committed(case):
        release = pipecat.__version__
        # A release the extra admits and docs/compatibility.md does not list has none.
        if not any(GOLDEN.glob(f"pc-{release}.*")):
            pytest.skip(f"pipecat-ai {release} has no reference ledgers")
        path = GOLDEN / f"pc-{release}.{case}.ledger.normalized.jsonl"
        assert path.exists(), f"no reference ledger of {case} for pipecat-ai {release}"
        return path

    return committed


# tests/golden changes only with scripts/check_all.sh --update, and the diff is reviewed.
@pytest.mark.parametrize("name", sorted(CASES))
def test_pipecat_run_reproduces_the_committed_ledger(name, reference_ledger):
    path = reference_ledger(name)
    lines = normalize(run_case(CASES[name]()).ledger.events)
    assert first_difference(path.read_text("ascii").splitlines(), lines) is None


def test_pipecat_only_a_release_with_no_reference_ledgers_at_all_skips_the_comparison(
    reference_ledger, monkeypatch
):
    monkeypatch.setattr(pipecat, "__version__", "1.16.999")
    with pytest.raises(pytest.skip.Exception, match=r"pipecat-ai 1\.16\.999 has no reference"):
        reference_ledger("greeting")
    tested = SUPPORT["pipecat-ai"].tested[-1]
    monkeypatch.setattr(pipecat, "__version__", tested)
    assert reference_ledger("greeting").name.startswith(f"pc-{tested}.greeting.")
    with pytest.raises(BaseException) as missing:  # a skip here would pass unnoticed
        reference_ledger("no_such_case")
    assert isinstance(missing.value, AssertionError)
    assert "no reference ledger of no_such_case" in str(missing.value)


def test_pipecat_every_reference_case_has_a_ledger_of_each_tested_release_and_no_other():
    committed = sorted(path.name for path in GOLDEN.glob("pc-*"))
    assert committed == sorted(
        f"pc-{tested}.{name}.ledger.normalized.jsonl"
        for tested in SUPPORT["pipecat-ai"].tested
        for name in CASES
    )


def test_pipecat_a_run_waits_with_the_standard_wait_for_whatever_pipecat_installed(monkeypatch):
    """On Python 3.11 pipecat-ai installs wait_for2's, which orders one instant otherwise."""
    before, called = asyncio.wait_for, []

    def installed(awaitable, timeout):
        called.append(timeout)
        return before(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", installed)
    run_case(CASES["greeting"]())
    assert not called and asyncio.wait_for is installed


STANDARD = [clock._wait_for, *([asyncio.tasks.wait_for] if sys.version_info >= (3, 12) else [])]


@pytest.mark.parametrize("wait_for", STANDARD)
def test_pipecat_wait_for_runs_what_it_waits_for_in_the_calling_task(wait_for):
    ran = []

    async def body():
        ran.append(asyncio.current_task())
        return "done"

    async def main():
        assert await wait_for(body(), 1) == "done" and ran == [asyncio.current_task()]
        with pytest.raises(TimeoutError):
            await wait_for(asyncio.sleep(2), 1)
        finished = asyncio.get_running_loop().create_future()
        finished.set_result("ready")
        assert await wait_for(finished, 0) == "ready"
        with pytest.raises(TimeoutError):
            await wait_for(body(), 0)  # out of time before it starts
        assert len(ran) == 1

    run_virtual(main)


@pytest.mark.parametrize("name", sorted(CASES))
def test_pipecat_same_case_same_ledger(name):
    runs = [run_case(CASES[name](), seed=0) for _ in range(5)]
    assert len({r.hash for r in runs}) == 1
    for run in runs:
        assert run.invalid == [] and run.harness_errors == []
        assert run.events[-1]["data"]["framework_warnings"] == 0


@pytest.mark.parametrize("name", sorted(CASES))
def test_pipecat_the_logging_level_does_not_change_the_ledger(name):
    """A log record reads the clock when it is made, and a lower level makes more of them."""
    logger.disable("pipecat")
    try:
        quiet = run_case(CASES[name]()).hash
    finally:
        logger.enable("pipecat")
    logged = []
    sink = logger.add(lambda message: logged.append(message.record["time"]), level="TRACE")
    try:
        assert run_case(CASES[name]()).hash == quiet
    finally:
        logger.remove(sink)
    assert logged
