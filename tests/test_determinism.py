"""Every case must produce the same normalized ledger on every run."""

import logging

import pytest

pytest.importorskip("livekit.agents")

from earful.livekit.cases import CASES
from earful.livekit.harness import run_case


@pytest.mark.parametrize("name", sorted(CASES))
def test_same_case_same_ledger(name):
    runs = [run_case(CASES[name](), seed=0) for _ in range(5)]
    assert len({r.hash for r in runs}) == 1
    for run in runs:
        assert run.invalid == []
        assert run.framework_warnings == []


def test_the_environment_cannot_switch_interruption_to_a_model_over_the_network(monkeypatch):
    """With these set, livekit-agents 1.8 builds an interruption detector that is a client."""
    plain = run_case(CASES["barge_in_mid_word"]()).hash
    monkeypatch.setenv("LIVEKIT_DEV_MODE", "1")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "secret")
    monkeypatch.setenv("LIVEKIT_INFERENCE_URL", "http://127.0.0.1:9")  # nothing listens there
    assert run_case(CASES["barge_in_mid_word"]()).hash == plain


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_logging_level_does_not_change_the_ledger(name, caplog):
    """A log record reads the clock when it is made, and a lower level makes more of them."""
    plain = run_case(CASES[name]()).hash
    with caplog.at_level(logging.DEBUG, logger="livekit.agents"):
        assert run_case(CASES[name]()).hash == plain
    assert caplog.records
