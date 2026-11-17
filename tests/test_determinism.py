"""Every case must produce the same normalized ledger on every run."""

import pytest

from earful.livekit.cases import CASES
from earful.livekit.harness import run_case


@pytest.mark.parametrize("name", sorted(CASES))
def test_same_case_same_ledger(name):
    runs = [run_case(CASES[name](), seed=0) for _ in range(5)]
    assert len({r.hash for r in runs}) == 1
    for run in runs:
        assert run.invalid == []
        assert run.framework_warnings == []
