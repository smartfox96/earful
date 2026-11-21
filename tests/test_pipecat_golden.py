"""The committed Pipecat reference ledgers: this machine reproduces them, run after run."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pipecat")

import pipecat
from loguru import logger

from earful.core.ledger import first_difference, normalize
from earful.pipecat.cases import CASES
from earful.pipecat.harness import run_case
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


def test_pipecat_every_reference_case_has_a_ledger_of_the_supported_release_and_no_other():
    tested = SUPPORT["pipecat-ai"].tested[-1]
    committed = sorted(path.name for path in GOLDEN.glob("pc-*"))
    assert committed == sorted(f"pc-{tested}.{name}.ledger.normalized.jsonl" for name in CASES)


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
